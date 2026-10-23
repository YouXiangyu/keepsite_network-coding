"""Durable processing intentions and fenced, short publication transactions."""
import json
import uuid

from .contracts import TicketFields
from .state import present_ticket
from .store import StoreError, encode, now_ms

ACTIVE = ("queued", "ocr", "poster")
MAX_ATTEMPTS = 3
LEASE_MS = 90_000
LIFETIME_MS = 10 * 60_000


def emit(store, db, user_id, ticket_id):
    db.execute("UPDATE tickets SET version=version+1,updated_at=? WHERE user_id=? AND id=?", (now_ms(), user_id, ticket_id))
    store.ensure_sync_baseline(db, user_id)
    store.bump(db, user_id)
    row = db.execute("SELECT * FROM tickets WHERE user_id=? AND id=?", (user_id, ticket_id)).fetchone()
    store.record_ticket_change(db, user_id, present_ticket(store, db, user_id, row))


def clear_poster(db, user_id, ticket_id):
    """Withdraw a current terminal job's cover; stored objects remain recoverable."""
    db.execute("UPDATE tickets SET poster_asset_id=NULL WHERE user_id=? AND id=?", (user_id, ticket_id))
    db.execute("DELETE FROM poster_sources WHERE user_id=? AND ticket_id=?", (user_id, ticket_id))


def cancel_current(db, user_id, ticket_id):
    db.execute("UPDATE processing_jobs SET status='cancelled',lease_until=0,updated_at=? WHERE id IN (SELECT job_id FROM ticket_processing WHERE user_id=? AND ticket_id=?) AND status IN ('queued','ocr','poster')",
               (now_ms(), user_id, ticket_id))


def enqueue(store, db, user_id, ticket_id):
    row = store.ticket(db, user_id, ticket_id)
    cancel_current(db, user_id, ticket_id)
    stamp, identifier = now_ms(), str(uuid.uuid4())
    db.execute("""INSERT INTO processing_jobs
        (id,user_id,ticket_id,input_version,original_asset_id,status,next_attempt_at,deadline_at,created_at,updated_at)
        VALUES (?,?,?,?,?,'queued',?,?,?,?)""",
               (identifier, user_id, ticket_id, row["input_version"], row["original_asset_id"], stamp, stamp + LIFETIME_MS, stamp, stamp))
    db.execute("INSERT INTO ticket_processing VALUES (?,?,?) ON CONFLICT(user_id,ticket_id) DO UPDATE SET job_id=excluded.job_id",
               (user_id, ticket_id, identifier))
    return identifier


def current_ticket(db, job):
    return db.execute("""SELECT t.* FROM tickets t JOIN ticket_processing p ON p.user_id=t.user_id AND p.ticket_id=t.id
        WHERE t.user_id=? AND t.id=? AND t.deleted_at IS NULL AND t.input_version=? AND t.original_asset_id=? AND p.job_id=?""",
                      (job["user_id"], job["ticket_id"], job["input_version"], job["original_asset_id"], job["id"])).fetchone()


def fenced(db, job):
    persisted = db.execute("SELECT * FROM processing_jobs WHERE id=?", (job["id"],)).fetchone()
    if (not persisted or persisted["lease_owner"] != job["lease_owner"] or persisted["lease_token"] != job["lease_token"]
            or persisted["status"] not in ("ocr", "poster") or persisted["lease_until"] <= now_ms()
            or persisted["deadline_at"] <= now_ms()):
        return None
    return current_ticket(db, persisted)


def is_current(store, job):
    with store.transaction(timeout=1) as db:
        return fenced(db, job) is not None


def claim(store, identifier, owner):
    with store.transaction(True, timeout=1) as db:
        row = db.execute("SELECT * FROM processing_jobs WHERE id=?", (identifier,)).fetchone()
        if not row or row["status"] not in ACTIVE:
            return None
        stamp = now_ms()
        if row["next_attempt_at"] > stamp or row["status"] != "queued" and row["lease_until"] > stamp:
            return None
        if not current_ticket(db, row):
            db.execute("UPDATE processing_jobs SET status='cancelled',updated_at=? WHERE id=?", (stamp, identifier))
            return None
        if row["attempts"] >= MAX_ATTEMPTS or row["deadline_at"] <= stamp:
            db.execute("UPDATE processing_jobs SET status='failed',error=?,lease_until=0,updated_at=? WHERE id=?",
                       ("处理重试已结束，原票已保留，请手动重试。", stamp, identifier))
            clear_poster(db, row["user_id"], row["ticket_id"])
            emit(store, db, row["user_id"], row["ticket_id"])
            return None
        state = "poster" if row["ocr_fields"] is not None else "ocr"
        db.execute("UPDATE processing_jobs SET status=?,error=NULL,attempts=attempts+1,lease_owner=?,lease_token=lease_token+1,lease_until=?,updated_at=? WHERE id=?",
                   (state, owner, min(stamp + LEASE_MS, row["deadline_at"]), stamp, identifier))
        emit(store, db, row["user_id"], row["ticket_id"])
        return dict(db.execute("SELECT * FROM processing_jobs WHERE id=?", (identifier,)).fetchone())


def save_ocr(store, job, candidates):
    validated = TicketFields.model_validate(candidates).model_dump()
    with store.transaction(True, timeout=1) as db:
        ticket = fenced(db, job)
        if ticket is None:
            return None
        fields, sources = json.loads(ticket["fields"]), json.loads(ticket["field_sources"])
        for name, value in validated.items():
            if value and sources[name] not in {"user", "migration"}:
                fields[name], sources[name] = value, "ocr"
        stamp = now_ms()
        db.execute("UPDATE tickets SET fields=?,field_sources=? WHERE user_id=? AND id=?",
                   (encode(fields), encode(sources), job["user_id"], job["ticket_id"]))
        db.execute("UPDATE processing_jobs SET ocr_fields=?,status='poster',updated_at=? WHERE id=?",
                   (encode(validated), stamp, job["id"]))
        emit(store, db, job["user_id"], job["ticket_id"])
        return fields


def matching_fields(store, job):
    with store.transaction(timeout=1) as db:
        row = fenced(db, job)
        return json.loads(row["fields"]) if row is not None else None


def prepare_artifact(store, job, descriptor, source, temp_name):
    with store.transaction(True, timeout=1) as db:
        if fenced(db, job) is None:
            return None
        identifier = str(uuid.uuid4())
        db.execute("INSERT INTO processing_artifacts VALUES (?,?,?,?,?,?,?)",
                   (identifier, job["id"], job["user_id"], encode(descriptor), encode(source), temp_name, now_ms()))
        db.execute("UPDATE processing_jobs SET artifact_id=?,updated_at=? WHERE id=?", (identifier, now_ms(), job["id"]))
        return identifier


def artifact(store, job):
    with store.transaction(timeout=1) as db:
        if fenced(db, job) is None:
            return None
        row = db.execute("SELECT a.* FROM processing_artifacts a JOIN processing_jobs j ON j.artifact_id=a.id WHERE j.id=? AND a.user_id=?",
                         (job["id"], job["user_id"])).fetchone()
        return dict(row) if row else None


def finish(store, job, prepared=None):
    with store.transaction(True, timeout=1) as db:
        ticket = fenced(db, job)
        if ticket is None:
            return False
        stamp = now_ms()
        if prepared is not None:
            # The descriptor came from this fenced job and was verified in storage.
            persisted = db.execute("SELECT * FROM processing_artifacts WHERE id=? AND job_id=? AND user_id=?",
                                   (prepared["id"], job["id"], job["user_id"])).fetchone()
            current = db.execute("SELECT artifact_id FROM processing_jobs WHERE id=?", (job["id"],)).fetchone()
            if not persisted or current[0] != persisted["id"]:
                return False
            descriptor, source = json.loads(persisted["descriptor"]), json.loads(persisted["source"])
            store.register_asset(db, descriptor)
            db.execute("INSERT OR IGNORE INTO asset_grants VALUES (?,?)", (job["user_id"], descriptor["id"]))
            db.execute("UPDATE tickets SET poster_asset_id=? WHERE user_id=? AND id=?",
                       (descriptor["id"], job["user_id"], job["ticket_id"]))
            db.execute("INSERT INTO poster_sources VALUES (?,?,?,?) ON CONFLICT(user_id,ticket_id) DO UPDATE SET asset_id=excluded.asset_id,source=excluded.source",
                       (job["user_id"], job["ticket_id"], descriptor["id"], encode(source)))
        else:
            clear_poster(db, job["user_id"], job["ticket_id"])
        db.execute("UPDATE processing_jobs SET status=?,error=NULL,lease_until=0,updated_at=? WHERE id=?",
                   ("ready" if prepared else "no_match", stamp, job["id"]))
        emit(store, db, job["user_id"], job["ticket_id"])
        return True


def fail(store, job, *, interrupted=False):
    with store.transaction(True, timeout=1) as db:
        persisted = db.execute("SELECT * FROM processing_jobs WHERE id=?", (job["id"],)).fetchone()
        if (not persisted or persisted["lease_owner"] != job["lease_owner"] or persisted["lease_token"] != job["lease_token"]
                or persisted["status"] not in ("ocr", "poster")):
            return False
        if not current_ticket(db, persisted):
            db.execute("UPDATE processing_jobs SET status='cancelled',lease_until=0,updated_at=? WHERE id=?", (now_ms(), job["id"]))
            return True
        retry = persisted["attempts"] < MAX_ATTEMPTS and persisted["deadline_at"] > now_ms()
        message = "处理暂时失败，正在有限重试；原票已保留。" if retry else "处理失败，原票已保留，请手动重试。"
        if interrupted and retry:
            message = "处理已中断，服务恢复后继续；原票已保留。"
        stamp = now_ms()
        db.execute("UPDATE processing_jobs SET status=?,error=?,lease_until=0,next_attempt_at=?,last_enqueued_at=0,updated_at=? WHERE id=?",
                   ("queued" if retry else "failed", message, stamp + min(30_000, 2 ** persisted["attempts"] * 1000), stamp, job["id"]))
        if not retry:
            clear_poster(db, job["user_id"], job["ticket_id"])
        emit(store, db, job["user_id"], job["ticket_id"])
        return True


def due_jobs(store, limit=50):
    with store.transaction() as db:
        stamp = now_ms()
        return [row[0] for row in db.execute("""SELECT id FROM processing_jobs
            WHERE (status='queued' OR status IN ('ocr','poster') AND lease_until<=?)
            AND next_attempt_at<=? AND last_enqueued_at<=? ORDER BY last_enqueued_at,created_at,id LIMIT ?""",
                                            (stamp, stamp, stamp - 10_000, limit))]


def mark_enqueued(store, identifier):
    with store.transaction(True, timeout=1) as db:
        db.execute("UPDATE processing_jobs SET last_enqueued_at=? WHERE id=?", (now_ms(), identifier))


def expire_jobs(store, limit=50):
    """Deadline convergence is SQL-only and keeps working while Redis is down."""
    with store.transaction() as db:
        identifiers = [row[0] for row in db.execute("SELECT id FROM processing_jobs WHERE status IN ('queued','ocr','poster') AND deadline_at<=? ORDER BY deadline_at LIMIT ?", (now_ms(), limit))]
    if not identifiers:
        return 0
    expired = 0
    with store.transaction(True, timeout=1) as db:
        for identifier in identifiers:
            job = db.execute("SELECT * FROM processing_jobs WHERE id=? AND status IN ('queued','ocr','poster') AND deadline_at<=?", (identifier, now_ms())).fetchone()
            if not job:
                continue
            ticket = current_ticket(db, job)
            db.execute("UPDATE processing_jobs SET status=?,error=?,lease_until=0,updated_at=? WHERE id=?",
                       ("failed" if ticket else "cancelled", "处理等待已超时，原票已保留，请手动重试。", now_ms(), identifier))
            if ticket:
                clear_poster(db, job["user_id"], job["ticket_id"])
                emit(store, db, job["user_id"], job["ticket_id"])
            expired += 1
    return expired


def has_active(store):
    with store.transaction() as db:
        return db.execute("SELECT 1 FROM processing_jobs WHERE status IN ('queued','ocr','poster') LIMIT 1").fetchone() is not None

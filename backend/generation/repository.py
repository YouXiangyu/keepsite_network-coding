"""Own short generation-state transactions and account/version checks."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..store import Store, encode, now_ms


@dataclass(frozen=True)
class GenerationTask:
    job: dict
    asset: dict


@dataclass(frozen=True)
class ClaimResult:
    progressed: bool
    task: GenerationTask | None = None


def recover_interrupted(store: Store):
    with store.transaction(True) as db:
        owners = [row[0] for row in db.execute("SELECT DISTINCT user_id FROM jobs WHERE status='processing'")]
        db.execute("UPDATE jobs SET status='queued',error='',updated_at=? WHERE status='processing'", (now_ms(),))
        for owner in owners:
            store.bump(db, owner)


def _matches(current, job):
    return bool(current and current["user_id"] == job["user_id"] and current["job_id"] == job["id"]
                and current["input_revision"] == job["input_revision"] and current["asset_id"] == job["asset_id"])


def claim_next(store: Store, status) -> ClaimResult:
    with store.transaction(True) as db:
        if not status["available"]:
            owners = [row[0] for row in db.execute("SELECT DISTINCT user_id FROM jobs WHERE status='queued'")]
            db.execute("UPDATE jobs SET status='blocked',error=?,updated_at=? WHERE status='queued'", (status["reason"], now_ms()))
            for owner in owners:
                store.bump(db, owner)
            return ClaimResult(progressed=False)
        job = db.execute("SELECT * FROM jobs WHERE status IN ('queued','blocked') ORDER BY created_at,id LIMIT 1").fetchone()
        if not job:
            return ClaimResult(progressed=False)
        current = db.execute("SELECT * FROM collections WHERE user_id=? AND id=?", (job["user_id"], job["collection_id"])).fetchone()
        if not _matches(current, job):
            db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE user_id=? AND id=?", (now_ms(), job["user_id"], job["id"]))
            store.bump(db, job["user_id"])
            return ClaimResult(progressed=True)
        asset = dict(store.asset(db, job["user_id"], job["asset_id"]))
        db.execute("UPDATE jobs SET status='processing',error='',updated_at=? WHERE user_id=? AND id=?", (now_ms(), job["user_id"], job["id"]))
        store.bump(db, job["user_id"])
        job = dict(job)
    return ClaimResult(progressed=True, task=GenerationTask(job=job, asset=asset))


def finalize(store: Store, job, model, error: str, is_stopping: Callable[[], bool]):
    with store.transaction(True) as db:
        current = db.execute("SELECT * FROM collections WHERE user_id=? AND id=?", (job["user_id"], job["collection_id"])).fetchone()
        if not _matches(current, job):
            db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE user_id=? AND id=?", (now_ms(), job["user_id"], job["id"]))
        elif error:
            state = "queued" if is_stopping() else "failed"
            db.execute("UPDATE jobs SET status=?,error=?,result=NULL,updated_at=? WHERE user_id=? AND id=?", (state, "" if state == "queued" else error, now_ms(), job["user_id"], job["id"]))
            store.bump(db, job["user_id"])
        else:
            store.register_model(db, model)
            db.execute("INSERT OR IGNORE INTO model_grants VALUES (?,?)", (job["user_id"], model["id"]))
            db.execute("UPDATE jobs SET status='ready',error='',model_id=?,result=?,updated_at=? WHERE user_id=? AND id=?", (model["id"], encode({"modelId": model["id"], "camera": model["camera"]}), now_ms(), job["user_id"], job["id"]))
            db.execute("UPDATE collections SET model_id=? WHERE user_id=? AND id=? AND job_id=? AND input_revision=?", (model["id"], job["user_id"], job["collection_id"], job["id"], job["input_revision"]))
            store.bump(db, job["user_id"])

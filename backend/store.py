"""Schema 3 movie tickets; application startup never upgrades legacy data."""
import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from .object_storage import ObjectStore


class StoreError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code, self.message = status_code, message


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def now_ms():
    return time.time_ns() // 1_000_000


def require_schema3_root(root: Path):
    if (root / "RESTORE_INCOMPLETE").exists():
        raise RuntimeError("Incomplete restoration: do not start this data directory.")
    database = root / "memoverse.sqlite3"
    if not database.exists():
        return
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error as error:
        raise RuntimeError("Unknown/legacy database: use a new schema-3 directory. Original data was not changed.") from error
    if not row or row[0] != "3":
        raise RuntimeError("Legacy schema: migrate to a new schema-3 directory. Original data was not changed.")


class Store:
    def __init__(self, root: Path, *, storage: ObjectStore):
        require_schema3_root(root)
        existing = root / "memoverse.sqlite3"
        if existing.exists():
            with closing(sqlite3.connect(existing.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                binding = db.execute("SELECT value FROM meta WHERE key='storage_binding'").fetchone()
                if binding and binding[0] != storage.storage_binding:
                    raise RuntimeError("Storage binding changed: use an explicitly verified isolated restore or migration.")
                if not binding and db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]:
                    raise RuntimeError("Storage binding is unknown for existing media: use an explicitly verified isolated restore.")
        self.root, self.storage = root.resolve(), storage
        self.processing_enabled = False
        self.processing_unavailable_reason = "电影票处理服务尚未配置。"
        self.processing_wake = lambda: None
        for folder in (self.root, self.root / "tmp", self.root / "exports"):
            folder.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "memoverse.sqlite3"
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, object_key TEXT NOT NULL UNIQUE, mime TEXT NOT NULL,
                    size INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_grants (
                    user_id TEXT NOT NULL REFERENCES users(id), asset_id TEXT NOT NULL REFERENCES assets(id),
                    PRIMARY KEY(user_id,asset_id)
                );
                CREATE TABLE IF NOT EXISTS upload_receipts (
                    user_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                    PRIMARY KEY(user_id,asset_id),
                    FOREIGN KEY(user_id,asset_id) REFERENCES asset_grants(user_id,asset_id)
                );
                CREATE TABLE IF NOT EXISTS tickets (
                    user_id TEXT NOT NULL REFERENCES users(id), id TEXT NOT NULL,
                    fields TEXT NOT NULL, field_sources TEXT NOT NULL,
                    original_asset_id TEXT NOT NULL, poster_asset_id TEXT,
                    version INTEGER NOT NULL DEFAULT 1, input_version INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, deleted_at INTEGER,
                    operation_id TEXT NOT NULL,
                    PRIMARY KEY(user_id,id),
                    FOREIGN KEY(user_id,original_asset_id) REFERENCES asset_grants(user_id,asset_id),
                    FOREIGN KEY(user_id,poster_asset_id) REFERENCES asset_grants(user_id,asset_id)
                );
                CREATE TABLE IF NOT EXISTS operations (
                    user_id TEXT NOT NULL REFERENCES users(id), id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, created_at INTEGER NOT NULL,
                    PRIMARY KEY(user_id,id)
                );
                CREATE TABLE IF NOT EXISTS sync_baselines (
                    user_id TEXT PRIMARY KEY REFERENCES users(id), cursor INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ticket_changes (
                    user_id TEXT NOT NULL REFERENCES users(id), cursor INTEGER NOT NULL,
                    ticket_id TEXT NOT NULL, payload TEXT NOT NULL, created_at INTEGER NOT NULL,
                    PRIMARY KEY(user_id,cursor)
                );
                CREATE TABLE IF NOT EXISTS upload_intents (
                    user_id TEXT NOT NULL REFERENCES users(id), id TEXT NOT NULL,
                    asset_id TEXT NOT NULL, object_key TEXT NOT NULL, mime TEXT NOT NULL,
                    size INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending','stored','attached')),
                    storage_identity TEXT NOT NULL, created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL, last_checked_at INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(user_id,id)
                );
                CREATE INDEX IF NOT EXISTS upload_pending ON upload_intents(status,last_checked_at,created_at);
                CREATE TABLE IF NOT EXISTS processing_jobs (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), ticket_id TEXT NOT NULL,
                    input_version INTEGER NOT NULL, original_asset_id TEXT NOT NULL,
                    status TEXT NOT NULL, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT, lease_token INTEGER NOT NULL DEFAULT 0, lease_until INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at INTEGER NOT NULL, deadline_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                    last_enqueued_at INTEGER NOT NULL DEFAULT 0, ocr_fields TEXT, artifact_id TEXT,
                    FOREIGN KEY(user_id,ticket_id) REFERENCES tickets(user_id,id),
                    FOREIGN KEY(user_id,original_asset_id) REFERENCES asset_grants(user_id,asset_id)
                );
                CREATE TABLE IF NOT EXISTS ticket_processing (
                    user_id TEXT NOT NULL, ticket_id TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES processing_jobs(id),
                    PRIMARY KEY(user_id,ticket_id), FOREIGN KEY(user_id,ticket_id) REFERENCES tickets(user_id,id)
                );
                CREATE TABLE IF NOT EXISTS processing_artifacts (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES processing_jobs(id), user_id TEXT NOT NULL,
                    descriptor TEXT NOT NULL, source TEXT NOT NULL, temp_name TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS poster_sources (
                    user_id TEXT NOT NULL, ticket_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                    source TEXT NOT NULL, PRIMARY KEY(user_id,ticket_id),
                    FOREIGN KEY(user_id,ticket_id) REFERENCES tickets(user_id,id),
                    FOREIGN KEY(user_id,asset_id) REFERENCES asset_grants(user_id,asset_id)
                );
                CREATE TABLE IF NOT EXISTS processing_cleanup (
                    temp_name TEXT PRIMARY KEY, artifact_id TEXT, reason TEXT NOT NULL, cleaned_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS processing_due ON processing_jobs(status,next_attempt_at,last_enqueued_at);
                CREATE INDEX IF NOT EXISTS tickets_active ON tickets(user_id,deleted_at,created_at);
            """)
            for key, value in {"schema_version": "3", "server_id": str(uuid.uuid4()), "storage_binding": storage.storage_binding}.items():
                db.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, value))
            bound = db.execute("SELECT value FROM meta WHERE key='storage_binding'").fetchone()[0]
            if bound != storage.storage_binding:
                raise RuntimeError("Storage binding changed during initialization; application was not started.")
            db.execute("INSERT OR IGNORE INTO sync_baselines SELECT id,revision FROM users")
            db.commit()

    def connect(self, *, timeout=15):
        db = sqlite3.connect(self.database, timeout=timeout)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self, write=False, *, timeout=15):
        db = self.connect(timeout=timeout)
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def require_user(db, user_id):
        row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise StoreError(401, "账号尚未初始化，请重新登录。")
        return row

    @staticmethod
    def bump(db, user_id):
        if db.execute("UPDATE users SET revision=revision+1 WHERE id=?", (user_id,)).rowcount != 1:
            raise StoreError(401, "账号不可用，请重新登录。")

    @staticmethod
    def ensure_sync_baseline(db, user_id):
        """Imported/restored metadata begins at this cursor, not invented history."""
        Store.require_user(db, user_id)
        db.execute("INSERT OR IGNORE INTO sync_baselines SELECT id,revision FROM users WHERE id=?", (user_id,))

    @staticmethod
    def record_ticket_change(db, user_id, ticket):
        revision = Store.require_user(db, user_id)["revision"]
        db.execute("INSERT INTO ticket_changes VALUES (?,?,?,?,?)",
                   (user_id, revision, ticket["id"], encode(ticket), now_ms()))

    @staticmethod
    def asset(db, user_id, identifier):
        row = db.execute("SELECT a.* FROM assets a JOIN asset_grants g ON g.asset_id=a.id WHERE g.user_id=? AND a.id=?", (user_id, identifier)).fetchone()
        if not row:
            raise StoreError(404, "图片不存在或无权访问。")
        return row

    @staticmethod
    def referenced_asset(db, user_id, identifier):
        asset = Store.asset(db, user_id, identifier)
        reference = db.execute("SELECT 1 FROM tickets WHERE user_id=? AND deleted_at IS NULL AND (original_asset_id=? OR poster_asset_id=?) LIMIT 1", (user_id, identifier, identifier)).fetchone()
        if not reference:
            raise StoreError(404, "图片不存在或无权访问。")
        return asset

    def attach_asset(self, db, user_id, identifier):
        """Only a freshly verified upload or an existing live reference is attachable."""
        asset = Store.asset(db, user_id, identifier)
        receipt = db.execute("SELECT 1 FROM upload_receipts r JOIN upload_intents u ON u.user_id=r.user_id AND u.asset_id=r.asset_id WHERE r.user_id=? AND r.asset_id=? AND u.status='stored' AND u.storage_identity=? LIMIT 1",
                             (user_id, identifier, self.storage.cache_identity)).fetchone()
        if not receipt:
            Store.referenced_asset(db, user_id, identifier)
        db.execute("DELETE FROM upload_receipts WHERE user_id=? AND asset_id=?", (user_id, identifier))
        db.execute("UPDATE upload_intents SET status='attached',updated_at=? WHERE user_id=? AND asset_id=? AND status IN ('pending','stored')",
                   (now_ms(), user_id, identifier))
        return asset

    @staticmethod
    def ticket(db, user_id, identifier):
        row = db.execute("SELECT * FROM tickets WHERE user_id=? AND id=? AND deleted_at IS NULL", (user_id, identifier)).fetchone()
        if not row:
            raise StoreError(404, "电影票不存在或已删除。")
        return row

    @staticmethod
    def register_asset(db, asset):
        values = (asset["id"], asset["path"], asset["mimeType"], asset["size"], asset["width"], asset["height"])
        old = db.execute("SELECT id,object_key,mime,size,width,height FROM assets WHERE id=?", (asset["id"],)).fetchone()
        if old:
            if tuple(old) != values:
                raise StoreError(409, "不可变图片元数据冲突，未覆盖原图。")
            return
        db.execute("INSERT INTO assets VALUES (?,?,?,?,?,?,?)", (*values, now_ms()))

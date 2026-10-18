from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path

from .template import InitialTemplate, canonical
from .object_storage import ObjectStore


class StoreError(Exception):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code, self.message = status_code, message


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def require_schema2_root(root: Path):
    """Inspect base-file identity without creating WAL/SHM or inferring ownership.

    An unknown database whose identity exists only in a WAL fails closed. Once
    the base file is confirmed as schema 2, normal owned connections may recover
    that database's WAL. No checkpoint is performed on an unrecognized source.
    """
    if (root / "RESTORE_INCOMPLETE").exists():
        raise RuntimeError("Incomplete account restoration: do not start this data directory.")
    database = root / "memoverse.sqlite3"
    if not database.exists():
        return
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.Error as error:
        raise RuntimeError("Unknown/legacy database: choose a new schema-2 data directory. Original data was not changed.") from error
    if not row or row[0] != "2":
        raise RuntimeError("Legacy schema detected: choose a new schema-2 data directory. Original data was not changed.")


class Store:
    def __init__(self, root: Path, *, storage: ObjectStore, template: InitialTemplate | None):
        require_schema2_root(root)
        self.root = root.resolve()
        self.storage, self.template = storage, template
        for folder in (self.root, self.root / "tmp", self.root / "exports"):
            folder.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "memoverse.sqlite3"
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0, template_version TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS profiles (
                    user_id TEXT PRIMARY KEY REFERENCES users(id), payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, object_key TEXT NOT NULL UNIQUE, mime TEXT NOT NULL,
                    size INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS models (
                    id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(id),
                    model_key TEXT NOT NULL UNIQUE, files TEXT NOT NULL, camera TEXT NOT NULL,
                    metadata TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_grants (
                    user_id TEXT NOT NULL REFERENCES users(id), asset_id TEXT NOT NULL REFERENCES assets(id),
                    PRIMARY KEY(user_id, asset_id)
                );
                CREATE TABLE IF NOT EXISTS model_grants (
                    user_id TEXT NOT NULL REFERENCES users(id), model_id TEXT NOT NULL REFERENCES models(id),
                    PRIMARY KEY(user_id, model_id)
                );
                CREATE TABLE IF NOT EXISTS collections (
                    user_id TEXT NOT NULL REFERENCES users(id), id TEXT NOT NULL, payload TEXT NOT NULL,
                    asset_id TEXT NOT NULL, model_id TEXT, version INTEGER NOT NULL DEFAULT 1,
                    input_revision INTEGER NOT NULL DEFAULT 1, job_id TEXT, create_fingerprint TEXT,
                    PRIMARY KEY(user_id,id),
                    FOREIGN KEY(user_id,asset_id) REFERENCES asset_grants(user_id,asset_id),
                    FOREIGN KEY(user_id,model_id) REFERENCES model_grants(user_id,model_id)
                );
                CREATE TABLE IF NOT EXISTS shares (
                    user_id TEXT NOT NULL REFERENCES users(id), id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(user_id,id)
                );
                CREATE TABLE IF NOT EXISTS community (
                    user_id TEXT NOT NULL REFERENCES users(id), id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(user_id,id)
                );
                CREATE TABLE IF NOT EXISTS likes (
                    user_id TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY(user_id,id),
                    FOREIGN KEY(user_id,id) REFERENCES community(user_id,id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    collection_id TEXT NOT NULL, asset_id TEXT NOT NULL, input_revision INTEGER NOT NULL,
                    status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', model_id TEXT,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, result TEXT,
                    UNIQUE(user_id,collection_id,input_revision),
                    FOREIGN KEY(user_id,asset_id) REFERENCES asset_grants(user_id,asset_id),
                    FOREIGN KEY(user_id,model_id) REFERENCES model_grants(user_id,model_id)
                );
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,created_at);
                CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(user_id,id);
            """)
            for key, value in {"schema_version": "2", "server_id": str(uuid.uuid4())}.items():
                db.execute("INSERT OR IGNORE INTO meta VALUES (?,?)", (key, value))
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
    def asset(db, user_id, identifier):
        row = db.execute("SELECT a.* FROM assets a JOIN asset_grants g ON g.asset_id=a.id WHERE g.user_id=? AND a.id=?", (user_id, identifier)).fetchone()
        if not row:
            raise StoreError(404, "图片不存在或无权访问。")
        return row

    @staticmethod
    def model(db, user_id, identifier):
        row = db.execute("SELECT m.* FROM models m JOIN model_grants g ON g.model_id=m.id WHERE g.user_id=? AND m.id=?", (user_id, identifier)).fetchone()
        if not row:
            raise StoreError(404, "模型不存在或无权访问。")
        return row

    @staticmethod
    def collection(db, user_id, identifier):
        row = db.execute("SELECT * FROM collections WHERE user_id=? AND id=?", (user_id, identifier)).fetchone()
        if not row:
            raise StoreError(404, "这件收藏已不存在或无权访问。")
        return row

    @staticmethod
    def job(db, user_id, identifier):
        row = db.execute("SELECT * FROM jobs WHERE user_id=? AND id=?", (user_id, identifier)).fetchone()
        if not row:
            raise StoreError(404, "生成任务不存在或无权访问。")
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

    @staticmethod
    def register_model(db, model):
        metadata = {key: value for key, value in model.items() if key not in {"id", "assetId", "path", "files", "camera"}}
        values = (model["id"], model["assetId"], model["path"], canonical(model["files"]).decode(), canonical(model["camera"]).decode(), canonical(metadata).decode())
        old = db.execute("SELECT id,asset_id,model_key,files,camera,metadata FROM models WHERE id=?", (model["id"],)).fetchone()
        if old:
            if tuple(old) != values:
                raise StoreError(409, "不可变模型元数据冲突，未覆盖已有模型。")
            return
        db.execute("INSERT INTO models VALUES (?,?,?,?,?,?,?)", (*values, now_ms()))

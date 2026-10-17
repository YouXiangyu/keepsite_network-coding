from __future__ import annotations

import copy
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
    def __init__(self, root: Path, *, storage: ObjectStore, template: InitialTemplate):
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

    def connect(self):
        db = sqlite3.connect(self.database, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self, write=False):
        db = self.connect()
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

    def ensure_account(self, user_id, email, name):
        if not isinstance(user_id, str) or not user_id or len(user_id) > 512:
            raise StoreError(401, "认证身份无效。")
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                return
        # Potentially slow object transfer is outside the SQL write transaction.
        self.template.prepare_objects(self.storage)
        with self.transaction(True) as db:
            if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                return
            display_name = (name or email.partition("@")[0] or "新朋友").strip()[:24] or "新朋友"
            db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)", (user_id, email, name, 1, self.template.version, now_ms()))
            for asset in self.template.assets.values():
                self.register_asset(db, asset)
                db.execute("INSERT INTO asset_grants VALUES (?,?)", (user_id, asset["id"]))
            for model in self.template.models.values():
                self.register_model(db, model)
                db.execute("INSERT INTO model_grants VALUES (?,?)", (user_id, model["id"]))
            initial = copy.deepcopy(self.template.data)
            initial["profile"]["name"] = display_name
            db.execute("INSERT INTO profiles VALUES (?,?)", (user_id, encode(initial["profile"])))
            for item in initial["collections"]:
                payload = {key: value for key, value in item.items() if key not in {"assetId", "modelId", "generation", "version"}}
                db.execute("INSERT INTO collections(user_id,id,payload,asset_id,model_id,version) VALUES (?,?,?,?,?,?)", (user_id, item["id"], encode(payload), item["assetId"], item["modelId"], item.get("version", 1)))
            for share in initial["shares"]:
                db.execute("INSERT INTO shares VALUES (?,?,?)", (user_id, share["id"], encode(share)))
            for item in initial["community"]:
                db.execute("INSERT INTO community VALUES (?,?,?)", (user_id, item["id"], encode(item)))
            for identifier in initial["likedCommunityIds"]:
                db.execute("INSERT INTO likes VALUES (?,?)", (user_id, identifier))

    def decorate_content(self, db, user_id, original):
        item = copy.deepcopy(original)
        for key in ("customImage", "modelUrl", "modelCamera", "generationJobId", "generationError"):
            item.pop(key, None)
        if item.get("assetId"):
            self.asset(db, user_id, item["assetId"])
            item["customImage"] = f"/api/media/{item['assetId']}"
        if item.get("modelId"):
            model = self.model(db, user_id, item["modelId"])
            if model["asset_id"] != item.get("assetId"):
                raise StoreError(409, "模型与图片引用不一致。")
            item["generation"] = "ready"
            item["modelUrl"] = f"/api/models/{model['id']}/model.glb"
            item["modelCamera"] = json.loads(model["camera"])
        return item

    def present_item(self, db, user_id, row):
        item = json.loads(row["payload"])
        item.update(version=row["version"], assetId=row["asset_id"], generation="queued")
        item.pop("modelId", None)
        if row["model_id"]:
            item["modelId"] = row["model_id"]
        item = self.decorate_content(db, user_id, item)
        if row["job_id"]:
            job = self.job(db, user_id, row["job_id"])
            item["generation"] = job["status"]
            item["generationJobId"] = job["id"]
            if job["error"]:
                item["generationError"] = job["error"]
            if job["status"] != "ready":
                item.pop("modelUrl", None)
                item.pop("modelCamera", None)
        return item

    def state(self, db, user_id):
        user = self.require_user(db, user_id)
        server = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
        collections = [self.present_item(db, user_id, row) for row in db.execute("SELECT * FROM collections WHERE user_id=? ORDER BY rowid", (user_id,))]
        shares = []
        for row in db.execute("SELECT payload FROM shares WHERE user_id=? ORDER BY rowid", (user_id,)):
            share = json.loads(row[0])
            share["item"] = self.decorate_content(db, user_id, share["item"])
            shares.append(share)
        community = [self.decorate_content(db, user_id, json.loads(row[0])) for row in db.execute("SELECT payload FROM community WHERE user_id=? ORDER BY rowid", (user_id,))]
        profile = db.execute("SELECT payload FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        return {"initialized": True, "serverId": server, "userId": user_id, "revision": user["revision"], "data": {
            "collections": collections, "shares": shares, "community": community,
            "profile": json.loads(profile[0]),
            "likedCommunityIds": [row[0] for row in db.execute("SELECT id FROM likes WHERE user_id=? ORDER BY rowid", (user_id,))],
        }}

    def enqueue(self, db, user_id, collection_id, asset_id, revision):
        self.asset(db, user_id, asset_id)
        job_id = str(uuid.uuid4())
        stamp = now_ms()
        db.execute("INSERT INTO jobs(id,user_id,collection_id,asset_id,input_revision,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)", (job_id, user_id, collection_id, asset_id, revision, "queued", stamp, stamp))
        if db.execute("UPDATE collections SET job_id=? WHERE user_id=? AND id=?", (job_id, user_id, collection_id)).rowcount != 1:
            raise StoreError(404, "生成任务没有对应的账号收藏。")
        return job_id

    def archive_snapshot(self, user_id):
        with self.transaction() as db:
            state = self.state(db, user_id)
            data = state["data"]
            items = [*data["collections"], *[share["item"] for share in data["shares"]], *data["community"]]
            assets = {item["assetId"]: dict(self.asset(db, user_id, item["assetId"])) for item in items if item.get("assetId")}
            models = {item["modelId"]: dict(self.model(db, user_id, item["modelId"])) for item in items if item.get("modelId") and item.get("generation") == "ready"}
            collections = [dict(row) for row in db.execute("SELECT * FROM collections WHERE user_id=? ORDER BY rowid", (user_id,))]
            jobs = [dict(self.job(db, user_id, row["job_id"])) for row in collections if row["job_id"]]
        return state, assets, models, collections, jobs

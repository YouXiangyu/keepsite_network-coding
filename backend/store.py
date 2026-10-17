from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from pathlib import Path


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def now_ms() -> int:
    return time.time_ns() // 1_000_000


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        for folder in (self.root, self.root / "media", self.root / "models", self.root / "exports"):
            folder.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "memoverse.sqlite3"
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, mime TEXT NOT NULL, filename TEXT NOT NULL,
                    size INTEGER NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collections (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, asset_id TEXT REFERENCES assets(id),
                    version INTEGER NOT NULL DEFAULT 1, input_revision INTEGER NOT NULL DEFAULT 1,
                    job_id TEXT, create_fingerprint TEXT
                );
                CREATE TABLE IF NOT EXISTS shares (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS community (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS likes (id TEXT PRIMARY KEY REFERENCES community(id));
                CREATE TABLE IF NOT EXISTS migrations (id TEXT PRIMARY KEY, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, collection_id TEXT NOT NULL, asset_id TEXT NOT NULL REFERENCES assets(id),
                    input_revision INTEGER NOT NULL, status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, result TEXT,
                    UNIQUE(collection_id, input_revision)
                );
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at);
            """)
            for key, value in {"schema_version": "1", "server_id": str(uuid.uuid4()), "revision": "0", "initialized": "0"}.items():
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
    def meta(db, key):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def bump(db):
        db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")

    def present_item(self, db, row):
        item = json.loads(row["payload"])
        item["version"] = row["version"]
        item.pop("customImage", None)
        item.pop("assetId", None)
        if row["asset_id"]:
            item["assetId"] = row["asset_id"]
            item["customImage"] = f"/api/media/{row['asset_id']}"
        item["generation"] = "reference"
        if row["job_id"]:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
            if job:
                item["generation"] = job["status"]
                item["generationJobId"] = job["id"]
                if job["error"]:
                    item["generationError"] = job["error"]
                if job["status"] == "ready":
                    item["modelUrl"] = f"/api/models/{job['id']}/model.glb"
                    result = json.loads(job["result"] or "{}")
                    if "camera" in result:
                        item["modelCamera"] = result["camera"]
        return item

    def state(self, db):
        initialized = self.meta(db, "initialized") == "1"
        data = None
        if initialized:
            collections = [self.present_item(db, row) for row in db.execute("SELECT * FROM collections ORDER BY rowid")]
            shares = [json.loads(row[0]) for row in db.execute("SELECT payload FROM shares ORDER BY rowid")]
            for share in shares:
                item = share["item"]
                if item.get("assetId"):
                    item["customImage"] = f"/api/media/{item['assetId']}"
            data = {
                "collections": collections,
                "shares": shares,
                "community": [json.loads(row[0]) for row in db.execute("SELECT payload FROM community ORDER BY rowid")],
                "profile": json.loads(self.meta(db, "profile") or '{"name":"小宇","bio":""}'),
                "likedCommunityIds": [row[0] for row in db.execute("SELECT id FROM likes ORDER BY rowid")],
            }
        return {"initialized": initialized, "serverId": self.meta(db, "server_id"), "revision": int(self.meta(db, "revision")), "data": data}

    def enqueue(self, db, collection_id, asset_id, revision):
        job_id = str(uuid.uuid4())
        stamp = now_ms()
        db.execute("INSERT INTO jobs(id,collection_id,asset_id,input_revision,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (job_id, collection_id, asset_id, revision, "queued", stamp, stamp))
        db.execute("UPDATE collections SET job_id=? WHERE id=?", (job_id, collection_id))
        return job_id

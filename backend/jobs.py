from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import tempfile
import threading

from .object_storage import sha_file
from .store import Store, encode, now_ms
from .template import canonical, portable, safe_file, validate_model_outputs

log = logging.getLogger(__name__)


def engine_status():
    try:
        from .relief_engine import engine_status as status
        return status()
    except ImportError:
        return {"available": False, "reason": "AnyImage 运行环境尚未就绪。"}


class JobWorker:
    def __init__(self, store: Store):
        self.store = store
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="memoverse-image-worker", daemon=True)

    def start(self):
        with self.store.transaction(True) as db:
            owners = [row[0] for row in db.execute("SELECT DISTINCT user_id FROM jobs WHERE status='processing'")]
            db.execute("UPDATE jobs SET status='queued',error='',updated_at=? WHERE status='processing'", (now_ms(),))
            for owner in owners:
                self.store.bump(db, owner)
        self.thread.start()

    def wake(self):
        self.wake_event.set()

    def stop(self):
        self.stop_event.set()
        self.wake()
        self.thread.join(timeout=20)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                if self.run_one():
                    continue
            except Exception:
                log.exception("Image worker failed; persistent tasks are retained")
            self.wake_event.wait(10)
            self.wake_event.clear()

    @staticmethod
    def _matches(current, job):
        return bool(current and current["user_id"] == job["user_id"] and current["job_id"] == job["id"]
                    and current["input_revision"] == job["input_revision"] and current["asset_id"] == job["asset_id"])

    def _prepare_model(self, job, asset):
        from .relief_engine import generate_relief
        with tempfile.TemporaryDirectory(prefix="generation-", dir=self.store.root / "tmp") as temporary:
            folder = Path(temporary)
            source = folder / ("original" + Path(asset["object_key"]).suffix)
            self.store.storage.download(asset["object_key"], source, asset["id"])
            if self.stop_event.is_set():
                raise RuntimeError("服务停止，生成任务将在下次启动时恢复。")
            output = folder / "output"
            result = generate_relief(source, output, cancel_event=self.stop_event)
            model_path = safe_file(output, "model.glb")
            if Path(result["model_path"]).resolve() != model_path.resolve():
                raise RuntimeError("生成程序返回了非当前任务的模型文件。")
            depth_path = safe_file(output, "depth.npy")
            geometry_path = safe_file(output, "geometry.json")
            geometry = portable(json.loads(geometry_path.read_text(encoding="utf-8")))
            geometry_path.write_bytes(canonical(geometry) + b"\n")
            validate_model_outputs(model_path, depth_path, geometry_path)
            records = [{"name": path.name, "size": path.stat().st_size, "sha256": sha_file(path)} for path in (model_path, depth_path, geometry_path)]
            model_id = hashlib.sha256(canonical({"assetId": asset["id"], "files": records, "camera": geometry["camera"]})).hexdigest()
            files = []
            for record in records:
                if self.stop_event.is_set():
                    raise RuntimeError("服务停止，生成任务将在下次启动时恢复。")
                key = f"models/{model_id}/{record['name']}"
                mime = "model/gltf-binary" if record["name"].endswith(".glb") else "application/json" if record["name"].endswith(".json") else "application/octet-stream"
                self.store.storage.put_file(key, safe_file(output, record["name"]), record["sha256"], mime)
                files.append({"path": key, "size": record["size"], "sha256": record["sha256"]})
            return {"id": model_id, "assetId": asset["id"], "path": f"models/{model_id}/model.glb",
                    "depthPath": f"models/{model_id}/depth.npy", "geometryPath": f"models/{model_id}/geometry.json",
                    "files": files, "camera": geometry["camera"], "provenance": {"kind": "generated"},
                    "geometry": geometry}

    def run_one(self):
        status = engine_status()
        with self.store.transaction(True) as db:
            if not status["available"]:
                owners = [row[0] for row in db.execute("SELECT DISTINCT user_id FROM jobs WHERE status='queued'")]
                db.execute("UPDATE jobs SET status='blocked',error=?,updated_at=? WHERE status='queued'", (status["reason"], now_ms()))
                for owner in owners:
                    self.store.bump(db, owner)
                return False
            job = db.execute("SELECT * FROM jobs WHERE status IN ('queued','blocked') ORDER BY created_at,id LIMIT 1").fetchone()
            if not job:
                return False
            current = db.execute("SELECT * FROM collections WHERE user_id=? AND id=?", (job["user_id"], job["collection_id"])).fetchone()
            if not self._matches(current, job):
                db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE user_id=? AND id=?", (now_ms(), job["user_id"], job["id"]))
                self.store.bump(db, job["user_id"])
                return True
            asset = dict(self.store.asset(db, job["user_id"], job["asset_id"]))
            db.execute("UPDATE jobs SET status='processing',error='',updated_at=? WHERE user_id=? AND id=?", (now_ms(), job["user_id"], job["id"]))
            self.store.bump(db, job["user_id"])
            job = dict(job)
        model, error = None, ""
        try:
            model = self._prepare_model(job, asset)
        except Exception as exc:
            if not self.stop_event.is_set():
                log.exception("AnyImage job %s failed", job["id"])
            error = str(exc)[-500:] or "生成失败，原图已保留。"
        # All uploads completed before opening this short final transaction.
        with self.store.transaction(True) as db:
            current = db.execute("SELECT * FROM collections WHERE user_id=? AND id=?", (job["user_id"], job["collection_id"])).fetchone()
            if not self._matches(current, job):
                db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE user_id=? AND id=?", (now_ms(), job["user_id"], job["id"]))
            elif error:
                state = "queued" if self.stop_event.is_set() else "failed"
                db.execute("UPDATE jobs SET status=?,error=?,result=NULL,updated_at=? WHERE user_id=? AND id=?", (state, "" if state == "queued" else error, now_ms(), job["user_id"], job["id"]))
                self.store.bump(db, job["user_id"])
            else:
                self.store.register_model(db, model)
                db.execute("INSERT OR IGNORE INTO model_grants VALUES (?,?)", (job["user_id"], model["id"]))
                db.execute("UPDATE jobs SET status='ready',error='',model_id=?,result=?,updated_at=? WHERE user_id=? AND id=?", (model["id"], encode({"modelId": model["id"], "camera": model["camera"]}), now_ms(), job["user_id"], job["id"]))
                db.execute("UPDATE collections SET model_id=? WHERE user_id=? AND id=? AND job_id=? AND input_revision=?", (model["id"], job["user_id"], job["collection_id"], job["id"], job["input_revision"]))
                self.store.bump(db, job["user_id"])
        return True

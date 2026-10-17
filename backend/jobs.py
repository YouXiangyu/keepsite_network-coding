from __future__ import annotations

import json
import logging
import threading

from .store import Store, encode, now_ms

log = logging.getLogger(__name__)


def engine_status():
    try:
        from .relief_engine import engine_status as status
        return status()
    except ImportError:
        return {"available": False, "reason": "AnyImage 本机运行环境尚未就绪。"}


class JobWorker:
    def __init__(self, store: Store):
        self.store = store
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.thread = threading.Thread(target=self.loop, name="memoverse-image-worker", daemon=True)

    def start(self):
        with self.store.transaction(True) as db:
            changed = db.execute("UPDATE jobs SET status='queued', error='', updated_at=? WHERE status='processing'", (now_ms(),)).rowcount
            if changed:
                self.store.bump(db)
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

    def run_one(self):
        status = engine_status()
        with self.store.transaction(True) as db:
            if not status["available"]:
                changed = db.execute("UPDATE jobs SET status='blocked',error=?,updated_at=? WHERE status='queued'", (status["reason"], now_ms())).rowcount
                if changed:
                    self.store.bump(db)
                return False
            job = db.execute("SELECT * FROM jobs WHERE status IN ('queued','blocked') ORDER BY created_at LIMIT 1").fetchone()
            if not job:
                return False
            item = db.execute("SELECT * FROM collections WHERE id=?", (job["collection_id"],)).fetchone()
            if not item or item["job_id"] != job["id"] or item["input_revision"] != job["input_revision"]:
                db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=?", (now_ms(), job["id"]))
                return True
            asset = db.execute("SELECT * FROM assets WHERE id=?", (job["asset_id"],)).fetchone()
            db.execute("UPDATE jobs SET status='processing',error='',updated_at=? WHERE id=?", (now_ms(), job["id"]))
            self.store.bump(db)
        result, error = None, ""
        try:
            from .relief_engine import generate_relief
            output_dir = self.store.root / "models" / job["id"]
            result = generate_relief(self.store.root / "media" / asset["filename"], output_dir, cancel_event=self.stop_event)
            model_path = result["model_path"].resolve()
            if not model_path.is_relative_to(output_dir.resolve()) or model_path.name != "model.glb" or not model_path.is_file():
                raise RuntimeError("生成程序未提供有效的本地模型文件。")
            # The manifest contains portable camera/geometry metadata, never machine paths.
            result = {key: value for key, value in result.items() if not key.endswith("_path")}
            json.dumps(result)
        except Exception as exc:
            if not self.stop_event.is_set():
                log.exception("AnyImage job %s failed", job["id"])
            error = str(exc)[-500:] or "生成失败，原图已保留。"
        with self.store.transaction(True) as db:
            current = db.execute("SELECT * FROM collections WHERE id=?", (job["collection_id"],)).fetchone()
            if not current or current["job_id"] != job["id"] or current["input_revision"] != job["input_revision"]:
                db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=?", (now_ms(), job["id"]))
            elif error and self.stop_event.is_set():
                db.execute("UPDATE jobs SET status='queued',error='',result=NULL,updated_at=? WHERE id=?", (now_ms(), job["id"]))
                self.store.bump(db)
            else:
                db.execute("UPDATE jobs SET status=?,error=?,result=?,updated_at=? WHERE id=?", ("failed" if error else "ready", error, encode(result) if result else None, now_ms(), job["id"]))
                self.store.bump(db)
        return True

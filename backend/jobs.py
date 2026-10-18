from __future__ import annotations

import logging
import threading

from .generation.pipeline import prepare_model
from .generation.repository import claim_next, finalize, recover_interrupted
from .store import Store

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
        recover_interrupted(self.store)
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
        claim = claim_next(self.store, engine_status())
        if claim.task is None:
            return claim.progressed
        job, asset = claim.task.job, claim.task.asset
        model, error = None, ""
        try:
            model = prepare_model(self.store.storage, self.store.root / "tmp", asset, self.stop_event)
        except Exception as exc:
            if not self.stop_event.is_set():
                log.exception("AnyImage job %s failed", job["id"])
            error = str(exc)[-500:] or "生成失败，原图已保留。"
        # All uploads completed before opening this short final transaction.
        finalize(self.store, job, model, error, self.stop_event.is_set)
        return True

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from .generation.cancellation import Cancellation, GenerationCancelled
from .generation.pipeline import prepare_model
from .generation.repository import claim_next, finalize, is_current, recover_interrupted
from .store import Store

log = logging.getLogger(__name__)

PUBLICATION_ATTEMPTS = 3
PUBLICATION_BUDGET_SECONDS = 8


@dataclass
class PendingCompletion:
    job: dict
    model: dict | None
    error: str
    cancellation: Cancellation
    publication_error: str = ""
    retry_requested: bool = False


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
        self.lock = threading.Lock()
        self.active: dict[tuple[str, str], Cancellation] = {}
        self.pending: dict[tuple[str, str], PendingCompletion] = {}
        self.thread = threading.Thread(target=self.loop, name="memoverse-image-worker", daemon=True)

    def start(self):
        recover_interrupted(self.store)
        self.thread.start()

    def wake(self):
        self.wake_event.set()

    def cancel(self, user_id, job_id):
        """Called only after the collection mutation has committed."""
        key = (user_id, job_id)
        with self.lock:
            cancellation = self.active.get(key)
            if cancellation:
                cancellation.cancelled.set()
            completion = self.pending.pop(key, None)
            if completion:
                completion.cancellation.cancelled.set()
        self.wake()

    def recovery_for(self, user_id):
        with self.lock:
            return [{"jobId": item.job["id"], "collectionId": item.job["collection_id"],
                     "error": item.publication_error}
                    for (owner, _), item in self.pending.items()
                    if owner == user_id and item.publication_error]

    def retry_publication(self, user_id, job_id):
        key = (user_id, job_id)
        with self.lock:
            completion = self.pending.get(key)
            if completion is None:
                return False
            if completion.publication_error or key not in self.active:
                completion.retry_requested = True
                completion.publication_error = ""
        self.wake()
        return True

    def has_completion(self, user_id, job_id):
        with self.lock:
            return (user_id, job_id) in self.pending

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
        with self.lock:
            pending = next((item for item in self.pending.values() if item.retry_requested), None)
            if pending:
                pending.retry_requested = False
                key = (pending.job["user_id"], pending.job["id"])
                self.active[key] = pending.cancellation
        if pending:
            try:
                self._publish(pending)
            finally:
                with self.lock:
                    self.active.pop(key, None)
            return True
        claim = claim_next(self.store, engine_status())
        if claim.task is None:
            return claim.progressed
        job, asset = claim.task.job, claim.task.asset
        key = (job["user_id"], job["id"])
        cancellation = Cancellation(self.stop_event)
        with self.lock:
            self.active[key] = cancellation
        model, error = None, ""
        try:
            try:
                if not is_current(self.store, job):
                    return True
                model = prepare_model(self.store.storage, self.store.root / "tmp", asset, cancellation)
            except Exception as exc:
                if not isinstance(exc, GenerationCancelled) and not self.stop_event.is_set():
                    log.exception("AnyImage job %s failed", job["id"])
                error = str(exc)[-500:] or "生成失败，原图已保留。"
            # Only publication is retried; the completed pipeline is never rerun.
            self._publish(PendingCompletion(job, model, error, cancellation))
        finally:
            with self.lock:
                self.active.pop(key, None)
        return True

    def _publish(self, completion: PendingCompletion):
        key = (completion.job["user_id"], completion.job["id"])
        with self.lock:
            if completion.cancellation.cancelled.is_set():
                return
            self.pending[key] = completion
        deadline = time.monotonic() + PUBLICATION_BUDGET_SECONDS
        for attempt in range(PUBLICATION_ATTEMPTS):
            if completion.cancellation.cancelled.is_set():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (attempt and self.stop_event.is_set()):
                break
            try:
                # BEGIN and COMMIT can each wait: reserve half the remaining
                # budget for each, and never use the general 15-second timeout.
                finalize(self.store, completion.job, completion.model, completion.error,
                         self.stop_event.is_set, timeout=min(1, remaining / 2))
            except Exception:
                log.exception("Publication attempt %s failed for job %s", attempt + 1, completion.job["id"])
            else:
                with self.lock:
                    self.pending.pop(key, None)
                return
            if attempt + 1 < PUBLICATION_ATTEMPTS:
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self.stop_event.wait(min(0.25 * (attempt + 1), remaining))
        with self.lock:
            if key in self.pending:
                completion.publication_error = "生成结果尚未写入数据库，自动恢复已暂停。请点击重试恢复保存；不会重复推理或上传。"
        log.error("Publication recovery exhausted for job %s; explicit retry or service restart required", completion.job["id"])

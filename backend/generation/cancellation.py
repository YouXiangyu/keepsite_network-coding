"""Task cancellation is independent from service shutdown."""
from __future__ import annotations

import threading


class GenerationCancelled(RuntimeError):
    pass


class Cancellation:
    def __init__(self, stopping: threading.Event):
        self.stopping = stopping
        self.cancelled = threading.Event()

    def is_set(self):
        return self.cancelled.is_set() or self.stopping.is_set()

    def check(self):
        if self.is_set():
            raise GenerationCancelled("生成已取消。" if self.cancelled.is_set() else "服务停止，生成任务将在下次启动时恢复。")

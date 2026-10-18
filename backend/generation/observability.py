"""Private operational measurements; no identities or business payloads."""
from __future__ import annotations

from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
import uuid

STATUSES = ("queued", "blocked", "processing", "ready", "failed", "cancelled")
OUTCOMES = ("succeeded", "failed", "cancelled", "interrupted")
_logger_lock = threading.Lock()


def emit_event(event):
    """A dedicated INFO sink works with Uvicorn's default root log level."""
    try:
        logger = logging.getLogger("memoverse.generation.metrics")
        with _logger_lock:
            if not logger.handlers:
                handler = logging.StreamHandler()
                handler.setFormatter(logging.Formatter("%(message)s"))
                logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        logger.info(json.dumps(event, ensure_ascii=False, allow_nan=False))
    except Exception:
        # Observability must never prevent publication or change task state.
        pass


class RunObservations:
    def __init__(self, *, emit=None, clock=time.monotonic):
        self.run_id = str(uuid.uuid4())
        self.emit = emit or emit_event
        self.clock = clock
        self.lock = threading.Lock()
        self.totals = dict.fromkeys(OUTCOMES, 0)

    def record(self, event, **fields):
        try:
            self.emit({"event": event, "runId": self.run_id, **fields})
        except Exception:
            pass

    def finished(self, attempt_id, started, outcome):
        duration = round(max(0, self.clock() - started), 6)
        with self.lock:
            self.totals[outcome] += 1
            totals = self.totals.copy()
        self.record("generation_attempt_finished", attemptId=attempt_id,
                    outcome=outcome, pipelineDurationSeconds=duration, totals=totals)


def inspect_queue(data_dir: Path, *, sampled_at_ms=None):
    """Aggregate an existing schema-2 DB, including its live committed WAL."""
    if not data_dir.is_absolute() or not data_dir.is_dir():
        raise ValueError("An existing absolute data directory is required")
    if (data_dir / "RESTORE_INCOMPLETE").exists():
        raise ValueError("Incomplete restoration cannot be inspected")
    database = data_dir / "memoverse.sqlite3"
    if not database.is_file():
        raise ValueError("The business database does not exist")
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        version = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if not version or version[0] != "2":
            raise ValueError("Only schema-2 business databases are supported")
        rows = db.execute("SELECT status,COUNT(*),MIN(created_at) FROM jobs GROUP BY status").fetchall()
        stamp = time.time_ns() // 1_000_000 if sampled_at_ms is None else sampled_at_ms
    counts = dict.fromkeys(STATUSES, 0)
    oldest = []
    for status, count, created in rows:
        if status not in counts:
            raise ValueError("Unknown generation status")
        counts[status] = count
        if status in ("queued", "blocked"):
            oldest.append(created)
    return {"sampledAt": stamp, "counts": counts,
            "waitingDepth": counts["queued"] + counts["blocked"],
            "oldestWaitingJobAgeSeconds": max(0, (stamp - min(oldest)) / 1000) if oldest else None}

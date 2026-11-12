"""Opt-in, account-scoped metadata traces. No payloads or persistent storage."""
from collections import OrderedDict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
import inspect
import asyncio
import json
import math
import re
import threading
import time
import uuid

CAPACITY = 200
MAX_BYTES = 128 * 1024
MAX_EVENT_BYTES = 2048
RETENTION_MS = 600_000
MAX_CAPTURES = 64
MAX_PER_ACCOUNT = 4
MAX_ACTIVE = 32
MAX_LINKS = 256
IDENTIFIER = re.compile(r"[0-9a-f]{32}")
EVENT_PROTOCOLS = {
    "http.request": "http", "sql.commit": "sql", "sql.rollback": "sql",
    "redis.snapshot.read": "redis", "redis.snapshot.write": "redis",
    "redis.stream.publish": "redis", "redis.stream.receive": "redis",
    "redis.stream.ack": "redis", "redis.pubsub.publish": "redis", "redis.pubsub.receive": "redis",
    "ws.ready": "ws", "ws.hint": "ws", "p2p.signaling": "p2p",
    "s3.open": "s3", "s3.read": "s3", "s3.head": "s3", "s3.put": "s3",
    "worker.prepare": "worker", "worker.ocr": "worker", "worker.poster": "worker",
    "worker.publish": "worker", "worker.retry": "worker",
}
STATUSES = {"started", "completed", "failed", "cancelled", "retrying"}
ERROR_CODES = {"operation_failed", "cancelled", "unavailable", "timeout", "retry_scheduled"}
CURRENT = ContextVar("keepsite_trace", default=None)


def identifier(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None and value != "0" * 32


@dataclass(frozen=True)
class TraceRef:
    hub: object
    owner: str
    session_key: str
    capture_id: str
    generation: str
    trace_id: str
    parent_id: str | None = None


class Capture:
    def __init__(self, mono, wall):
        self.generation = uuid.uuid4().hex
        self.started, self.expires = mono, mono + RETENTION_MS
        self.expires_at = wall + RETENTION_MS
        self.events = deque()
        self.size = self.sequence = self.dropped = self.recorded = self.active = 0
        self.cost_ms = 0.0
        self.retired = False


class TraceHub:
    def __init__(self, *, monotonic=None, wall=None):
        self.clock_id = uuid.uuid4().hex
        self.monotonic = monotonic or (lambda: time.monotonic_ns() / 1_000_000)
        self.wall = wall or (lambda: time.time_ns() // 1_000_000)
        self.origin = self.monotonic()
        self.lock = threading.RLock()
        self.captures = OrderedDict()
        self.links = OrderedDict()
        self.instrumented = {"http"}
        self.enabled = True

    def _prune(self, now):
        for key in tuple(self.captures):
            if self.captures[key].expires <= now:
                del self.captures[key]
        for key, (expires, ref) in tuple(self.links.items()):
            if ref is None:
                if expires <= now:
                    del self.links[key]
                continue
            capture = self.captures.get((ref.owner, ref.session_key, ref.capture_id))
            if expires <= now or capture is None or capture.retired or capture.generation != ref.generation:
                del self.links[key]

    def begin(self, owner, session_key, capture_id, trace_id):
        if (not self.enabled or not isinstance(owner, str) or not 1 <= len(owner) <= 512
                or not isinstance(session_key, str) or not 1 <= len(session_key) <= 128
                or not identifier(capture_id) or not identifier(trace_id)):
            return None
        with self.lock:
            now = self.monotonic()
            self._prune(now)
            key = (owner, session_key, capture_id)
            capture = self.captures.get(key)
            if capture is not None and capture.retired:
                return None
            if capture is None:
                owned = sum(key[0] == owner for key in self.captures)
                if len(self.captures) >= MAX_CAPTURES or owned >= MAX_PER_ACCOUNT:
                    return None
                capture = self.captures[key] = Capture(now, self.wall())
            return TraceRef(self, owner, session_key, capture_id, capture.generation, trace_id)

    def _capture(self, ref):
        if not self.enabled:
            return None
        capture = self.captures.get((ref.owner, ref.session_key, ref.capture_id))
        if capture is None or capture.retired or capture.generation != ref.generation or capture.expires <= self.monotonic():
            return None
        return capture

    def slot(self, ref, delta):
        with self.lock:
            capture = self._capture(ref)
            if capture is None:
                return False
            if delta > 0 and capture.active >= MAX_ACTIVE:
                capture.dropped += 1
                return False
            capture.active = max(0, capture.active + delta)
            return True

    def record(self, ref, event, status, *, span_id=None, started=None, duration_ms=0, bytes_count=None,
               attempt=None, error_code=None, coalesced=None):
        before = time.perf_counter_ns()
        if event not in EVENT_PROTOCOLS or status not in STATUSES or not identifier(span_id):
            return
        if not math.isfinite(duration_ms) or duration_ms < 0:
            return
        with self.lock:
            capture = self._capture(ref)
            if capture is None:
                return
            now = self.monotonic()
            capture.sequence += 1
            item = {"sequence": capture.sequence, "timestamp": self.wall(),
                    "offsetMs": max(0, now - self.origin),
                    "durationMs": round(duration_ms, 3), "origin": "server",
                    "protocol": EVENT_PROTOCOLS[event], "event": event, "status": status,
                    "traceId": ref.trace_id, "spanId": span_id, "parentSpanId": ref.parent_id}
            if isinstance(started, (int, float)) and math.isfinite(started) and self.origin <= started <= now:
                item["startOffsetMs"] = started - self.origin
            if type(bytes_count) is int and 0 <= bytes_count <= 2**53 - 1:
                item["bytes"] = bytes_count
            if type(attempt) is int and 0 <= attempt <= 1_000_000:
                item["attempt"] = attempt
            if error_code in ERROR_CODES:
                item["errorCode"] = error_code
            if type(coalesced) is bool:
                item["coalesced"] = coalesced
                if coalesced:
                    item["traceId"] = item["parentSpanId"] = None
            raw = json.dumps(item, separators=(",", ":"), allow_nan=False).encode()
            if len(raw) > MAX_EVENT_BYTES:
                capture.dropped += 1
                return
            while capture.events and (len(capture.events) >= CAPACITY or capture.size + len(raw) > MAX_BYTES):
                _, size = capture.events.popleft()
                capture.size -= size
                capture.dropped += 1
            capture.events.append((item, len(raw)))
            capture.size += len(raw)
            capture.recorded += 1
            capture.cost_ms += (time.perf_counter_ns() - before) / 1_000_000

    def remember(self, kind, key, ref=None):
        ref = ref or CURRENT.get()
        key = link_key(kind, key)
        if ref is None or ref.hub is not self or key is None:
            return
        with self.lock:
            capture = self._capture(ref)
            if capture is None:
                return
            link = (kind, ref.owner, key)
            previous = self.links.get(link)
            if previous is not None:
                old = previous[1]
                if old is None or (old.capture_id, old.trace_id) != (ref.capture_id, ref.trace_id):
                    self.links[link] = (max(previous[0], capture.expires), None)
            else:
                self.links[link] = (capture.expires, ref)
            self.links.move_to_end(link)
            while len(self.links) > MAX_LINKS:
                self.links.popitem(last=False)

    def recall(self, kind, owner, key):
        key = link_key(kind, key)
        if key is None:
            return None
        with self.lock:
            self._prune(self.monotonic())
            item = self.links.get((kind, owner, key))
            return item[1] if item else None

    def recall_job(self, key):
        key = link_key("job", key)
        if key is None:
            return None
        with self.lock:
            self._prune(self.monotonic())
            matches = [value[1] for link, value in self.links.items() if link[0] == "job" and link[2] == key]
            return matches[0] if len(matches) == 1 else None

    def clear(self, owner, session_key, capture_id):
        with self.lock:
            capture = self.captures.get((owner, session_key, capture_id))
            if capture is not None:
                capture.retired = True
                capture.events.clear()
                capture.size = capture.active = 0
            self._prune(self.monotonic())

    def revoke(self, session_key):
        with self.lock:
            for key in tuple(self.captures):
                if key[1] == session_key:
                    self.captures[key].retired = True
                    self.captures[key].events.clear()
                    self.captures[key].size = self.captures[key].active = 0
            self._prune(self.monotonic())

    def close(self):
        with self.lock:
            self.enabled = False
            self.captures.clear()
            self.links.clear()

    def read(self, owner, session_key, capture_id, after=0):
        with self.lock:
            self._prune(self.monotonic())
            capture = self.captures.get((owner, session_key, capture_id))
            if capture is not None and capture.retired:
                capture = None
            return {"v": 1, "captureId": capture_id, "clockId": self.clock_id,
                    "events": [dict(event) for event, _ in capture.events if event["sequence"] > after] if capture else [],
                    "next": capture.sequence if capture else 0, "dropped": capture.dropped if capture else 0,
                    "capacity": CAPACITY, "retentionMs": RETENTION_MS,
                    "expiresAt": capture.expires_at if capture else None,
                    "instrumented": sorted(self.instrumented),
                    "metrics": {"recorded": capture.recorded if capture else 0,
                                "dropped": capture.dropped if capture else 0,
                                "captureCostMs": round(capture.cost_ms, 3) if capture else 0}}


def safe(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception:
        return None


def link_key(kind, key):
    if kind == "cursor" and type(key) is int and 0 <= key <= 2**53 - 1:
        return str(key)
    if kind == "job" and isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", key):
        return key
    return None


@contextmanager
def using(ref):
    token = CURRENT.set(ref)
    try:
        yield
    finally:
        CURRENT.reset(token)


@contextmanager
def span(event, *, ref=None, outcome=None, **fields):
    error_code = fields.get("error_code")
    error_code = error_code if isinstance(error_code, str) and error_code in ERROR_CODES else "operation_failed"
    fields = {key: value for key, value in fields.items() if key in {"bytes_count", "attempt", "coalesced"}}
    ref = ref or CURRENT.get()
    if ref is None or not safe(ref.hub.slot, ref, 1):
        yield None
        return
    started = safe(ref.hub.monotonic)
    if started is None:
        safe(ref.hub.slot, ref, -1)
        yield None
        return
    span_id = uuid.uuid4().hex
    safe(ref.hub.record, ref, event, "started", span_id=span_id, started=started, **fields)
    token = CURRENT.set(replace(ref, parent_id=span_id))
    try:
        yield span_id
    except BaseException as error:
        cancelled = isinstance(error, (asyncio.CancelledError, GeneratorExit))
        safe(ref.hub.record, ref, event, "cancelled" if cancelled else "failed", span_id=span_id, started=started,
             duration_ms=elapsed(ref.hub, started), error_code="cancelled" if cancelled else error_code, **fields)
        raise
    else:
        status = "completed" if outcome is None else safe(outcome)
        if isinstance(status, str) and status in STATUSES:
            safe(ref.hub.record, ref, event, status, span_id=span_id, started=started,
                 duration_ms=elapsed(ref.hub, started), **fields)
    finally:
        CURRENT.reset(token)
        safe(ref.hub.slot, ref, -1)


def elapsed(hub, started):
    now = safe(hub.monotonic)
    return max(0, now - started) if now is not None else 0


def observed(event):
    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def run(*args, **kwargs):
                with span(event):
                    return await function(*args, **kwargs)
        else:
            @wraps(function)
            def run(*args, **kwargs):
                with span(event):
                    return function(*args, **kwargs)
        return run
    return decorate

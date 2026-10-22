"""Owned loopback browser fixtures, never imported by the production application."""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import dataclass
import hashlib
from http.cookies import SimpleCookie, CookieError
import io
import json
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import fakeredis
import uvicorn
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image
from starlette.concurrency import run_in_threadpool

from backend import accounts
from backend.app import create_app
from backend.auth import hashed
from backend.contracts import CreateTicket
from backend.media import persist_image
from backend.object_storage import LocalObjectStore
from backend.services import tickets
from backend.shared_state import RedisSharedState
from backend.tests.account_fixtures import FakeAuthFactory

FIXTURE_HEADER = "p2p-browser-fixture"
SEED = 20261022
MAX_DATASET_BYTES = 96 * 1024 * 1024
MAX_REQUESTS = 10000
MAX_PHASES = 128
PHASE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}\Z")


@dataclass(frozen=True)
class Profile:
    name: str
    port: int
    dist: Path
    count: int
    width: int
    height: int
    prefix: str

    @property
    def origin(self):
        return f"http://127.0.0.1:{self.port}"


P2P_PROFILE = Profile("p2p", 4177, ROOT / "demo/dist/client", 1, 512, 768, "p2p")


def benchmark_profile(count=None):
    value = os.environ.get("MEMOVERSE_M6_TICKET_COUNT", "300") if count is None else str(count)
    if not re.fullmatch(r"[1-9][0-9]{0,2}", value) or not 1 <= int(value) <= 300:
        raise ValueError("M6 ticket count must be an integer from 1 to 300")
    return Profile("benchmark", 4178, ROOT / ".cache/m6-bench-client", int(value), 256, 384, "m6")


def safe_directory(path):
    path = Path(path).absolute()
    if os.name == "nt" and path.drive.upper() != "D:":
        raise ValueError("Fixture files must use D:")
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise ValueError("Fixture paths cannot traverse links")
    return path.resolve()


def route_category(path):
    if path in {"/api/sync/snapshot", "/api/sync/changes", "/api/auth/session", "/api/auth/logout", "/api/tickets", "/api/media"}:
        return path
    if re.fullmatch(r"/api/media/[a-f0-9]{64}/download", path):
        return "/api/media/:assetId/download"
    if re.fullmatch(r"/api/media/[a-f0-9]{64}", path):
        return "/api/media/:assetId"
    if path.startswith("/api/operations/"):
        return "/api/operations/:operationId"
    if path.startswith("/api/tickets/"):
        return "/api/tickets/:ticketId"
    return "other-api" if path.startswith("/api/") else "static"


class PhaseMetrics:
    """Complete bounded samples; overflow is an explicit failed measurement."""
    def __init__(self, *, maximum=MAX_REQUESTS):
        self.maximum = maximum
        self._lock = threading.Lock()
        self._active = None
        self._phases = {}
        self._total = 0

    def start(self, phase_id):
        with self._lock:
            if self._active is not None or any(phase["inflight"] for phase in self._phases.values()):
                raise HTTPException(409, "Previous phase still has active requests")
            if phase_id in self._phases or len(self._phases) >= MAX_PHASES:
                raise HTTPException(409, "Phase identity unavailable")
            self._phases[phase_id] = {"requests": [], "inflight": 0, "overflow": False, "closed": False}
            self._active = phase_id
        return {"phaseId": phase_id, "started": True}

    def begin(self, *, label, method, path):
        with self._lock:
            if self._active is None:
                return None
            phase = self._phases[self._active]
            if self._total >= self.maximum:
                phase["overflow"] = True
                return False
            self._total += 1
            phase["inflight"] += 1
            row = {"phaseId": self._active, "label": label, "method": method,
                   "route": route_category(path), "status": None, "bodyBytes": 0,
                   "durationMs": None, "startedAt": time.time_ns() / 1_000_000,
                   "finishedAt": None, "complete": False}
            phase["requests"].append(row)
            return row, time.perf_counter_ns()

    def finish(self, measurement, *, status, body_bytes, complete):
        row, started = measurement
        duration = (time.perf_counter_ns() - started) / 1_000_000
        with self._lock:
            if row["finishedAt"] is not None:
                return
            row.update(status=status, bodyBytes=body_bytes, durationMs=duration,
                       finishedAt=row["startedAt"] + duration, complete=complete)
            self._phases[row["phaseId"]]["inflight"] -= 1

    async def stop(self, phase_id, *, timeout=5):
        with self._lock:
            phase = self._phases.get(phase_id)
            if phase is None:
                raise HTTPException(404, "Unknown phase")
            if self._active not in (None, phase_id):
                raise HTTPException(409, "Another phase is active")
            self._active = None
            phase["closed"] = True
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                pending = phase["inflight"]
            if not pending or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.01)
        with self._lock:
            rows = [dict(row) for row in phase["requests"]]
            return {"phaseId": phase_id, "requests": rows, "overflow": phase["overflow"],
                    "incomplete": bool(phase["inflight"]) or any(not row["complete"] for row in rows)}

    def status(self):
        with self._lock:
            return {"activePhase": self._active, "recordedRequests": self._total,
                    "phases": [{"phaseId": key, "requests": len(value["requests"]),
                                "inflight": value["inflight"], "overflow": value["overflow"], "closed": value["closed"]}
                               for key, value in self._phases.items()]}


class MeasureHTTP:
    """Count accepted ASGI response chunks, including streaming final-body completion."""
    def __init__(self, app, metrics, auth):
        self.app, self.metrics, self.auth = app, metrics, auth

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] != "http" or path.startswith("/__test/") or path == "/api/health":
            return await self.app(scope, receive, send)
        label = "anonymous"
        for key, value in scope.get("headers", []):
            if key.lower() == b"cookie":
                try:
                    cookies = SimpleCookie()
                    cookies.load(value.decode("latin-1"))
                    cookie = cookies.get(self.auth.cookie_name)
                    if cookie:
                        label = self.auth.labels.get(cookie.value, "unknown")
                except (CookieError, UnicodeError):
                    pass
                break
        measurement = self.metrics.begin(label=label, method=scope["method"], path=path)
        if measurement is False:
            return await JSONResponse({"detail": "Fixture measurement sample limit exceeded"}, status_code=503)(scope, receive, send)
        if measurement is None:
            return await self.app(scope, receive, send)
        status, body_bytes, completed = None, 0, False
        async def measured_send(message):
            nonlocal status, body_bytes, completed
            await send(message)
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                body_bytes += len(message.get("body", b""))
                if not message.get("more_body", False):
                    completed = True
                    self.metrics.finish(measurement, status=status, body_bytes=body_bytes, complete=True)
        try:
            await self.app(scope, receive, measured_send)
        finally:
            if not completed:
                self.metrics.finish(measurement, status=status, body_bytes=body_bytes, complete=False)


class LoopbackAuth(FakeAuthFactory):
    def __init__(self, run_token, profile):
        super().__init__(origin=profile.origin)
        self.run_token, self.profile = run_token, profile
        self.sessions.clear()
        self.labels, self.requests = {}, []

    @staticmethod
    def response(user):
        return {"authenticated": bool(user), "configured": True,
                "providers": {"email": True, "google": False},
                **({"user": user, "csrfToken": "test-csrf"} if user else {})}

    def __call__(self, app, _data_root, *, on_authenticated):
        self._initialize = on_authenticated
        @app.middleware("http")
        async def authenticate(request, call_next):
            path = request.url.path
            if path.startswith("/__test/"):
                if (request.client.host != "127.0.0.1"
                        or request.headers.get("X-MemoVerse-Fixture") != FIXTURE_HEADER
                        or request.headers.get("X-MemoVerse-Fixture-Run") != self.run_token
                        or request.headers.get("Origin", self.origin) != self.origin):
                    return JSONResponse({"detail": "Fixture access denied"}, status_code=403)
                return await call_next(request)
            if not path.startswith("/api/") or path == "/api/health":
                return await call_next(request)
            sid = request.cookies.get(self.cookie_name)
            user = self.sessions.get(sid)
            if path == "/api/auth/session":
                return JSONResponse(self.response(user))
            if not user:
                return JSONResponse({"detail": "请先登录。"}, status_code=401)
            if request.method in {"POST", "PATCH", "PUT", "DELETE"} and (
                    request.headers.get("Origin") != self.origin
                    or request.headers.get("X-CSRF-Token") != "test-csrf"):
                return JSONResponse({"detail": "请求校验失败。"}, status_code=403)
            if path == "/api/auth/logout" and request.method == "POST":
                self.sessions.pop(sid, None)
                with self._lock:
                    callbacks = tuple(self._revocations)
                for callback in callbacks:
                    callback(hashed(sid))
                response = JSONResponse({"ok": True})
                response.delete_cookie(self.cookie_name)
                return response
            await run_in_threadpool(on_authenticated, user)
            request.state.user = user.copy()
            response = await call_next(request)
            if self.profile.name == "p2p":
                self.requests.append({"label": self.labels.get(sid, "unknown"), "path": path,
                                      "method": request.method, "status": response.status_code})
                self.requests[:] = self.requests[-2000:]
            return response
        return self


def create_dataset(storage, profile):
    generator = random.Random(SEED)
    items, total, seen = [], 0, set()
    for index in range(profile.count):
        picture = Image.frombytes("RGB", (profile.width, profile.height), generator.randbytes(profile.width * profile.height * 3))
        with io.BytesIO() as stream:
            picture.save(stream, "PNG")
            raw = stream.getvalue()
        picture.close()
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen:
            raise RuntimeError("Synthetic images must be unique")
        seen.add(digest)
        total += len(raw)
        if total > MAX_DATASET_BYTES:
            raise RuntimeError("Synthetic dataset exceeds the 96 MiB budget")
        key = f"media/{digest}.png"
        storage.put_bytes(key, raw, digest, "image/png")
        items.append({"ticketId": "fixture-ticket" if profile.name == "p2p" else f"fixture-ticket-{index:03d}",
                      "media": {"assetId": digest, "sha256": digest, "size": len(raw), "mimeType": "image/png",
                                "width": profile.width, "height": profile.height}, "objectKey": key})
    return items, {"seed": SEED, "count": profile.count, "width": profile.width,
                   "height": profile.height, "totalBytes": total}


def application(directory, run_token, profile=P2P_PROFILE):
    directory = safe_directory(directory)
    if not re.fullmatch(r"[a-f0-9]{32}", run_token) or (directory / "data").exists() or (directory / "objects").exists():
        raise ValueError("Fixture requires an owned new data directory and run token")
    auth = LoopbackAuth(run_token, profile)
    client = fakeredis.FakeRedis()
    storage = LocalObjectStore(directory / "objects")
    try:
        app = create_app(directory / "data", auth_provider=auth, object_store=storage,
                         shared_state=RedisSharedState(client=client), run_recovery=False, run_processing=False)
        items, dataset = create_dataset(storage, profile)
    except BaseException:
        client.close()
        storage.close()
        raise
    metrics = PhaseMetrics()
    app.state.fixture_metrics = metrics
    app.state.fixture_auth = auth
    app.state.fixture_dataset = dataset
    seeded, media, seed_lock = set(), {}, threading.Lock()

    @app.post("/__test/session")
    def session(request: Request, body: dict):
        group, who, label = body.get("group"), body.get("who", "alice"), body.get("label")
        if (set(body) - {"group", "who", "label"} or not isinstance(group, str) or not re.fullmatch(r"[a-z-]{1,30}", group)
                or who not in ("alice", "bob") or not isinstance(label, str) or not re.fullmatch(r"[a-z-]{1,50}", label)):
            raise HTTPException(400, "Invalid fixture session")
        user_id = f"fixture-{group}-{who}"
        user = {"id": user_id, "email": f"{user_id}@example.test", "name": who}
        with seed_lock:
            if len(auth.sessions) >= 64:
                raise HTTPException(400, "Fixture session limit reached")
            accounts.ensure_account(app.state.store, user_id, user["email"], who)
            if who == "alice" and user_id not in seeded:
                for index, item in enumerate(items):
                    with closing(storage.open(item["objectKey"])) as incoming:
                        raw = incoming.read(item["media"]["size"] + 1)
                    asset = persist_image(app.state.store, user_id, "fixture-upload" if profile.name == "p2p" else f"fixture-upload-{index:03d}", raw)
                    ticket_id = item["ticketId"]
                    tickets.apply(app.state.store, user_id, ticket_id, "create", CreateTicket(
                        id=ticket_id, operationId="fixture-create" if profile.name == "p2p" else f"fixture-create-{index:03d}",
                        originalAssetId=asset["assetId"], fields={"title": "合成图片传输测试" if profile.name == "p2p" else f"合成电影票 {index + 1:03d}"}))
                    media[user_id] = asset
                seeded.add(user_id)
            sid = "fixture-" + uuid.uuid4().hex
            auth.sessions[sid], auth.labels[sid] = user, label
        response = JSONResponse({"userId": user_id, **({"media": media.get(user_id)} if profile.name == "p2p" else {})})
        response.set_cookie(auth.cookie_name, sid, httponly=True, samesite="lax")
        return response

    @app.get("/__test/status")
    def status():
        return {"requests": auth.requests} if profile.name == "p2p" else metrics.status()

    if profile.name == "benchmark":
        @app.get("/__test/manifest")
        def manifest(request: Request):
            sid = request.cookies.get(auth.cookie_name)
            user = auth.sessions.get(sid)
            if user is None:
                raise HTTPException(401, "Fixture account login required")
            if user["id"] not in seeded:
                raise HTTPException(404, "No dataset for this account")
            with app.state.store.transaction() as db:
                for item in items:
                    app.state.store.referenced_asset(db, user["id"], item["media"]["assetId"])
            return {"userId": user["id"], "items": [{"ticketId": item["ticketId"], "media": item["media"]} for item in items], "dataset": dataset}

        @app.post("/__test/phase")
        async def phase(body: dict):
            if set(body) != {"phaseId", "action"} or not isinstance(body["phaseId"], str) or not PHASE_ID.fullmatch(body["phaseId"]):
                raise HTTPException(400, "Invalid phase")
            if body["action"] == "start":
                return metrics.start(body["phaseId"])
            if body["action"] == "stop":
                return await metrics.stop(body["phaseId"])
            raise HTTPException(400, "Invalid phase action")
        app.add_middleware(MeasureHTTP, metrics=metrics, auth=auth)

    @app.post("/__test/shutdown")
    def shutdown():
        app.state.fixture_shutdown()
        return {"ok": True, "runId": directory.name}

    @app.get("/{path:path}")
    def static(path: str):
        if path.startswith(("api/", "__test/")):
            raise HTTPException(404)
        dist = safe_directory(profile.dist)
        target = safe_directory(dist / path)
        if not target.is_relative_to(dist):
            raise HTTPException(404)
        return FileResponse(target if target.is_file() else dist / "index.html")
    return app, client


def run_fixture(profile):
    dist = safe_directory(profile.dist)
    if not (dist / "index.html").is_file():
        raise SystemExit("Build the matching browser client before starting its fixture")
    cache = safe_directory(ROOT / ".cache")
    cache.mkdir(exist_ok=True)
    run_token = os.environ.get("MEMOVERSE_P2P_RUN_TOKEN", "")
    if not re.fullmatch(r"[a-f0-9]{32}", run_token):
        raise SystemExit("Launch with a random MEMOVERSE_P2P_RUN_TOKEN ownership key")
    os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
    manifest = cache / f"{profile.prefix}-run-{run_token}.json"
    owns_manifest, directory = False, None
    try:
        with tempfile.TemporaryDirectory(prefix=f"{profile.prefix}-browser-", dir=cache) as temporary:
            directory = safe_directory(temporary)
            if directory.parent != cache or not re.fullmatch(re.escape(profile.prefix) + r"-browser-[a-z0-9_]{8}", directory.name):
                raise SystemExit("Unexpected fixture directory ownership")
            with manifest.open("x", encoding="utf-8") as output:
                json.dump({"runId": directory.name}, output)
            owns_manifest = True
            app, client = application(directory, run_token, profile)
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=profile.port,
                                    ws="websockets-sansio", ws_max_size=65536,
                                    ws_ping_interval=15, ws_ping_timeout=15, ws_per_message_deflate=False,
                                    log_level="warning", access_log=False, timeout_graceful_shutdown=5))
            app.state.fixture_shutdown = lambda: setattr(server, "should_exit", True)
            try:
                server.run()
            finally:
                client.close()
    finally:
        if owns_manifest and directory is not None and not directory.exists():
            manifest.unlink(missing_ok=True)

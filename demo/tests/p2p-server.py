"""Explicit loopback-only browser fixture: real HTTP/WS/SQL, fake Redis, local objects.

Never imported by the production entry. Every run owns a temporary D:-workspace
directory and synthetic images; it cannot read real account data or contact S3.
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[2]
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

ORIGIN = "http://127.0.0.1:4177"
FIXTURE_HEADER = "p2p-browser-fixture"
DIST = ROOT / "demo/dist/client"


class LoopbackAuth(FakeAuthFactory):
    def __init__(self, run_token):
        super().__init__(origin=ORIGIN)
        self.run_token = run_token
        self.sessions.clear()
        self.labels = {}
        self.requests = []

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
                        or request.headers.get("Origin", ORIGIN) != ORIGIN):
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
                    request.headers.get("Origin") != ORIGIN
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
            self.requests.append({"label": self.labels.get(sid, "unknown"), "path": path,
                                  "method": request.method, "status": response.status_code})
            self.requests[:] = self.requests[-2000:]
            return response
        return self


def application(directory, run_token):
    auth = LoopbackAuth(run_token)
    client = fakeredis.FakeRedis()
    app = create_app(directory / "data", auth_provider=auth,
                     object_store=LocalObjectStore(directory / "objects"),
                     shared_state=RedisSharedState(client=client),
                     run_recovery=False, run_processing=False)
    stream = io.BytesIO()
    # Seeded random pixels give a valid ~1.2 MiB PNG with enough chunks to test
    # backpressure/interruption. This is generated data, never a user's photo.
    Image.frombytes("RGB", (512, 768), random.Random(20261022).randbytes(512 * 768 * 3)).save(stream, "PNG")
    raw = stream.getvalue()
    seeded = set()
    media = {}

    @app.post("/__test/session")
    def session(request: Request, body: dict):
        group, who, label = body.get("group"), body.get("who", "alice"), body.get("label")
        if (not isinstance(group, str) or not re.fullmatch(r"[a-z-]{1,30}", group)
                or who not in ("alice", "bob") or not isinstance(label, str)
                or not re.fullmatch(r"[a-z-]{1,50}", label) or len(auth.sessions) >= 64):
            raise HTTPException(400, "Invalid fixture session")
        user_id = f"fixture-{group}-{who}"
        user = {"id": user_id, "email": f"{user_id}@example.test", "name": who}
        accounts.ensure_account(app.state.store, user_id, user["email"], who)
        if who == "alice" and user_id not in seeded:
            asset = persist_image(app.state.store, user_id, "fixture-upload", raw)
            tickets.apply(app.state.store, user_id, "fixture-ticket", "create", CreateTicket(
                id="fixture-ticket", operationId="fixture-create", originalAssetId=asset["assetId"],
                fields={"title": "合成图片传输测试"}))
            media[user_id] = asset
            seeded.add(user_id)
        sid = "fixture-" + uuid.uuid4().hex
        auth.sessions[sid], auth.labels[sid] = user, label
        response = JSONResponse({"userId": user_id, "media": media.get(user_id)})
        response.set_cookie(auth.cookie_name, sid, httponly=True, samesite="lax")
        return response

    @app.get("/__test/status")
    def status():
        return {"requests": auth.requests}

    @app.post("/__test/shutdown")
    def shutdown():
        app.state.fixture_shutdown()
        return {"ok": True, "runId": directory.name}

    @app.get("/{path:path}")
    def static(path: str):
        if path.startswith(("api/", "__test/")):
            raise HTTPException(404)
        target = (DIST / path).resolve()
        if not target.is_relative_to(DIST.resolve()):
            raise HTTPException(404)
        return FileResponse(target if target.is_file() else DIST / "index.html")

    return app, client


if __name__ == "__main__":
    if not (DIST / "index.html").is_file():
        raise SystemExit("Build demo before running the P2P browser fixture")
    cache = ROOT / ".cache"
    if cache.is_symlink() or cache.is_junction() or cache.resolve() != cache:
        raise SystemExit("Fixture cache must be a normal directory inside this workspace")
    cache.mkdir(exist_ok=True)
    run_token = os.environ.get("MEMOVERSE_P2P_RUN_TOKEN", "")
    if not re.fullmatch(r"[a-f0-9]{32}", run_token):
        raise SystemExit("Launch the fixture through playwright.p2p.config.ts")
    manifest = cache / f"p2p-run-{run_token}.json"
    owns_manifest, directory = False, None
    try:
        with tempfile.TemporaryDirectory(prefix="p2p-browser-", dir=cache) as temporary:
            directory = Path(temporary).resolve()
            if not directory.is_relative_to(cache.resolve()):
                raise SystemExit("Fixture directory escaped the owned workspace cache")
            with manifest.open("x", encoding="utf-8") as output:
                json.dump({"runId": directory.name}, output)
            owns_manifest = True
            app, client = application(directory, run_token)
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=4177,
                                    ws="websockets-sansio", ws_max_size=65536,
                                    ws_ping_interval=15, ws_ping_timeout=15,
                                    ws_per_message_deflate=False, log_level="warning",
                                    access_log=False, timeout_graceful_shutdown=5))
            app.state.fixture_shutdown = lambda: setattr(server, "should_exit", True)
            try:
                server.run()
            finally:
                client.close()
    finally:
        if owns_manifest and directory is not None and not directory.exists():
            manifest.unlink(missing_ok=True)

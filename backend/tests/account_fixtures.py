"""Named authentication and small image fixtures, only for tests."""
from __future__ import annotations

import io
import threading
import time

from PIL import Image
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from backend.auth import AuthError, hashed



USERS = {
    "alice": {"id": "user-alice", "email": "alice@example.test", "name": "Alice"},
    "bob": {"id": "user-bob", "email": "bob@example.test", "name": "Bob"},
}
ORIGIN = "https://testserver"


class FakeAuthFactory:
    """Explicit dependency injection, never imported by production code."""

    def __init__(self):
        self.sessions = {f"session-{name}": user.copy() for name, user in USERS.items()}
        self.cookie_name = "test_session"
        self._revocations = set()
        self._lock = threading.Lock()
        self._initialize = None

    def __call__(self, app, data_root, *, on_authenticated):
        self._initialize = on_authenticated
        @app.middleware("http")
        async def authenticate(request, call_next):
            if request.url.path == "/api/health":
                return await call_next(request)
            token = request.cookies.get("test_session")
            user = self.sessions.get(token)
            if not user:
                return JSONResponse({"detail": "请先登录。"}, status_code=401)
            if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
                if request.headers.get("X-CSRF-Token") != "test-csrf" or request.headers.get("Origin") != ORIGIN:
                    return JSONResponse({"detail": "请求校验失败。"}, status_code=403)
            await run_in_threadpool(on_authenticated, user)
            request.state.user = user.copy()
            return await call_next(request)
        return self

    def revoke(self, name):
        sid = f"session-{name}"
        self.sessions.pop(sid, None)
        with self._lock:
            callbacks = tuple(self._revocations)
        for callback in callbacks:
            callback(hashed(sid))

    def websocket_session(self, sid, origin):
        if origin != ORIGIN:
            raise AuthError(403, "请求来源验证失败。", "invalid_origin")
        user = self.sessions.get(sid)
        if user is None:
            raise AuthError(401, "请先登录。", "authentication_required")
        if self._initialize:
            self._initialize(user)
        return {"user": user.copy(), "sessionKey": hashed(sid), "expiresAt": int(time.time() * 1000) + 60_000}

    def subscribe_revocations(self, callback):
        with self._lock:
            self._revocations.add(callback)

        def unsubscribe():
            with self._lock:
                self._revocations.discard(callback)
        return unsubscribe


def signed_in(client, name="alice"):
    client.cookies.set("test_session", f"session-{name}")
    return client


WRITE_HEADERS = {"X-CSRF-Token": "test-csrf", "X-MemoVerse-Client": "web", "Origin": ORIGIN}


def image_bytes(color="red", format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (9, 7), color).save(output, format=format)
    return output.getvalue()


def account_snapshot(client):
    """Inspect business state through all validated pages of the new protocol."""
    response = client.get("/api/sync/snapshot")
    assert response.status_code == 200, response.text
    first = response.json()
    items, page = [], first
    stable = ("serverId", "userId", "snapshotId", "cursor", "total", "expiresAt", "capabilities")
    while True:
        assert all(page[key] == first[key] for key in stable)
        assert page["offset"] == len(items)
        items.extend(page["tickets"])
        if page["nextPageToken"] is None:
            break
        response = client.get("/api/sync/snapshot", params={"pageToken": page["nextPageToken"]})
        assert response.status_code == 200, response.text
        page = response.json()
    assert len(items) == first["total"]
    return {"serverId": first["serverId"], "userId": first["userId"], "cursor": first["cursor"],
            "capabilities": first["capabilities"], "tickets": items}

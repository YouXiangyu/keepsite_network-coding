"""Named authentication and small image fixtures, only for tests."""
from __future__ import annotations

import io

from PIL import Image
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool



USERS = {
    "alice": {"id": "user-alice", "email": "alice@example.test", "name": "Alice"},
    "bob": {"id": "user-bob", "email": "bob@example.test", "name": "Bob"},
}
ORIGIN = "https://testserver"


class FakeAuthFactory:
    """Explicit dependency injection, never imported by production code."""

    def __init__(self):
        self.sessions = {f"session-{name}": user.copy() for name, user in USERS.items()}

    def __call__(self, app, data_root, *, on_authenticated):
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
        self.sessions.pop(f"session-{name}", None)


def signed_in(client, name="alice"):
    client.cookies.set("test_session", f"session-{name}")
    return client


WRITE_HEADERS = {"X-CSRF-Token": "test-csrf", "X-MemoVerse-Client": "web", "Origin": ORIGIN}


def image_bytes(color="red", format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (9, 7), color).save(output, format=format)
    return output.getvalue()

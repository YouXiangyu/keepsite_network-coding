"""Authenticated diagnostic access and complete HTTP response observation."""
from dataclasses import replace
import uuid

from fastapi import Header, Query, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders

from .diagnostics import elapsed, identifier, safe, using


class DiagnosticsMiddleware:
    def __init__(self, app, hub):
        self.app, self.hub = app, hub

    async def __call__(self, scope, receive, send):
        state = scope.get("state", {})
        user = state.get("user")
        path = scope.get("path", "")
        if (scope["type"] != "http" or not path.startswith("/api/")
                or path.startswith(("/api/diagnostics/", "/api/auth/")) or not isinstance(user, dict)):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        ref = safe(self.hub.begin, user.get("id"), state.get("session_key"),
                   headers.get("x-keepsite-diagnostics"), headers.get("x-keepsite-trace-id"))
        if ref is None or not safe(self.hub.slot, ref, 1):
            await self.app(scope, receive, send)
            return
        started = safe(self.hub.monotonic)
        if started is None:
            safe(self.hub.slot, ref, -1)
            await self.app(scope, receive, send)
            return
        span_id, size, status, finished = uuid.uuid4().hex, 0, None, False
        safe(self.hub.record, ref, "http.request", "started", span_id=span_id, started=started)

        async def observed_send(message):
            nonlocal size, status, finished
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Keepsite-Trace-Id"] = ref.trace_id
            await send(message)
            if message["type"] == "http.response.body":
                size += len(message.get("body", b""))
                finished = not message.get("more_body", False)

        try:
            with using(replace(ref, parent_id=span_id)):
                await self.app(scope, receive, observed_send)
        finally:
            safe(self.hub.record, ref, "http.request", "completed" if finished and status is not None and status < 400 else "failed",
                 span_id=span_id, started=started, duration_ms=elapsed(self.hub, started), bytes_count=size)
            safe(self.hub.slot, ref, -1)


def install_diagnostics(app, hub):
    app.add_middleware(DiagnosticsMiddleware, hub=hub)

    def scope_for(request, capture_id):
        user = getattr(request.state, "user", None)
        session_key = getattr(request.state, "session_key", None)
        if not isinstance(user, dict) or not isinstance(user.get("id"), str) or not session_key:
            return None, JSONResponse({"detail": "请先登录。"}, status_code=401)
        if not identifier(capture_id):
            return None, JSONResponse({"detail": "诊断会话无效。"}, status_code=422)
        return (user["id"], session_key, capture_id), None

    @app.get("/api/diagnostics/events")
    def events(request: Request, after: int = Query(0, ge=0, le=2**53 - 1),
               x_keepsite_diagnostics: str | None = Header(None)):
        key, error = scope_for(request, x_keepsite_diagnostics)
        if error is not None:
            return error
        result = safe(hub.read, *key, after)
        return result if result is not None else JSONResponse({"detail": "诊断暂时不可用。"}, status_code=503)

    @app.delete("/api/diagnostics/events")
    def clear(request: Request, x_keepsite_diagnostics: str | None = Header(None)):
        key, error = scope_for(request, x_keepsite_diagnostics)
        if error is not None:
            return error
        safe(hub.clear, *key)
        return {"cleared": True}

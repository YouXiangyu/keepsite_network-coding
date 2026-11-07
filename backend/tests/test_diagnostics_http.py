"""Real ASGI/auth boundaries with isolated fixture accounts and storage."""
import json
import uuid

import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from account_fixtures import ORIGIN, USERS, WRITE_HEADERS, signed_in
from test_api import make_app


def headers():
    return {"X-Keepsite-Diagnostics": uuid.uuid4().hex, "X-Keepsite-Trace-Id": uuid.uuid4().hex}


@pytest.fixture
def workspace(tmp_path):
    app = make_app(tmp_path)
    @app.get("/api/stream-fixture")
    def stream():
        def content():
            yield b"private-"
            yield b"payload"
        return StreamingResponse(content())
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        yield client, app


def test_default_off_and_explicit_authenticated_opt_in(workspace):
    client, app = workspace
    assert client.get("/api/sync/snapshot").status_code == 200
    assert not app.state.diagnostics.captures
    request_headers = headers()
    response = client.get("/api/sync/snapshot", headers=request_headers)
    assert response.status_code == 200
    assert response.headers["X-Keepsite-Trace-Id"] == request_headers["X-Keepsite-Trace-Id"]
    exported = client.get("/api/diagnostics/events", headers=request_headers).json()
    assert [event["status"] for event in exported["events"]] == ["started", "completed"]
    assert {event["traceId"] for event in exported["events"]} == {request_headers["X-Keepsite-Trace-Id"]}
    assert "user-alice" not in json.dumps(exported)


def test_anonymous_and_other_account_or_session_cannot_read_or_clear(workspace):
    client, app = workspace
    request_headers = headers()
    client.get("/api/sync/snapshot", headers=request_headers)
    signed_in(client, "bob")
    assert client.get("/api/diagnostics/events", headers=request_headers).json()["events"] == []
    assert client.delete("/api/diagnostics/events", headers={**WRITE_HEADERS, **request_headers}).status_code == 200
    app.state.auth.sessions["session-alternate"] = USERS["alice"].copy()
    client.cookies.set("test_session", "session-alternate")
    assert client.get("/api/diagnostics/events", headers=request_headers).json()["events"] == []
    signed_in(client)
    assert len(client.get("/api/diagnostics/events", headers=request_headers).json()["events"]) == 2
    client.cookies.clear()
    assert client.get("/api/diagnostics/events", headers=request_headers).status_code == 401


def test_clear_requires_csrf_and_retired_capture_rejects_late_request(workspace):
    client, _ = workspace
    request_headers = headers()
    client.get("/api/sync/snapshot", headers=request_headers)
    assert client.delete("/api/diagnostics/events", headers=request_headers).status_code == 403
    assert client.delete("/api/diagnostics/events", headers={**WRITE_HEADERS, **request_headers}).status_code == 200
    result = client.get("/api/sync/snapshot", headers=request_headers)
    assert result.status_code == 200 and "X-Keepsite-Trace-Id" not in result.headers
    assert client.get("/api/diagnostics/events", headers=request_headers).json()["events"] == []


def test_http_span_finishes_after_all_stream_bytes_without_recording_body(workspace):
    client, _ = workspace
    request_headers = headers()
    response = client.get("/api/stream-fixture", headers=request_headers)
    assert response.content == b"private-payload"
    result = client.get("/api/diagnostics/events", headers=request_headers).json()
    completed = result["events"][-1]
    assert completed["status"] == "completed" and completed["bytes"] == len(response.content)
    assert completed["durationMs"] >= 0
    assert "private-payload" not in json.dumps(result)
    assert "stream-fixture" not in json.dumps(result)


def test_http_error_status_is_failure_not_success(workspace):
    client, _ = workspace
    request_headers = headers()
    response = client.post("/api/tickets", headers={**request_headers, **WRITE_HEADERS}, json={"secret": "never-export"})
    assert response.status_code == 422
    result = client.get("/api/diagnostics/events", headers=request_headers).json()
    assert result["events"][-1]["status"] == "failed"
    assert "never-export" not in json.dumps(result)


def test_diagnostic_observer_failure_cannot_fail_business_request(workspace, monkeypatch):
    client, app = workspace
    def broken(*args, **kwargs):
        raise RuntimeError("observer only")
    monkeypatch.setattr(app.state.diagnostics, "record", broken)
    assert client.get("/api/sync/snapshot", headers=headers()).status_code == 200
    monkeypatch.setattr(app.state.diagnostics, "begin", broken)
    assert client.get("/api/sync/snapshot", headers=headers()).status_code == 200


def test_revocation_clears_server_capture_immediately(workspace):
    client, app = workspace
    request_headers = headers()
    client.get("/api/sync/snapshot", headers=request_headers)
    app.state.auth.revoke("alice")
    assert client.get("/api/diagnostics/events", headers=request_headers).status_code == 401
    assert all(capture.retired and not capture.events for capture in app.state.diagnostics.captures.values())


def test_stream_interruption_keeps_original_error_and_only_actual_sent_bytes():
    import asyncio
    from backend.diagnostics import TraceHub
    from backend.diagnostics_http import DiagnosticsMiddleware
    hub, error = TraceHub(), ValueError("private original stream error")
    request_headers = headers()
    async def broken(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
        raise error
    async def unused():
        return {}
    async def delivered(message):
        pass
    scope = {"type": "http", "path": "/api/media/fixture", "state": {"user": {"id": "a"}, "session_key": "s"},
             "headers": [(key.lower().encode(), value.encode()) for key, value in request_headers.items()]}
    with pytest.raises(ValueError) as caught:
        asyncio.run(DiagnosticsMiddleware(broken, hub)(scope, unused, delivered))
    assert caught.value is error
    events = hub.read("a", "s", request_headers["X-Keepsite-Diagnostics"])["events"]
    assert events[-1]["status"] == "failed" and events[-1]["bytes"] == 7
    assert "private original" not in json.dumps(events)

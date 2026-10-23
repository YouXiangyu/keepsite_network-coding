"""Real socket semantics, subscription races and authentication lifetimes."""
from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from backend.sync_socket import install_sync_socket
from test_sync_notifications import hub_fixture
from test_auth_integration import integrated, signing_key, login, HEADERS as AUTH_HEADERS
from account_fixtures import image_bytes


def socket_app(heartbeat=0.03):
    hub, store, auth = hub_fixture(heartbeat=heartbeat)

    @asynccontextmanager
    async def lifespan(app):
        await hub.start()
        try:
            yield
        finally:
            await hub.stop()

    app = FastAPI(lifespan=lifespan)
    install_sync_socket(app, hub, auth)
    return app, hub, store, auth


HEADERS = {"origin": "http://localhost", "cookie": "fixture_session=alice"}


def test_ready_subscription_precedes_watermark_and_hint_has_only_metadata():
    app, hub, store, _ = socket_app(heartbeat=10)
    def during_read(user):
        assert any(item.user_id == user for item in hub.connections)
        store.commit(user, 3)
    store.on_read = during_read
    with TestClient(app) as client, client.websocket_connect("/api/sync/events", headers=HEADERS) as ws:
        ready = ws.receive_json()
        assert ready == {"v": 1, "type": "sync.ready", "serverId": store.server_id, "userId": "alice", "cursor": 3}
        store.on_read = None
        store.commit("alice", 4)
        while (hint := ws.receive_json())["cursor"] < 4:
            pass
        assert set(hint) == set(ready) and hint["type"] == "sync.hint"
    assert not hub.connections


@pytest.mark.parametrize("headers,path", [({}, "/api/sync/events"),
    ({**HEADERS, "origin": "https://evil.test"}, "/api/sync/events"),
    (HEADERS, "/api/sync/events?userId=bob")])
def test_handshake_requires_cookie_origin_and_no_identity_query(headers, path):
    app, hub, _, _ = socket_app()
    with TestClient(app) as client, pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect(path, headers=headers):
            pass
    assert error.value.code == 1008 and not hub.connections


@pytest.mark.parametrize("kind,code", [("revoke", 1008), ("expiry", 1008), ("outage", 1013)])
def test_revocation_expiry_and_temporary_auth_failure_close(kind, code):
    app, hub, _, auth = socket_app()
    with TestClient(app) as client, client.websocket_connect("/api/sync/events", headers=HEADERS) as ws:
        assert ws.receive_json()["type"] == "sync.ready"
        if kind == "revoke":
            auth.revoke("alice")
        else:
            auth.error = 401 if kind == "expiry" else 503
        with pytest.raises(WebSocketDisconnect) as error:
            while True:
                ws.receive_json()
        assert error.value.code == code
        if kind == "outage":
            assert "alice" in auth.sessions
    assert not hub.connections


@pytest.mark.parametrize("payload,code", [("{}", 1008), ("x" * 4097, 1009)], ids=["unknown-message", "oversized-message"])
def test_business_input_and_oversize_are_rejected(payload, code):
    app, hub, _, _ = socket_app()
    with TestClient(app) as client, client.websocket_connect("/api/sync/events", headers=HEADERS) as ws:
        ws.receive_json()
        ws.send_text(payload)
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == code
    assert not hub.connections


def test_slow_consumer_times_out_and_releases_subscription(monkeypatch):
    import backend.sync_socket as module
    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()
    monkeypatch.setattr(module, "SEND_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(module.WebSocket, "send_text", blocked)
    app, hub, _, _ = socket_app()
    with TestClient(app) as client, client.websocket_connect("/api/sync/events", headers=HEADERS) as ws:
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 1013
    assert not hub.connections


def test_real_auth_and_committed_sql_hint_then_logout_closes_before_upstream_revoke(integrated, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    client, app, provider, config = integrated
    session = login(client, provider, "alice")
    headers = {**AUTH_HEADERS, "X-CSRF-Token": session["csrfToken"]}
    entered, release = threading.Event(), threading.Event()

    def blocked_revoke(token):
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(provider, "revoke", blocked_revoke)
    # TestClient uses a ws:// scope; provide the Secure cookie explicitly as a browser does over wss://.
    ws_headers = {"Origin": config.public_url,
                  "Cookie": f"{app.state.auth.cookie_name}={client.cookies.get(app.state.auth.cookie_name)}"}
    with client.websocket_connect("/api/sync/events", headers=ws_headers) as ws:
        ready = ws.receive_json()
        assert ready["userId"] == session["user"]["id"] and ready["cursor"] == 0
        upload = client.post("/api/media", headers=headers, data={"uploadId": "ws-original"},
                             files={"file": ("ticket.png", image_bytes(), "image/png")})
        assert upload.status_code == 200
        response = client.post("/api/tickets", headers=headers, json={"id": "ws-ticket", "operationId": "ws-create",
            "originalAssetId": upload.json()["assetId"], "fields": {"title": "WS fixture"}})
        assert response.status_code == 200
        hint = ws.receive_json()
        assert hint["cursor"] == response.json()["committedCursor"] and hint["type"] == "sync.hint"
        changes = client.get("/api/sync/changes", params={"serverId": ready["serverId"], "after": ready["cursor"]})
        assert changes.status_code == 200, changes.text
        assert changes.json()["changes"][0]["ticket"]["id"] == "ws-ticket"
        with ThreadPoolExecutor(max_workers=1) as executor:
            logout = executor.submit(client.post, "/api/auth/logout", headers=headers)
            try:
                assert entered.wait(2)
                with pytest.raises(WebSocketDisconnect) as closed:
                    ws.receive_json()
                assert closed.value.code == 1008 and not logout.done()
            finally:
                release.set()
            assert logout.result(timeout=3).status_code == 200
    assert not app.state.sync_hub.connections

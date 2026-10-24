"""Real WS dispatch and SQL authorization with disposable Redis transport state."""
from contextlib import contextmanager
import asyncio
import json
import threading
import time
from types import SimpleNamespace
import uuid

import fakeredis
from fastapi.testclient import TestClient
import pytest
from starlette.websockets import WebSocketDisconnect

from backend.app import create_app
from backend.object_storage import LocalObjectStore
from backend.peer_contracts import parse_message, PeerProtocolError
from backend.peer_service import PeerConnection, PeerService
from backend.peer_registry import RegistryError
from backend.shared_state import RedisSharedState
from account_fixtures import FakeAuthFactory, ORIGIN, signed_in
from test_api import upload, create, delete


def peer_app(root, redis=None):
    app = create_app(root / "data", auth_provider=FakeAuthFactory(), run_recovery=False, run_processing=False,
                     object_store=LocalObjectStore(root / "objects"), shared_state=RedisSharedState(client=redis))
    app.state.sync_hub.heartbeat_seconds = 0.03
    return app


def ready(app):
    deadline = time.monotonic() + 3
    while not app.state.peer_service._ready.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert app.state.peer_service._ready.is_set()


@contextmanager
def socket(client, name="alice"):
    headers = {"Origin": ORIGIN, "Cookie": "test_session=session-" + name}
    with client.websocket_connect("/api/sync/events", headers=headers) as ws:
        first = ws.receive_json()
        assert first["type"] == "sync.ready"
        assert set(first) == {"v", "type", "serverId", "userId", "cursor"}
        yield ws


def receive(ws, kind=None, request_id=None):
    for _ in range(150):
        message = ws.receive_json()
        if message["type"].startswith("sync."):
            continue
        if (kind is None or message["type"] == kind) and (request_id is None or message.get("requestId") == request_id):
            return message
        raise AssertionError(f"Unexpected peer event: {message}")
    raise AssertionError("Expected peer event did not arrive within bounded sync heartbeats")


def rpc(ws, message_type, **fields):
    request_id = uuid.uuid4().hex
    ws.send_json({"v": 1, "type": message_type, "requestId": request_id, **fields})
    return receive(ws, request_id=request_id)


def register(ws, name="Test page"):
    result = rpc(ws, "device.register", name=name)
    assert result["type"] == "device.registered" and result["enabled"]
    assert result["peerId"] and result["inventoryRevision"] == 0
    assert result["iceServers"] == [] and result["turnEnabled"] is False
    return result["peerId"]


def manifest(ws, asset):
    result = rpc(ws, "cache.replace", revision=1, batchIndex=0, batchCount=1, assetIds=[asset["assetId"]])
    assert result["type"] == "cache.ack" and result["complete"] and result["count"] == 1


def test_missing_redis_is_explicit_and_sync_remains_usable(tmp_path):
    app = peer_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client, socket(client) as ws:
        result = rpc(ws, "device.register", name="")
        assert result["enabled"] is False and result["peerId"] is None and result["reason"] == "redis_unavailable"
        assert rpc(ws, "device.unregister")["type"] == "device.unregistered"
        signed_in(client)
        asset = upload(client)
        create(client, asset=asset)
        assert client.get(asset["url"] + "/download").status_code == 200
    assert not app.state.peer_service._connections


@pytest.mark.parametrize("raw", [
    '{"v":1,"v":1,"type":"device.list","requestId":"a"}',
    '{"v":1,"type":"device.list","requestId":"a","userId":"bob"}',
    '{"v":1,"type":"device.register","requestId":"a","name":NaN}',
    '{"v":1,"type":[],"requestId":"a"}',
    '{"v":1,"type":"device.register","requestId":"a","name":1e999}',
    json.dumps({"v": 1, "type": "peer.signal", "requestId": "a", "transferId": "x", "kind": [], "payload": None}),
])
def test_strict_peer_parser_rejects_ambiguous_identity_or_values(raw):
    with pytest.raises(PeerProtocolError):
        parse_message(raw)


def test_parser_separates_control_and_sdp_byte_budgets():
    with pytest.raises(PeerProtocolError) as error:
        parse_message(json.dumps({"v": 1, "type": "device.register", "requestId": "a", "name": "x" * 5000}))
    assert error.value.close_code == 1009
    signal = {"v": 1, "type": "peer.signal", "requestId": "a", "transferId": "x", "kind": "offer",
              "payload": {"type": "offer", "sdp": "x" * 49152}}
    assert parse_message(json.dumps(signal))["payload"] == signal["payload"]
    signal["payload"]["sdp"] += "x"
    with pytest.raises(PeerProtocolError):
        parse_message(json.dumps(signal))


def test_tabs_share_account_but_have_distinct_peer_ids_and_asset_authority(tmp_path):
    app = peer_app(tmp_path, fakeredis.FakeRedis())
    with TestClient(app, base_url=ORIGIN) as client:
        ready(app)
        signed_in(client)
        asset = upload(client)
        with socket(client) as source, socket(client) as requester, socket(client, "bob") as other:
            source_id, requester_id, other_id = register(source), register(requester), register(other)
            assert len({source_id, requester_id, other_id}) == 3
            rejected = rpc(source, "cache.replace", revision=1, batchIndex=0, batchCount=1, assetIds=[asset["assetId"]])
            assert rejected["type"] == "peer.error" and rejected["code"] == "not_found"
            create(client, asset=asset)
            manifest(source, asset)
            assert rpc(other, "peer.find", assetId=asset["assetId"])["code"] == "not_found"
            sources = rpc(requester, "peer.find", assetId=asset["assetId"])
            assert sources["peers"] == [{"peerId": source_id, "name": "Test page"}]
            assert rpc(requester, "device.list")["peers"] == rpc(source, "device.list")["peers"]
            assert rpc(source, "device.unregister")["type"] == "device.unregistered"
            assert rpc(requester, "peer.find", assetId=asset["assetId"])["peers"] == []
            assert register(source) != source_id


def test_same_account_transfer_routes_and_final_reference_deletion_revokes(tmp_path):
    app = peer_app(tmp_path, fakeredis.FakeRedis())
    with TestClient(app, base_url=ORIGIN) as client:
        ready(app)
        signed_in(client)
        asset = upload(client)
        create(client, asset=asset)
        create(client, identifier="second", asset=asset)
        with socket(client) as source, socket(client) as requester:
            source_id, requester_id = register(source), register(requester)
            manifest(source, asset)
            pending = rpc(requester, "peer.open", assetId=asset["assetId"], targetPeerId=source_id)
            transfer_id = pending["transferId"]
            requested = receive(source, "peer.request")
            assert requested["transferId"] == transfer_id and requested["requesterPeerId"] == requester_id
            assert set(requested["media"]) == {"assetId", "sha256", "size", "mimeType"}
            authorized = rpc(source, "peer.accept", transferId=transfer_id)
            assert authorized["type"] == "peer.authorized" and authorized["offererPeerId"] == requester_id
            assert receive(requester, "peer.authorized")["transferId"] == transfer_id
            offered = {"type": "offer", "sdp": "v=0\r\n" + "x" * 5000}
            assert rpc(requester, "peer.signal", transferId=transfer_id, kind="offer", payload=offered)["type"] == "peer.signalled"
            delivered = receive(source, "peer.signal")
            assert delivered["payload"] == offered and delivered["fromPeerId"] == requester_id
            assert rpc(source, "peer.signal", transferId=transfer_id, kind="answer", payload={"type": "answer", "sdp": "v=0"})["type"] == "peer.signalled"
            receive(requester, "peer.signal")
            assert delete(client).status_code == 200
            assert rpc(requester, "peer.renew", transferId=transfer_id)["type"] == "peer.lease"
            assert delete(client, identifier="second", operation="delete-second").status_code == 200
            assert receive(requester, "peer.cancelled")["transferId"] == transfer_id
            assert receive(source, "peer.cancelled")["transferId"] == transfer_id
            assert rpc(requester, "peer.find", assetId=asset["assetId"])["code"] == "not_found"
            assert client.get(asset["url"] + "/download").status_code == 404


def test_cross_process_pubsub_uses_same_account_sessions_and_state_loss_requires_registration(tmp_path):
    server = fakeredis.FakeServer()
    one = peer_app(tmp_path, fakeredis.FakeRedis(server=server))
    two = peer_app(tmp_path, fakeredis.FakeRedis(server=server))
    with TestClient(one, base_url=ORIGIN) as first, TestClient(two, base_url=ORIGIN) as second:
        ready(one)
        ready(two)
        signed_in(first)
        asset = upload(first)
        create(first, asset=asset)
        with socket(first) as source, socket(second) as requester:
            source_id = register(source)
            register(requester)
            manifest(source, asset)
            pending = rpc(requester, "peer.open", assetId=asset["assetId"], targetPeerId=source_id)
            transfer_id = pending["transferId"]
            assert receive(source, "peer.request")["transferId"] == transfer_id
            assert rpc(source, "peer.accept", transferId=transfer_id)["type"] == "peer.authorized"
            assert receive(requester, "peer.authorized")["transferId"] == transfer_id
            assert rpc(requester, "peer.signal", transferId=transfer_id, kind="offer", payload={"type": "offer", "sdp": "v=0"})["type"] == "peer.signalled"
            assert receive(source, "peer.signal")["transferId"] == transfer_id
            redis = one.state.shared_state.client
            sentinel = "unrelated-test-" + uuid.uuid4().hex
            redis.set(sentinel, "retained")
            prefix = one.state.peer_service.registry.prefix
            owned = list(redis.scan_iter(match=prefix + "*"))
            assert owned and all(key.decode().startswith(prefix) for key in owned)
            redis.delete(*owned)
            assert redis.get(sentinel) == b"retained"
            # Disposable state loss cannot authorize the previous transfer or peer.
            received = []
            for _ in range(4):
                message = receive(requester)
                received.append(message)
                if message["type"] == "device.registered":
                    break
            assert any(message["type"] == "device.registered" and not message["enabled"] for message in received)
            assert register(requester) != source_id
            assert rpc(requester, "peer.find", assetId=asset["assetId"])["peers"] == []


def test_binary_and_unknown_business_input_close_without_breaking_other_tabs(tmp_path):
    app = peer_app(tmp_path, fakeredis.FakeRedis())
    with TestClient(app, base_url=ORIGIN) as client:
        ready(app)
        with socket(client) as bad, socket(client) as good:
            good_id = register(good)
            bad.send_bytes(b"not-signalling")
            with pytest.raises(WebSocketDisconnect) as error:
                while True:
                    bad.receive_json()
            assert error.value.code == 1008
            assert rpc(good, "device.list")["peers"][0]["peerId"] == good_id


def test_manifest_is_atomic_and_final_batch_rechecks_earlier_asset_authority(tmp_path):
    app = peer_app(tmp_path, fakeredis.FakeRedis())
    with TestClient(app, base_url=ORIGIN) as client:
        ready(app)
        signed_in(client)
        first, second = upload(client), upload(client, color="blue")
        create(client, asset=first)
        create(client, identifier="second", asset=second)
        with socket(client) as source, socket(client) as requester:
            register(source)
            register(requester)
            ack = rpc(source, "cache.replace", revision=1, batchIndex=0, batchCount=2, assetIds=[first["assetId"]])
            assert ack["complete"] is False and ack["nextBatchIndex"] == 1
            assert rpc(source, "device.heartbeat")["inventoryRevision"] == 0
            assert rpc(requester, "peer.find", assetId=first["assetId"])["peers"] == []
            assert delete(client).status_code == 200
            rejected = rpc(source, "cache.replace", revision=1, batchIndex=1, batchCount=2, assetIds=[second["assetId"]])
            assert rejected["type"] == "peer.error" and rejected["code"] == "not_found"
            assert rpc(source, "device.heartbeat")["inventoryRevision"] == 0
            assert rpc(requester, "peer.find", assetId=second["assetId"])["peers"] == []


def test_remote_session_revocation_cancels_transfer_without_logging_requester_out(tmp_path):
    app = peer_app(tmp_path, fakeredis.FakeRedis())
    app.state.auth.sessions["session-alice-source"] = app.state.auth.sessions["session-alice"].copy()
    with TestClient(app, base_url=ORIGIN) as client:
        ready(app)
        signed_in(client)
        asset = upload(client)
        create(client, asset=asset)
        with socket(client, "alice-source") as source, socket(client) as requester:
            source_id = register(source)
            requester_id = register(requester)
            manifest(source, asset)
            transfer = rpc(requester, "peer.open", assetId=asset["assetId"], targetPeerId=source_id)["transferId"]
            receive(source, "peer.request")
            rpc(source, "peer.accept", transferId=transfer)
            receive(requester, "peer.authorized")
            app.state.auth.revoke("alice-source")
            assert receive(requester, "peer.cancelled")["transferId"] == transfer
            assert rpc(requester, "device.list")["peers"] == [{"peerId": requester_id, "name": "Test page"}]
            assert client.get(asset["url"] + "/download").status_code == 200


def test_delete_before_accept_never_authorizes_started_transfer(tmp_path):
    app = peer_app(tmp_path, fakeredis.FakeRedis())
    with TestClient(app, base_url=ORIGIN) as client:
        ready(app)
        signed_in(client)
        asset = upload(client)
        create(client, asset=asset)
        with socket(client) as source, socket(client) as requester:
            source_id = register(source)
            register(requester)
            manifest(source, asset)
            transfer = rpc(requester, "peer.open", assetId=asset["assetId"], targetPeerId=source_id)["transferId"]
            receive(source, "peer.request")
            assert delete(client).status_code == 200
            request_id = uuid.uuid4().hex
            source.send_json({"v": 1, "type": "peer.accept", "requestId": request_id, "transferId": transfer})
            for _ in range(4):
                message = receive(source)
                assert message["type"] != "peer.authorized"
                if message.get("requestId") == request_id:
                    assert message["type"] == "peer.error" and message["code"] in {"not_found", "expired"}
                    break
            else:
                pytest.fail("Deleted transfer must receive an explicit refusal")


def test_subscriber_queue_overflow_reconnects_and_keeps_borrowed_client():
    client = fakeredis.FakeRedis()
    service = PeerService(SimpleNamespace(storage=SimpleNamespace(cache_identity="test")),
                          SimpleNamespace(client=client), object(), "test-" + uuid.uuid4().hex)
    thread = threading.Thread(target=service._subscriber)
    thread.start()
    try:
        assert service._ready.wait(3)
        with service._lock:
            service._incoming.extend([b"{}"] * 64)
            service._incoming_bytes = 128
        client.publish(service.channel_prefix + service.process_id, b"{}")
        deadline = time.monotonic() + 3
        while service._generation == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service._generation == 1
        assert service._ready.wait(3), "Overflow must resubscribe, not permanently disable peers"
        assert not service._incoming and service._incoming_bytes == 0
        assert dict(client.pubsub_numsub(service.channel_prefix + service.process_id))[service.channel_prefix.encode() + service.process_id.encode()] == 1
    finally:
        service._stopping.set()
        thread.join(3)
        assert not thread.is_alive()
        assert client.ping()
        assert dict(client.pubsub_numsub(service.channel_prefix + service.process_id))[service.channel_prefix.encode() + service.process_id.encode()] == 0
        client.close()


@pytest.mark.parametrize("failure", ["subscription", "thread"])
def test_partial_peer_start_releases_subscriptions_and_threads(tmp_path, monkeypatch, failure):
    app = peer_app(tmp_path, fakeredis.FakeRedis())
    service = app.state.peer_service
    if failure == "subscription":
        monkeypatch.setattr(app.state.auth, "subscribe_revocations", lambda _callback: (_ for _ in ()).throw(RuntimeError("injected")))
    else:
        original = threading.Thread.start
        def start(thread):
            if thread.name == "keepsite-peer-subscriber":
                raise RuntimeError("injected")
            return original(thread)
        monkeypatch.setattr(threading.Thread, "start", start)
    async def run():
        try:
            with pytest.raises(RuntimeError, match="injected"):
                await service.start()
        finally:
            await service.stop()
    asyncio.run(run())
    assert not app.state.store._change_subscribers and not app.state.auth._revocations
    assert service._loop is None and service._thread is None and not service._unsubscribers
    assert service.client.ping()
    app.state.shared_state.close()


def test_peer_rate_and_outbound_queue_are_bounded_without_mutating_sync_cursor(monkeypatch):
    class Subscription:
        user_id, cursor = "alice", 91
        def __init__(self):
            self.changed = threading.Event()
            self.close_code = None
        def close(self, code):
            self.close_code = code
    monkeypatch.setattr("backend.peer_service.time.monotonic", lambda: 1000.0)
    connection = PeerConnection(Subscription(), opted_in=True, checked_at=1000.0)
    for _ in range(40):
        connection.check_rate()
    with pytest.raises(RegistryError, match="rate_limited"):
        connection.check_rate()
    for _ in range(33):
        PeerService._enqueue(connection, {"type": "peer.error", "code": "invalid"})
    assert len(connection.queue) == 32 and connection.subscription.close_code == 1013
    assert connection.subscription.cursor == 91


def test_decoded_redis_messages_use_utf8_byte_limits_for_frames_and_queue():
    client = fakeredis.FakeRedis(decode_responses=True)
    service = PeerService(SimpleNamespace(storage=SimpleNamespace(cache_identity="test")),
                          SimpleNamespace(client=client), object(), "test-" + uuid.uuid4().hex)
    thread = threading.Thread(target=service._subscriber)
    thread.start()
    channel = service.channel_prefix + service.process_id
    try:
        assert service._ready.wait(3)
        client.publish(channel, "汉" * 23000)  # 69,000 bytes, not 23,000 bytes.
        client.publish(channel, "汉" * 20000)
        deadline = time.monotonic() + 3
        while not service._incoming and time.monotonic() < deadline:
            time.sleep(0.01)
        with service._lock:
            assert list(service._incoming) == [("汉" * 20000).encode()]
            assert service._incoming_bytes == 60000
        for _ in range(18):
            client.publish(channel, "汉" * 20000)
        deadline = time.monotonic() + 3
        while service._generation == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert service._generation == 1, "The 1 MiB bound counts UTF-8 bytes, not characters"
        assert service._ready.wait(3)
        assert not service._incoming and service._incoming_bytes == 0
    finally:
        service._stopping.set()
        thread.join(3)
        assert not thread.is_alive() and client.ping()
        client.close()


@pytest.mark.parametrize("worker", ["maintenance", "execute"])
def test_stop_waits_for_cancelled_callers_blocking_workers_before_pool_can_close(tmp_path, monkeypatch, worker):
    app = peer_app(tmp_path, fakeredis.FakeRedis())
    service = app.state.peer_service
    entered, release = threading.Event(), threading.Event()
    def blocked(*_args):
        entered.set()
        assert release.wait(5)
    monkeypatch.setattr(service, "_maintain" if worker == "maintenance" else "_execute", blocked)
    async def run():
        await service.start()
        dispatch = None
        if worker == "maintenance":
            service._committed("user-alice", 1)
        else:
            connection = service.attach(SimpleNamespace(user_id="user-alice", session_key="test", changed=asyncio.Event()))
            dispatch = asyncio.create_task(service.dispatch(connection, {"v": 1, "type": "device.register", "requestId": "a", "name": "Test"}))
        assert await asyncio.to_thread(entered.wait, 3)
        if dispatch is not None:
            dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)
        stopping = asyncio.create_task(service.stop())
        try:
            await asyncio.sleep(0.03)
            assert not stopping.done(), "Stop must not return while Redis work still owns a parser"
            assert service._workers and service.client.ping()
        finally:
            release.set()
            await asyncio.wait_for(stopping, 3)
        assert not service._workers and service._thread is None and service._loop is None
    asyncio.run(run())
    app.state.shared_state.close()


def test_concurrent_detach_and_stop_wait_for_late_cleanup_worker(tmp_path, monkeypatch):
    app = peer_app(tmp_path, fakeredis.FakeRedis())
    service = app.state.peer_service
    entered, release, cleanup_entered, cleanup_release = (threading.Event() for _ in range(4))
    def blocked():
        entered.set()
        assert release.wait(5)
    original = service._unregister
    def cleanup(*args):
        cleanup_entered.set()
        assert cleanup_release.wait(5)
        return original(*args)
    monkeypatch.setattr(service, "_unregister", cleanup)
    async def run():
        await service.start()
        connection = service.attach(SimpleNamespace(user_id="alice", session_key="key"))
        work = asyncio.create_task(service._run(blocked, connection=connection))
        assert await asyncio.to_thread(entered.wait, 2)
        detaching = asyncio.create_task(service.detach(connection))
        await asyncio.sleep(0)
        assert connection.connection_id in service._closing and connection.connection_id in service._connections
        stopping = asyncio.create_task(service.stop())
        try:
            release.set()
            assert await asyncio.to_thread(cleanup_entered.wait, 2)
            await asyncio.sleep(0.03)
            assert not stopping.done() and not detaching.done()
            assert service.client.ping()
        finally:
            release.set()
            cleanup_release.set()
            await asyncio.wait_for(asyncio.gather(work, detaching, stopping), 3)
        assert not service._workers and not service._closing and not service._connections
    asyncio.run(run())
    app.state.shared_state.close()


def test_detach_does_not_wait_for_another_connections_slow_work(tmp_path):
    app = peer_app(tmp_path, fakeredis.FakeRedis())
    service = app.state.peer_service
    entered, release = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        assert release.wait(5)
    async def run():
        await service.start()
        first = service.attach(SimpleNamespace(user_id="alice", session_key="first"))
        second = service.attach(SimpleNamespace(user_id="bob", session_key="second"))
        work = asyncio.create_task(service._run(blocked, connection=second))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.wait_for(service.detach(first), 0.5)
            assert not work.done() and second.connection_id in service._connections
        finally:
            release.set()
            await work
            await service.stop()
    asyncio.run(run())
    app.state.shared_state.close()

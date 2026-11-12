"""Opt-in spans follow actual fixture commits, delivery, processing and signaling."""
import json
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient

from backend.diagnostics import using
from backend.peer_contracts import PeerProtocolError, parse_message
from account_fixtures import ORIGIN, USERS, WRITE_HEADERS, signed_in
from test_api import upload
from test_peer_service import peer_app, ready, rpc, socket
from test_processing import pipeline


def context():
    return {"captureId": uuid.uuid4().hex, "traceId": uuid.uuid4().hex}


def trace_headers(diagnostic):
    return {"X-Keepsite-Diagnostics": diagnostic["captureId"], "X-Keepsite-Trace-Id": diagnostic["traceId"]}


def create_traced(client, diagnostic):
    asset = upload(client)
    result = client.post("/api/tickets", headers={**WRITE_HEADERS, **trace_headers(diagnostic)},
                        json={"id": "ticket-1", "operationId": "private-operation", "originalAssetId": asset["assetId"],
                              "fields": {"title": "PRIVATE TICKET CONTENT"}})
    assert result.status_code == 200
    return asset, result.json()


def test_http_job_stream_worker_s3_publish_share_true_source_trace(tmp_path):
    with pipeline(tmp_path) as (client, app, worker, *_):
        diagnostic = context()
        asset, _ = create_traced(client, diagnostic)
        assert worker.run_once()
        result = client.get("/api/diagnostics/events", headers=trace_headers(diagnostic)).json()
        events = result["events"]
        completed = {event["event"] for event in events if event["status"] == "completed"}
        assert {"http.request", "sql.commit", "redis.stream.publish", "redis.stream.receive", "redis.stream.ack",
                "worker.prepare", "worker.ocr", "worker.poster", "worker.publish", "s3.open", "s3.read", "s3.put"} <= completed
        assert {event["traceId"] for event in events} <= {diagnostic["traceId"], None}
        assert all(event["traceId"] is not None or event.get("coalesced") is True for event in events)
        assert any(event["event"] == "s3.read" and event.get("bytes", 0) > 0 for event in events)
        with app.state.store.transaction() as db:
            job_id = db.execute("SELECT id FROM processing_jobs").fetchone()[0]
        exported = json.dumps(result)
        for secret in ("PRIVATE TICKET CONTENT", "private-operation", "user-alice", "alice@example.test", job_id, asset["assetId"]):
            assert secret not in exported


def test_detail_off_records_no_background_spans(tmp_path):
    from test_api import create
    with pipeline(tmp_path) as (client, app, worker, *_):
        create(client)
        assert worker.run_once()
        assert not app.state.diagnostics.captures and not app.state.diagnostics.links


def test_retired_capture_cannot_be_repopulated_by_queued_job(tmp_path):
    with pipeline(tmp_path) as (client, app, worker, *_):
        diagnostic = context()
        create_traced(client, diagnostic)
        assert client.delete("/api/diagnostics/events", headers={**WRITE_HEADERS, **trace_headers(diagnostic)}).status_code == 200
        assert worker.run_once()
        result = client.get("/api/diagnostics/events", headers=trace_headers(diagnostic)).json()
        assert result["events"] == [] and result["expiresAt"] is None


def test_rollback_never_records_commit_or_exposes_private_sql(tmp_path):
    from test_api import make_app
    from backend.auth import hashed
    app = make_app(tmp_path)
    hub, store = app.state.diagnostics, app.state.store
    diagnostic = context()
    ref = hub.begin(USERS["alice"]["id"], hashed("session-alice"), diagnostic["captureId"], diagnostic["traceId"])
    with pytest.raises(ValueError), using(ref):
        with store.transaction(True) as db:
            db.execute("INSERT INTO meta VALUES ('private-key', 'private-password')")
            raise ValueError("private-database-error")
    result = hub.read(ref.owner, ref.session_key, ref.capture_id)
    assert [event["event"] for event in result["events"]] == ["sql.rollback"]
    assert result["events"][0]["status"] == "completed"
    with store.transaction() as db:
        assert db.execute("SELECT 1 FROM meta WHERE key='private-key'").fetchone() is None
    assert "private-" not in json.dumps(result)


def test_ambiguous_links_do_not_pick_last_trace():
    from backend.diagnostics import TraceHub
    hub = TraceHub()
    first = hub.begin("a", "s", uuid.uuid4().hex, uuid.uuid4().hex)
    second = hub.begin("a", "s", uuid.uuid4().hex, uuid.uuid4().hex)
    hub.remember("cursor", 7, first)
    hub.remember("cursor", 7, second)
    assert hub.recall("cursor", "a", 7) is None


def test_optional_signaling_metadata_never_changes_business_validation():
    command = {"v": 1, "type": "device.register", "requestId": "request", "name": "Private device"}
    diagnostic = context()
    assert parse_message(json.dumps({**command, "diagnostics": diagnostic}))["diagnostics"] == diagnostic
    assert parse_message(json.dumps({**command, "diagnostics": {"token": "secret"}})) == command
    with pytest.raises(PeerProtocolError):
        parse_message(json.dumps({**command, "diagnostics": diagnostic, "userId": "victim"}))


def test_signaling_opt_in_is_bound_to_authenticated_socket_and_safe(tmp_path):
    app = peer_app(tmp_path, fakeredis.FakeRedis())
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        ready(app)
        diagnostic = context()
        with socket(client) as ws:
            result = rpc(ws, "device.register", name="PrivateDeviceName", diagnostics=diagnostic)
            assert result["type"] == "device.registered" and result["enabled"]
            assert "diagnostics" not in result
            events = client.get("/api/diagnostics/events", headers=trace_headers(diagnostic)).json()["events"]
            assert any(event["event"] == "p2p.signaling" and event["status"] == "completed" for event in events)
            assert {event["traceId"] for event in events} == {diagnostic["traceId"]}
            assert "PrivateDeviceName" not in json.dumps(events)
        signed_in(client, "bob")
        assert client.get("/api/diagnostics/events", headers=trace_headers(diagnostic)).json()["events"] == []


def test_committed_http_operation_is_correlated_with_actual_ws_hint(tmp_path):
    import time
    with pipeline(tmp_path) as (client, app, *_):
        diagnostic = context()
        with client.websocket_connect("/api/sync/events", headers={"Origin": ORIGIN, "Cookie": "test_session=session-alice"}) as ws:
            first = ws.receive_json()
            assert first["type"] == "sync.ready" and first["heartbeatMs"] == 15_000
            _, receipt = create_traced(client, diagnostic)
            hint = ws.receive_json()
            assert hint["type"] == "sync.hint" and hint["cursor"] == receipt["committedCursor"]
            result = None
            for _ in range(100):
                result = client.get("/api/diagnostics/events", headers=trace_headers(diagnostic)).json()
                if any(event["event"] == "ws.hint" and event["status"] == "completed" for event in result["events"]):
                    break
                time.sleep(0.005)
            hint_events = [event for event in result["events"] if event["event"] == "ws.hint"]
            assert hint_events and any(event["status"] == "completed" for event in hint_events)
            assert {event["traceId"] for event in hint_events} == {diagnostic["traceId"]}


@pytest.mark.parametrize("consumed", [0, 3])
def test_failed_s3_upload_does_not_report_declared_size_as_actual_bytes(consumed):
    import hashlib
    from backend.diagnostics import TraceHub
    from backend.object_storage import S3ObjectStore, StorageError
    from test_object_storage import FakeS3Client
    fake, hub = FakeS3Client(), TraceHub()
    def failed_put(**request):
        request["Body"].read(consumed)
        raise OSError("private provider failure")
    fake.put_object = failed_put
    store = S3ObjectStore("test-bucket", "ap-northeast-1", client=fake)
    diagnostic = context()
    ref = hub.begin("a", "s", diagnostic["captureId"], diagnostic["traceId"])
    raw = b"a payload longer than three bytes"
    with pytest.raises(StorageError), using(ref):
        store.put_bytes("media/a.png", raw, hashlib.sha256(raw).hexdigest(), "image/png")
    events = hub.read("a", "s", ref.capture_id)["events"]
    uploads = [event for event in events if event["event"] == "s3.put"]
    assert uploads[-1]["status"] == "failed" and all("bytes" not in event for event in uploads)


def test_s3_head_success_does_not_claim_content_verification():
    from backend.diagnostics import TraceHub
    from backend.object_storage import S3ObjectStore, StorageError
    from test_object_storage import FakeS3Client
    fake, hub = FakeS3Client(), TraceHub()
    fake.objects["media/a.png"] = (b"wrong", {"sha256": "0" * 64})
    store = S3ObjectStore("test-bucket", "ap-northeast-1", client=fake)
    diagnostic = context()
    ref = hub.begin("a", "s", diagnostic["captureId"], diagnostic["traceId"])
    with pytest.raises(StorageError), using(ref):
        store.verify("media/a.png", 5, "0" * 64, "image/png")
    events = hub.read("a", "s", ref.capture_id)["events"]
    assert events[-1]["event"] == "s3.head" and events[-1]["status"] == "completed"
    assert not any(event["event"] == "s3.verify" or event.get("verified") is True for event in events)


def test_no_match_is_a_completed_prepare_and_actual_no_match_publication(tmp_path):
    from test_processing import PosterFixture, ticket
    with pipeline(tmp_path, provider=PosterFixture(result=None)) as (client, _, worker, *_):
        diagnostic = context()
        create_traced(client, diagnostic)
        assert worker.run_once()
        assert ticket(client)["processing"]["status"] == "no_match"
        events = client.get("/api/diagnostics/events", headers=trace_headers(diagnostic)).json()["events"]
        assert any(event["event"] == "worker.prepare" and event["status"] == "completed" for event in events)
        assert any(event["event"] == "worker.publish" and event["status"] == "completed" for event in events)
        assert not any(event["event"] == "worker.attempt" for event in events)


def test_fenced_worker_publication_is_cancelled_without_faking_commit(tmp_path, monkeypatch):
    from backend import processing_repository as repository
    with pipeline(tmp_path) as (client, app, worker, *_):
        diagnostic = context()
        create_traced(client, diagnostic)
        original = repository.finish
        def fenced(store, job, prepared):
            with store.transaction(True) as db:
                db.execute("UPDATE processing_jobs SET lease_token=lease_token+1 WHERE id=?", (job["id"],))
            return original(store, job, prepared)
        monkeypatch.setattr(repository, "finish", fenced)
        assert worker.run_once()
        events = client.get("/api/diagnostics/events", headers=trace_headers(diagnostic)).json()["events"]
        published = [event for event in events if event["event"] == "worker.publish"]
        assert published[-1]["status"] == "cancelled" and not any(event["status"] == "completed" for event in published)
        with app.state.store.transaction() as db:
            assert db.execute("SELECT status FROM processing_jobs").fetchone()[0] != "ready"


@pytest.mark.parametrize("multiple", [False, True])
def test_real_pending_pubsub_merge_has_no_fake_unique_parent(multiple):
    from dataclasses import replace
    from types import SimpleNamespace
    from test_sync_notifications import hub_fixture
    class Publisher:
        raw = None
        def publish(self, channel, raw):
            self.raw = raw
            hub._stopping.set()
            return 1
    publisher = Publisher()
    hub, store, _ = hub_fixture(shared=SimpleNamespace(client=publisher))
    hub._loop = SimpleNamespace(call_soon_threadsafe=lambda *args: None)
    hub.channel = "fixture"
    diagnostic = context()
    first = store.diagnostics.begin("alice", "alice", diagnostic["captureId"], diagnostic["traceId"])
    first = replace(first, parent_id=uuid.uuid4().hex)
    store.diagnostics.remember("cursor", 1, first)
    hub._committed("alice", 1)
    if multiple:
        second = store.diagnostics.begin("alice", "alice", diagnostic["captureId"], uuid.uuid4().hex)
        store.diagnostics.remember("cursor", 2, replace(second, parent_id=uuid.uuid4().hex))
        hub._committed("alice", 2)
    assert len(hub._publish) == 1
    hub._publisher()
    hub._incoming(publisher.raw)
    result = store.diagnostics.read("alice", "alice", diagnostic["captureId"])
    events = [event for event in result["events"] if event["protocol"] == "redis"]
    assert {event["event"] for event in events} == {"redis.pubsub.publish", "redis.pubsub.receive"}
    assert all(event["coalesced"] is multiple for event in events)
    if multiple:
        assert all(event["traceId"] is None and event["parentSpanId"] is None for event in events)
    else:
        assert all(event["traceId"] == diagnostic["traceId"] and event["parentSpanId"] == first.parent_id for event in events)
    from backend.sync_notifications import Subscription
    subscription = Subscription("alice", "alice")
    hub.connections.add(subscription)
    hub._drain()
    cursor, combined, unknown = subscription.take_hint()
    assert cursor == (2 if multiple else 1) and combined is multiple and not unknown
    subscription.hint(cursor + 1)
    assert subscription.take_hint() == (cursor + 1, False, False)


def test_diagnostic_metadata_counts_toward_existing_raw_frame_budget():
    from backend.peer_contracts import MAX_CONTROL_BYTES
    command = {"v": 1, "type": "device.register", "requestId": "request", "name": "Device"}
    raw = json.dumps(command)
    padding = " " * (MAX_CONTROL_BYTES - len(raw.encode()))
    assert parse_message(raw + padding) == command
    with pytest.raises(PeerProtocolError) as caught:
        parse_message(json.dumps({**command, "diagnostics": context()}) + padding)
    assert caught.value.close_code == 1009


@pytest.mark.parametrize("first_cursor,second_cursor", [(1, 2), (3, 2)])
def test_coalesced_source_is_preserved_until_actual_ws_send(first_cursor, second_cursor):
    import threading
    import time
    from test_sync_socket import socket_app, HEADERS
    app, hub, store, auth = socket_app(heartbeat=10)
    diagnostic = context()
    with TestClient(app) as client, client.websocket_connect("/api/sync/events", headers=HEADERS) as ws:
        assert ws.receive_json()["type"] == "sync.ready"
        entered, release = threading.Event(), threading.Event()
        original = auth.websocket_session
        def paused_authorization(*args):
            entered.set()
            assert release.wait(3)
            return original(*args)
        auth.websocket_session = paused_authorization
        first = store.diagnostics.begin("alice", "alice", diagnostic["captureId"], diagnostic["traceId"])
        store.diagnostics.remember("cursor", first_cursor, first)
        store.commit("alice", first_cursor)
        assert entered.wait(3)
        second = store.diagnostics.begin("alice", "alice", diagnostic["captureId"], uuid.uuid4().hex)
        store.diagnostics.remember("cursor", second_cursor, second)
        store.commit("alice", second_cursor)
        subscription = next(iter(hub.connections))
        for _ in range(100):
            if subscription.coalesced:
                break
            time.sleep(0.005)
        assert subscription.cursor == max(first_cursor, second_cursor) and subscription.coalesced
        release.set()
        hint = ws.receive_json()
        assert hint["cursor"] == max(first_cursor, second_cursor) and hint["type"] == "sync.hint"
        for _ in range(100):
            events = store.diagnostics.read("alice", "alice", diagnostic["captureId"])["events"]
            completed = [event for event in events if event["event"] == "ws.hint" and event["status"] == "completed"]
            if completed:
                break
            time.sleep(0.005)
        assert completed and completed[-1]["coalesced"] is True
        assert completed[-1]["traceId"] is None and completed[-1]["parentSpanId"] is None
        assert not subscription.coalesced and not subscription.pending
        auth.websocket_session = original

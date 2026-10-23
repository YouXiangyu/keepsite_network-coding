"""Snapshot capture boundaries, contiguous changes and commit-only notifications."""
import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from backend import state as presentation
from backend import sync_http
from backend.store import CHANGE_RETENTION_MS, StoreError, encode, now_ms
from account_fixtures import ORIGIN, USERS, WRITE_HEADERS, account_snapshot, signed_in
from test_api import make_app, upload

OWNER = USERS["alice"]["id"]


@pytest.fixture
def workspace(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        yield client, app


def seed(client, count=3):
    asset = upload(client)
    for index in range(count):
        result = client.post("/api/tickets", headers=WRITE_HEADERS,
                             json={"id": f"ticket-{index}", "operationId": f"create-{index}",
                                   "originalAssetId": asset["assetId"], "fields": {"title": f"Movie {index}"}})
        assert result.status_code == 200, result.text
        assert set(result.json()) == {"serverId", "operationId", "committedCursor"}
    return asset


def changes(client, snapshot, after=None, limit=100):
    return client.get("/api/sync/changes", params={"serverId": snapshot["serverId"],
                                                  "after": snapshot["cursor"] if after is None else after, "limit": limit})


def test_snapshot_pages_remain_fixed_during_create_edit_and_delete(workspace):
    client, app = workspace
    asset = seed(client)
    first = client.get("/api/sync/snapshot", params={"limit": 2}).json()
    assert first["total"] == 3 and first["offset"] == 0 and len(first["tickets"]) == 2
    created = client.post("/api/tickets", headers=WRITE_HEADERS,
                          json={"id": "new-ticket", "operationId": "new-ticket", "originalAssetId": asset["assetId"], "fields": {"title": "New"}}).json()
    client.patch("/api/tickets/ticket-0", headers=WRITE_HEADERS, json={"operationId": "edit", "fields": {"title": "Edited"}})
    client.request("DELETE", "/api/tickets/ticket-1", headers=WRITE_HEADERS, json={"operationId": "delete"})
    second = client.get("/api/sync/snapshot", params={"pageToken": first["nextPageToken"], "limit": 2}).json()
    assert all(second[key] == first[key] for key in ("snapshotId", "cursor", "serverId", "userId", "total", "expiresAt"))
    assert second["offset"] == 2 and second["nextPageToken"] is None
    original = first["tickets"] + second["tickets"]
    assert {row["id"] for row in original} == {"ticket-0", "ticket-1", "ticket-2"}
    assert original[0]["fields"]["title"] == "Movie 0"
    view = {row["id"]: row for row in original}
    cursor = first["cursor"]
    while True:
        response = changes(client, first, after=cursor, limit=2)
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["fromCursor"] == cursor
        for event in page["changes"]:
            assert event["cursor"] == cursor + 1
            cursor = event["cursor"]
            if event["type"] == "delete":
                assert set(event) == {"cursor", "type", "id", "version", "deletedAt"}
                view.pop(event["id"], None)
            else:
                assert event["type"] == "upsert"
                view[event["ticket"]["id"]] = event["ticket"]
        assert page["nextCursor"] == cursor
        if not page["hasMore"]:
            break
    current = account_snapshot(client)
    assert cursor == current["cursor"] >= created["committedCursor"]
    assert view == {row["id"]: row for row in current["tickets"]}


def test_single_page_and_empty_wallet_reload_release_all_views(workspace):
    client, app = workspace
    for _ in range(8):
        assert client.get("/api/sync/snapshot").status_code == 200
    seed(client, 1)
    for _ in range(8):
        page = client.get("/api/sync/snapshot").json()
        assert page["total"] == 1 and page["nextPageToken"] is None
    assert app.state.sync_http.views == {} and app.state.sync_http.tokens == {} and app.state.sync_http.bytes_used == 0


def test_final_page_retry_requests_fresh_snapshot_without_retaining_done_views(workspace):
    client, app = workspace
    seed(client)
    first = client.get("/api/sync/snapshot", params={"limit": 1}).json()
    token = first["nextPageToken"]
    final = client.get("/api/sync/snapshot", params={"pageToken": token, "limit": 200})
    assert final.status_code == 200 and final.json()["nextPageToken"] is None
    expired = client.get("/api/sync/snapshot", params={"pageToken": token})
    assert expired.status_code == 410 and expired.json()["detail"]["code"] == "snapshot_expired"
    assert client.get("/api/sync/snapshot").status_code == 200
    assert not app.state.sync_http.views


def test_invalid_other_account_and_expired_tokens_are_indistinguishable(workspace):
    client, app = workspace
    seed(client)
    first = client.get("/api/sync/snapshot", params={"limit": 1}).json()
    invalid = client.get("/api/sync/snapshot", params={"pageToken": "invalid"})
    signed_in(client, "bob")
    foreign = client.get("/api/sync/snapshot", params={"pageToken": first["nextPageToken"]})
    assert foreign.status_code == invalid.status_code == 410 and foreign.json() == invalid.json()
    signed_in(client)
    app.state.sync_http.monotonic = lambda: time.monotonic() + sync_http.SNAPSHOT_SECONDS + 1
    expired = client.get("/api/sync/snapshot", params={"pageToken": first["nextPageToken"]})
    assert expired.status_code == 410 and expired.json() == invalid.json()
    assert app.state.sync_http.bytes_used == 0


def test_active_view_quotas_are_explicit_and_release_restores_capacity(workspace):
    client, app = workspace
    seed(client)
    pages = [client.get("/api/sync/snapshot", params={"limit": 1}).json() for _ in range(4)]
    rejected = client.get("/api/sync/snapshot", params={"limit": 1})
    assert rejected.status_code == 429 and len(app.state.sync_http.views) == 4
    assert client.get("/api/sync/snapshot", params={"pageToken": pages[0]["nextPageToken"], "limit": 200}).status_code == 200
    assert client.get("/api/sync/snapshot", params={"limit": 1}).status_code == 200


def test_global_count_and_reserved_byte_capacity_fail_explicitly(workspace, monkeypatch):
    client, app = workspace
    seed(client)
    monkeypatch.setattr(sync_http, "MAX_VIEWS", 1)
    assert client.get("/api/sync/snapshot", params={"limit": 1}).status_code == 200
    signed_in(client, "bob")
    assert client.get("/api/sync/snapshot").status_code == 429
    app.state.sync_http.close()
    monkeypatch.setattr(sync_http, "MAX_VIEWS", 32)
    monkeypatch.setattr(sync_http, "TOTAL_BYTES", sync_http.SNAPSHOT_BYTES - 1)
    assert client.get("/api/sync/snapshot").status_code == 503
    assert app.state.sync_http.reservations == {}


def test_snapshot_byte_limit_rejects_during_capture_without_partial_view(workspace, monkeypatch):
    client, app = workspace
    seed(client, 10)
    calls = []
    original = presentation.present_ticket
    def counted(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(presentation, "present_ticket", counted)
    monkeypatch.setattr(sync_http, "SNAPSHOT_BYTES", 3500)
    response = client.get("/api/sync/snapshot")
    assert response.status_code == 413 and len(calls) < 10
    assert app.state.sync_http.views == {} and app.state.sync_http.reservations == {}
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 10


def test_snapshot_count_limit_checks_before_materializing_ticket_bodies(workspace, monkeypatch):
    client, app = workspace
    seed(client, 3)
    monkeypatch.setattr(sync_http, "MAX_TICKETS", 2)
    monkeypatch.setattr(presentation, "present_ticket", lambda *args, **kwargs: pytest.fail("Count rejection precedes materialization"))
    assert client.get("/api/sync/snapshot").status_code == 413
    assert not app.state.sync_http.views and not app.state.sync_http.reservations


def test_snapshot_page_byte_budget_splits_without_dropping_records(workspace, monkeypatch):
    client, app = workspace
    seed(client, 5)
    monkeypatch.setattr(sync_http, "PAGE_BYTES", 2048)
    page_token, all_ids, expected_offset = None, [], 0
    while True:
        response = client.get("/api/sync/snapshot", params={"limit": 200, **({"pageToken": page_token} if page_token else {})})
        assert response.status_code == 200, response.text
        assert len(response.content) <= 2048
        page = response.json()
        assert page["offset"] == expected_offset
        all_ids.extend(item["id"] for item in page["tickets"])
        expected_offset += len(page["tickets"])
        page_token = page["nextPageToken"]
        if page_token is None:
            break
    assert len(set(all_ids)) == len(all_ids) == 5


def test_cursor_errors_do_not_jump_or_mix_accounts(workspace):
    client, app = workspace
    seed(client, 1)
    first = account_snapshot(client)
    wrong = client.get("/api/sync/changes", params={"serverId": "other-space", "after": 0})
    ahead = changes(client, first, after=2)
    assert wrong.status_code == ahead.status_code == 410
    assert wrong.json()["detail"]["code"] == "server_changed"
    assert ahead.json()["detail"]["code"] == "cursor_ahead"
    signed_in(client, "bob")
    response = changes(client, first, after=0)
    assert response.status_code == 200
    assert response.json()["userId"] == USERS["bob"]["id"] and response.json()["changes"] == []
    assert response.json()["headCursor"] == 0


def test_log_gap_is_explicit_reset_not_silent_head_advance(workspace):
    client, app = workspace
    seed(client, 3)
    snapshot = account_snapshot(client)
    with app.state.store.transaction(True) as db:
        db.execute("DELETE FROM ticket_changes WHERE user_id=? AND cursor=2", (OWNER,))
    response = changes(client, snapshot, after=0)
    assert response.status_code == 410 and response.json()["detail"]["code"] == "cursor_gap"
    assert "changes" not in response.json()


def test_age_pruning_advances_floor_but_keeps_user_tombstones_and_operations(workspace):
    client, app = workspace
    seed(client, 1)
    client.request("DELETE", "/api/tickets/ticket-0", headers=WRITE_HEADERS, json={"operationId": "delete"})
    snapshot = account_snapshot(client)
    with app.state.store.transaction(True) as db:
        db.execute("UPDATE ticket_changes SET created_at=?", (now_ms() - CHANGE_RETENTION_MS - 1,))
    expired = changes(client, snapshot, after=0)
    assert expired.status_code == 410 and expired.json()["detail"]["code"] == "cursor_expired"
    assert changes(client, snapshot).json()["changes"] == []
    with app.state.store.transaction() as db:
        assert db.execute("SELECT cursor FROM sync_baselines").fetchone()[0] == snapshot["cursor"]
        assert db.execute("SELECT COUNT(*) FROM ticket_changes").fetchone()[0] == 0
        assert db.execute("SELECT deleted_at FROM tickets").fetchone()[0] is not None
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 2


def test_count_window_and_floor_update_are_atomic(workspace, monkeypatch):
    import backend.store as storage_module
    client, app = workspace
    seed(client, 4)
    snapshot = account_snapshot(client)
    monkeypatch.setattr(storage_module, "CHANGE_WINDOW", 2)
    with app.state.store.transaction(True) as db:
        db.execute("CREATE TRIGGER fail_floor BEFORE UPDATE ON sync_baselines BEGIN SELECT RAISE(ABORT,'injected floor failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        app.state.store.prune_changes(OWNER)
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM ticket_changes").fetchone()[0] == 4
        assert db.execute("SELECT cursor FROM sync_baselines").fetchone()[0] == 0
    with app.state.store.transaction(True) as db:
        db.execute("DROP TRIGGER fail_floor")
    assert changes(client, snapshot, after=1).status_code == 410
    valid = changes(client, snapshot, after=2).json()
    assert [entry["cursor"] for entry in valid["changes"]] == [3, 4]


def test_operation_receipt_is_current_barrier_without_historical_cursor_mapping(workspace):
    client, app = workspace
    asset = seed(client, 1)
    first = account_snapshot(client)
    update = client.patch("/api/tickets/ticket-0", headers=WRITE_HEADERS, json={"operationId": "update", "fields": {"title": "Changed"}})
    replay = client.post("/api/tickets", headers=WRITE_HEADERS,
                         json={"id": "ticket-0", "operationId": "create-0", "originalAssetId": asset["assetId"], "fields": {"title": "Movie 0"}})
    assert replay.json() == {"serverId": first["serverId"], "operationId": "create-0", "committedCursor": update.json()["committedCursor"]}
    status = client.get("/api/operations/create-0").json()
    assert status == {"serverId": first["serverId"], "operationId": "create-0", "applied": True, "committedCursor": update.json()["committedCursor"]}
    delta = changes(client, first).json()
    assert delta["fromCursor"] == first["cursor"] and delta["nextCursor"] == status["committedCursor"]
    assert delta["changes"][0]["ticket"]["fields"]["title"] == "Changed"
    assert client.get("/api/operations/missing").json() == {"serverId": first["serverId"], "operationId": "missing", "applied": False, "committedCursor": None}


def test_change_subscribers_only_receive_committed_changes_after_connection_release(workspace, monkeypatch):
    client, app = workspace
    account_snapshot(client)
    observed = []
    def callback(user_id, cursor):
        with app.state.store.transaction(True, timeout=0.01):
            pass
        assert app.state.store.watermark(user_id)["cursor"] >= cursor
        observed.append((user_id, cursor))
    unsubscribe = app.state.store.subscribe_changes(callback)
    seed(client, 1)
    assert observed == [(OWNER, 1)]
    original = app.state.store.record_ticket_change
    def rollback(*args):
        original(*args)
        raise RuntimeError("rollback after recording")
    monkeypatch.setattr(app.state.store, "record_ticket_change", rollback)
    with pytest.raises(RuntimeError):
        client.patch("/api/tickets/ticket-0", headers=WRITE_HEADERS, json={"operationId": "rolled-back", "fields": {"title": "not committed"}})
    assert observed == [(OWNER, 1)]
    monkeypatch.setattr(app.state.store, "record_ticket_change", original)
    unsubscribe()
    client.patch("/api/tickets/ticket-0", headers=WRITE_HEADERS, json={"operationId": "committed", "fields": {"title": "Committed"}})
    assert observed == [(OWNER, 1)]


def test_notifier_failure_does_not_turn_committed_write_into_failure(workspace):
    client, app = workspace
    app.state.store.subscribe_changes(lambda *args: (_ for _ in ()).throw(RuntimeError("notification unavailable")))
    seed(client, 1)
    assert client.get("/api/operations/create-0").json()["applied"] is True
    assert app.state.store.watermark(OWNER)["cursor"] == 1
    with pytest.raises(StoreError):
        app.state.store.watermark("unknown-owner")


def test_receipts_name_the_restored_space_even_when_its_cursor_is_lower(tmp_path):
    from backend.backups import create_account_backup, restore_account_backup
    from test_backup_restore import restored_app
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        asset = seed(client, 1)
        archive = create_account_backup(app.state.store, OWNER)
        for index in range(4):
            client.patch("/api/tickets/ticket-0", headers=WRITE_HEADERS,
                         json={"operationId": f"later-{index}", "fields": {"title": f"Later {index}"}})
        old_space = account_snapshot(client)
    destination = tmp_path / "restored"
    restore_account_backup(archive, destination, user_id=OWNER)
    with TestClient(restored_app(destination), base_url=ORIGIN) as client:
        signed_in(client)
        current = account_snapshot(client)
        replay = client.post("/api/tickets", headers=WRITE_HEADERS,
                             json={"id": "ticket-0", "operationId": "create-0", "originalAssetId": asset["assetId"], "fields": {"title": "Movie 0"}}).json()
        assert replay["serverId"] == current["serverId"] != old_space["serverId"]
        assert replay["committedCursor"] == current["cursor"] < old_space["cursor"]
        known = client.get("/api/operations/create-0").json()
        unknown = client.get("/api/operations/unknown").json()
        assert known["serverId"] == unknown["serverId"] == current["serverId"]
        assert known["committedCursor"] == current["cursor"] and unknown["committedCursor"] is None
        response = changes(client, old_space)
        assert response.status_code == 410 and response.json()["detail"]["code"] == "server_changed"


def test_failed_worker_start_cleans_hub_and_already_started_recovery(tmp_path, monkeypatch):
    import fakeredis
    from backend.app import create_app
    from backend.object_storage import LocalObjectStore
    from backend.shared_state import RedisSharedState
    from account_fixtures import FakeAuthFactory
    cache = RedisSharedState(client=fakeredis.FakeRedis())
    app = create_app(tmp_path / "data", object_store=LocalObjectStore(tmp_path / "objects"),
                     auth_provider=FakeAuthFactory(), shared_state=cache)
    def fail():
        raise RuntimeError("injected processing startup failure")
    monkeypatch.setattr(app.state.processing_worker, "start", fail)
    with pytest.raises(RuntimeError, match="injected processing startup failure"):
        with TestClient(app, base_url=ORIGIN):
            pytest.fail("Application cannot start after worker startup failure")
    assert app.state.sync_hub._threads == [] and app.state.sync_hub._unsubscribers == []
    assert app.state.sync_hub._loop is None
    assert not app.state.upload_recovery.thread.is_alive()
    assert app.state.store._change_subscribers == set()
    assert app.state.auth._revocations == set()
    assert cache.client is None


def test_log_pruning_during_snapshot_pages_requires_resnapshot_after_completion(workspace):
    client, app = workspace
    seed(client, 3)
    first = client.get("/api/sync/snapshot", params={"limit": 1}).json()
    client.patch("/api/tickets/ticket-0", headers=WRITE_HEADERS,
                 json={"operationId": "after-capture", "fields": {"title": "Current"}})
    with app.state.store.transaction(True) as db:
        db.execute("UPDATE ticket_changes SET created_at=?", (now_ms() - CHANGE_RETENTION_MS - 1,))
    app.state.store.prune_changes(OWNER)
    last = client.get("/api/sync/snapshot", params={"pageToken": first["nextPageToken"], "limit": 200}).json()
    assert last["snapshotId"] == first["snapshotId"] and last["cursor"] == first["cursor"]
    expired = changes(client, first)
    assert expired.status_code == 410 and expired.json()["detail"]["code"] == "cursor_expired"
    replacement = account_snapshot(client)
    assert replacement["cursor"] > first["cursor"]
    assert changes(client, replacement).json()["changes"] == []


def test_clock_regression_age_cleanup_still_deletes_only_a_cursor_prefix(workspace):
    client, app = workspace
    seed(client, 3)
    snapshot = account_snapshot(client)
    with app.state.store.transaction(True) as db:
        db.execute("UPDATE ticket_changes SET created_at=? WHERE cursor=2", (now_ms() - CHANGE_RETENTION_MS - 1,))
    app.state.store.prune_changes(OWNER)
    with app.state.store.transaction() as db:
        assert [row[0] for row in db.execute("SELECT cursor FROM ticket_changes ORDER BY cursor")] == [3]
        assert db.execute("SELECT cursor FROM sync_baselines").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 3
    assert changes(client, snapshot, after=1).status_code == 410
    assert [event["cursor"] for event in changes(client, snapshot, after=2).json()["changes"]] == [3]

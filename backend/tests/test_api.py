"""Schema-3 ticket contracts, authorization, persistence and failure boundaries."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend import accounts
from backend.object_storage import LocalObjectStore, S3ObjectStore, StorageError
from backend.shared_state import RedisSharedState
from account_fixtures import FakeAuthFactory, ORIGIN, USERS, WRITE_HEADERS, image_bytes, signed_in
from test_object_storage import FakeS3Client


def make_app(root, storage=None):
    return create_app(root / "data", auth_provider=FakeAuthFactory(), run_recovery=False, run_processing=False, shared_state=RedisSharedState(),
                      object_store=storage or LocalObjectStore(root / "objects"))


@pytest.fixture
def workspace(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        yield client, app


@pytest.fixture
def client(workspace):
    return workspace[0]


def upload(client, color="red", upload_id=None):
    response = client.post("/api/media", headers=WRITE_HEADERS,
                           data={"uploadId": upload_id or str(uuid.uuid4())},
                           files={"file": ("ticket.png", image_bytes(color), "image/png")})
    assert response.status_code == 200, response.text
    assert "cloudStatus" not in response.json()
    return response.json()


def create(client, identifier="ticket-1", asset=None, fields=None):
    asset = asset or upload(client)
    payload = {"id": identifier, "operationId": "create-" + identifier,
               "originalAssetId": asset["assetId"], "fields": fields or {}}
    response = client.post("/api/tickets", headers=WRITE_HEADERS, json=payload)
    assert response.status_code == 200, response.text
    return response.json(), payload


def patch(client, fields, operation="update-1", identifier="ticket-1"):
    return client.patch("/api/tickets/" + identifier, headers=WRITE_HEADERS,
                        json={"operationId": operation, "fields": fields})


def delete(client, identifier="ticket-1", operation="delete-1"):
    return client.request("DELETE", "/api/tickets/" + identifier, headers=WRITE_HEADERS,
                          json={"operationId": operation})


def test_empty_accounts_are_atomic_and_never_clone_templates(workspace):
    client, app = workspace
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: accounts.ensure_account(app.state.store, **{
            "user_id": USERS["alice"]["id"], "email": "alice@example.test", "name": "Alice"}), range(8)))
    state = client.get("/api/state").json()
    assert state["data"] == {"tickets": []}
    assert state["capabilities"] == {"processing": False, "processingUnavailableReason": "需要先配置私有云存储，才能运行电影票处理。", "cloudStorage": False, "deviceSync": False}
    assert not hasattr(app.state, "worker")
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    signed_in(client, "bob")
    assert client.get("/api/state").json()["data"] == {"tickets": []}


@pytest.mark.parametrize("method,path", [("GET", "/api/state"), ("POST", "/api/tickets"),
    ("PATCH", "/api/tickets/x"), ("DELETE", "/api/tickets/x"), ("POST", "/api/media"),
    ("GET", "/api/media/" + "a" * 64), ("GET", "/api/backup"), ("GET", "/api/operations/create-ticket-1")])
def test_business_requires_session(workspace, method, path):
    client, app = workspace
    app.state.auth.revoke("alice")
    assert client.request(method, path).status_code == 401
    assert client.get("/api/health").json() == {"status": "ok"}


@pytest.mark.parametrize("method,path", [("POST", "/api/collections"), ("PATCH", "/api/profile"),
    ("GET", "/api/models/x/model.glb"), ("GET", "/api/jobs/x"), ("POST", "/api/shares/x/collect"),
    ("PUT", "/api/community/x/like"), ("POST", "/api/bootstrap")])
def test_retired_routes_are_removed(client, method, path):
    assert client.request(method, path, headers=WRITE_HEADERS, json={}).status_code == 404


def test_movie_contract_sources_and_actual_local_storage_status(client):
    state, _ = create(client, fields={"title": "电影", "showtime": "2026-10-22T18:30", "seat": ""})
    ticket = state["data"]["tickets"][0]
    assert ticket["fields"] == {"title": "电影", "cinema": "", "hall": "", "seat": "", "showtime": "2026-10-22T18:30"}
    assert ticket["fieldSources"] == {"title": "user", "cinema": "empty", "hall": "empty", "seat": "user", "showtime": "user"}
    assert ticket["processing"] == {"status": "not_started", "error": None}
    assert ticket["media"]["poster"] is None
    assert ticket["media"]["original"]["cloudStatus"] == "not_configured"
    assert ticket["createdAt"] == ticket["updatedAt"] and ticket["version"] == ticket["inputVersion"] == 1
    assert ticket["deletedAt"] is None
    assert set(ticket) == {"id", "fields", "fieldSources", "createdAt", "updatedAt", "version", "inputVersion", "deletedAt", "operationId", "media", "processing"}


@pytest.mark.parametrize("fields", [{"category": "movie"}, {"title": None}, {"title": "x" * 121},
    {"showtime": "2026-10-22"}, {"showtime": "2026-10-22T18:30Z"}, {"showtime": "2026-02-30T18:30"},
    {"showtime": "2026-1-22T18:30"}, {"showtime": 123}, {"cinema": "x" * 201}])
def test_invalid_fields_are_rejected_without_mutation(workspace, fields):
    client, app = workspace
    asset = upload(client)
    response = client.post("/api/tickets", headers=WRITE_HEADERS,
        json={"id": "invalid", "operationId": "invalid-op", "originalAssetId": asset["assetId"], "fields": fields})
    assert response.status_code == 422
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


def test_unreferenced_upload_cannot_be_read_and_shared_active_reference_survives_delete(client):
    asset = upload(client)
    assert client.get(asset["url"]).status_code == 404
    create(client, "one", asset)
    assert client.get(asset["url"]).content == image_bytes()
    create(client, "two", asset)
    assert delete(client, "one").status_code == 200
    assert client.get(asset["url"]).status_code == 200
    assert delete(client, "two", "delete-2").status_code == 200
    assert client.get(asset["url"]).status_code == 404


def test_two_accounts_cannot_read_attach_edit_delete_or_replay_each_others_data(client):
    a, payload = create(client, fields={"title": "Private"})
    original = a["data"]["tickets"][0]["media"]["original"]
    signed_in(client, "bob")
    assert client.get("/api/state").json()["data"]["tickets"] == []
    assert client.get(original["url"]).status_code == 404
    assert client.post("/api/tickets", headers=WRITE_HEADERS, json=payload).status_code == 404
    assert patch(client, {"title": "stolen"}).status_code == 404
    assert delete(client).status_code == 404
    b, _ = create(client, fields={"title": "Bob"})
    assert b["data"]["tickets"][0]["fields"]["title"] == "Bob"
    signed_in(client, "alice")
    assert client.get("/api/state").json()["data"]["tickets"][0]["fields"]["title"] == "Private"


def test_create_replay_and_changed_payload_conflict(client):
    state, payload = create(client)
    replay = client.post("/api/tickets", headers=WRITE_HEADERS, json=payload)
    assert replay.json() == state
    different = {**payload, "fields": {"title": "other"}}
    assert client.post("/api/tickets", headers=WRITE_HEADERS, json=different).status_code == 409
    assert patch(client, {"title": "other"}, payload["operationId"]).status_code == 409


def test_partial_updates_merge_and_replays_do_not_overwrite_later_values(client):
    create(client, fields={"title": "Old", "hall": "1"})
    first = patch(client, {"title": "New"}).json()
    second = patch(client, {"seat": "A1"}, "update-2").json()
    third = patch(client, {"title": "Newest"}, "update-3").json()
    assert third["data"]["tickets"][0]["fields"] == {"title": "Newest", "cinema": "", "hall": "1", "seat": "A1", "showtime": ""}
    assert patch(client, {"title": "New"}).json() == third
    assert patch(client, {"title": "Different"}).status_code == 409
    assert first["data"]["tickets"][0]["inputVersion"] == second["data"]["tickets"][0]["inputVersion"] == 2
    assert third["data"]["tickets"][0]["inputVersion"] == 3
    assert third["data"]["tickets"][0]["version"] == 4


def test_deleted_id_remains_tombstoned_and_old_requests_cannot_revive_it(workspace):
    client, app = workspace
    state, payload = create(client)
    url = state["data"]["tickets"][0]["media"]["original"]["url"]
    patch(client, {"title": "Updated"})
    deleted = delete(client).json()
    assert deleted["data"]["tickets"] == []
    assert client.get(url).status_code == 404
    assert delete(client).json() == deleted
    assert client.post("/api/tickets", headers=WRITE_HEADERS, json=payload).json() == deleted
    assert patch(client, {"title": "Updated"}).json() == deleted
    assert patch(client, {"title": "revive"}, "late-update").status_code == 404
    assert client.post("/api/tickets", headers=WRITE_HEADERS, json={**payload, "operationId": "recreate"}).status_code == 409
    with app.state.store.transaction() as db:
        row = db.execute("SELECT * FROM tickets").fetchone()
        assert row["deleted_at"] is not None
        assert row["version"] == 3
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 3


def test_original_change_revokes_old_reference_and_advances_input(client):
    state, _ = create(client)
    original = state["data"]["tickets"][0]["media"]["original"]
    next_asset = upload(client, "blue")
    response = client.patch("/api/tickets/ticket-1", headers=WRITE_HEADERS,
                            json={"operationId": "change-photo", "originalAssetId": next_asset["assetId"]})
    assert response.status_code == 200
    assert response.json()["data"]["tickets"][0]["inputVersion"] == 2
    assert client.get(original["url"]).status_code == 404
    assert client.get(next_asset["url"]).status_code == 200


def test_operation_and_data_commit_are_atomic(workspace, monkeypatch):
    client, app = workspace
    asset = upload(client)
    def fail(*args):
        raise RuntimeError("injected transaction failure")
    monkeypatch.setattr(app.state.store, "bump", fail)
    with pytest.raises(RuntimeError, match="injected"):
        create(client, asset=asset)
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


def test_persistence_and_replay_after_reopening(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        state, payload = create(client)
    second = make_app(tmp_path)
    with TestClient(second, base_url=ORIGIN) as client:
        signed_in(client)
        assert client.post("/api/tickets", headers=WRITE_HEADERS, json=payload).json() == state
        assert client.get(state["data"]["tickets"][0]["media"]["original"]["url"]).content == image_bytes()


@pytest.mark.parametrize("raw,status", [(b"", 422), (b"not-image", 422), (b"x" * (12 * 1024 * 1024 + 1), 413)], ids=["empty", "invalid", "oversized"])
def test_invalid_uploads_create_no_grants(workspace, raw, status):
    client, app = workspace
    response = client.post("/api/media", headers=WRITE_HEADERS, data={"uploadId": "invalid"}, files={"file": ("image.png", raw, "image/png")})
    assert response.status_code == status
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM asset_grants").fetchone()[0] == 0


def test_storage_upload_failure_is_visible_without_committed_grant(workspace, monkeypatch):
    client, app = workspace
    def fail(*args):
        raise StorageError("private details")
    monkeypatch.setattr(app.state.store.storage, "put_bytes", fail)
    response = client.post("/api/media", headers=WRITE_HEADERS, data={"uploadId": "failure"}, files={"file": ("a.png", image_bytes(), "image/png")})
    assert response.status_code == 503 and "private details" not in response.text
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0


def test_upload_io_never_holds_database_write_lock(workspace, monkeypatch):
    client, app = workspace
    original = app.state.store.storage.put_bytes
    def checked(*args):
        with app.state.store.transaction(True, timeout=0.01):
            pass
        return original(*args)
    monkeypatch.setattr(app.state.store.storage, "put_bytes", checked)
    upload(client)


def test_s3_only_claims_saved_after_reference_commit(tmp_path):
    storage = S3ObjectStore("test", "ap-northeast-1", client=FakeS3Client())
    app = make_app(tmp_path, storage)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        asset = upload(client)
        assert client.get(asset["url"]).status_code == 404
        state, _ = create(client, asset=asset)
        assert state["capabilities"]["cloudStorage"] is True
        assert state["data"]["tickets"][0]["media"]["original"]["cloudStatus"] == "saved"
        assert client.get(asset["url"]).content == image_bytes()


def test_csrf_and_account_body_injection_do_not_mutate(client):
    asset = upload(client)
    payload = {"id": "x", "operationId": "x", "originalAssetId": asset["assetId"]}
    assert client.post("/api/tickets", json=payload).status_code == 403
    assert client.post("/api/tickets", headers={**WRITE_HEADERS, "Origin": "https://evil.test"}, json=payload).status_code == 403
    assert client.post("/api/tickets", headers=WRITE_HEADERS, json={**payload, "userId": "user-bob"}).status_code == 422
    assert client.get("/api/state").json()["data"]["tickets"] == []


@pytest.mark.parametrize("version", ["1", "2", "unknown"])
def test_legacy_schema_is_refused_before_any_directory_write(tmp_path, version):
    root = tmp_path / "legacy"
    root.mkdir()
    path = root / "memoverse.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO meta VALUES('schema_version',?)", (version,))
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="Legacy schema"):
        create_app(root, object_store=object())
    assert path.read_bytes() == before
    assert set(root.iterdir()) == {path}


def test_incomplete_restore_cannot_start(tmp_path):
    root = tmp_path / "incomplete"
    root.mkdir()
    (root / "RESTORE_INCOMPLETE").write_text("pending")
    with pytest.raises(RuntimeError, match="Incomplete"):
        create_app(root, object_store=object())
    assert not (root / "memoverse.sqlite3").exists()


def test_deleted_media_requires_a_fresh_upload_before_new_reference(client):
    state, payload = create(client)
    delete(client)
    new_payload = {**payload, "id": "different-id", "operationId": "different-op"}
    assert client.post("/api/tickets", headers=WRITE_HEADERS, json=new_payload).status_code == 404
    upload(client)
    result = client.post("/api/tickets", headers=WRITE_HEADERS, json=new_payload)
    assert result.status_code == 200
    assert result.json()["data"]["tickets"][0]["id"] == "different-id"


def test_pending_upload_receipt_is_not_consumed_on_failed_creation(workspace, monkeypatch):
    client, app = workspace
    asset = upload(client)
    original = app.state.store.bump
    monkeypatch.setattr(app.state.store, "bump", lambda *args: (_ for _ in ()).throw(RuntimeError("commit failure")))
    with pytest.raises(RuntimeError, match="commit failure"):
        create(client, asset=asset)
    monkeypatch.setattr(app.state.store, "bump", original)
    assert create(client, asset=asset)[0]["data"]["tickets"]


def test_existing_poster_stays_valid_until_new_publication(workspace):
    client, app = workspace
    create(client)
    poster = upload(client, "blue")
    with app.state.store.transaction(True) as db:
        db.execute("UPDATE tickets SET poster_asset_id=? WHERE user_id=? AND id=?", (poster["assetId"], USERS["alice"]["id"], "ticket-1"))
    edited = patch(client, {"title": "Corrected title"}).json()["data"]["tickets"][0]
    assert edited["media"]["poster"]["assetId"] == poster["assetId"]
    assert client.get(poster["url"]).status_code == 200


def test_operation_status_is_account_scoped_and_survives_later_photo_changes(client):
    assert client.get("/api/operations/create-ticket-1").json() == {
        "operationId": "create-ticket-1", "applied": False}
    _, payload = create(client)
    applied = {"operationId": payload["operationId"], "applied": True}
    assert client.get("/api/operations/" + payload["operationId"]).json() == applied
    replacement = upload(client, "blue")
    response = client.patch("/api/tickets/ticket-1", headers=WRITE_HEADERS,
                            json={"operationId": "later-photo", "originalAssetId": replacement["assetId"]})
    assert response.status_code == 200
    assert client.get("/api/operations/" + payload["operationId"]).json() == applied
    delete(client)
    assert client.get("/api/operations/" + payload["operationId"]).json() == applied
    signed_in(client, "bob")
    assert client.get("/api/operations/" + payload["operationId"]).json() == {
        "operationId": payload["operationId"], "applied": False}
    assert client.get("/api/operations/missing-operation").json() == {
        "operationId": "missing-operation", "applied": False}


@pytest.mark.parametrize("identifier", ["invalid.id", "x" * 121, "bad%20id"])
def test_operation_status_validates_identifier(client, identifier):
    assert client.get("/api/operations/" + identifier).status_code == 422


def test_operation_status_reads_persistent_fact_without_mutating_state(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        state, payload = create(client)
    reopened = make_app(tmp_path)
    with TestClient(reopened, base_url=ORIGIN) as client:
        signed_in(client)
        assert client.get("/api/operations/" + payload["operationId"]).json() == {
            "operationId": payload["operationId"], "applied": True}
        assert client.get("/api/operations/missing").json()["applied"] is False
        assert client.get("/api/state").json() == state
        with reopened.state.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1

"""Schema-2 account API tests; all data and authentication are test-owned."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import sqlite3
import uuid
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend import accounts
from backend.object_storage import LocalObjectStore, StorageError
from account_fixtures import FakeAuthFactory, ORIGIN, USERS, WRITE_HEADERS, image_bytes, signed_in, template_fixture


def state(response):
    assert response.status_code in (200, 201), response.text
    result = response.json()
    assert result["initialized"] is True
    assert result["userId"] in {user["id"] for user in USERS.values()}
    assert isinstance(result["revision"], int)
    return result


def error(response, code):
    assert response.status_code == code, response.text
    assert isinstance(response.json().get("detail"), str)


def build_app(root, *, storage=None):
    template = root / "template"
    if not template.exists():
        template_fixture(template)
    auth = FakeAuthFactory()
    app = create_app(data_dir=root / "workspace", run_worker=False, auth_provider=auth,
                     object_store=storage or LocalObjectStore(root / "objects"), template_dir=template)
    return app, auth


@pytest.fixture
def workspace(tmp_path):
    app, auth = build_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        state(client.get("/api/state"))
        yield client, app, auth


@pytest.fixture
def client(workspace):
    return workspace[0]


def upload(client, content=None, mime="image/png"):
    content = image_bytes() if content is None else content
    response = client.post("/api/media", headers=WRITE_HEADERS, files={"file": ("photo.png", content, mime)})
    assert response.status_code == 200, response.text
    return response.json()


def create_payload(asset_id, title="我的照片", identifier=None):
    return {"id": identifier or str(uuid.uuid4()), "title": title, "category": "daily", "tags": ["日常"],
            "note": "保留原图", "date": "2026-10-18", "art": "cafe", "assetId": asset_id}


def create_collection(client, payload=None):
    payload = payload or create_payload(upload(client)["assetId"])
    result = state(client.post("/api/collections", headers=WRITE_HEADERS, json=payload))
    return get_item(result, payload["id"])


def edit_payload(item, **changes):
    return {**{key: item[key] for key in ("version", "title", "category", "tags", "note", "date", "art")}, **changes}


def get_item(result, identifier):
    return next(item for item in result["data"]["collections"] if item["id"] == identifier)


def test_public_health_exposes_no_engine_or_workspace(workspace):
    client, _, _ = workspace
    client.cookies.clear()
    assert client.get("/api/health").json() == {"status": "ok"}


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/state"), ("GET", "/api/media/known"), ("GET", "/api/models/known/model.glb"),
    ("GET", "/api/jobs/known"), ("GET", "/api/backup"), ("POST", "/api/media"),
    ("POST", "/api/collections"), ("PATCH", "/api/collections/known"), ("DELETE", "/api/collections/known"),
    ("POST", "/api/collections/known/retry-generation"), ("PATCH", "/api/profile"),
    ("POST", "/api/shares/read-all"), ("POST", "/api/shares/share-1/read"),
    ("POST", "/api/shares/share-1/collect"), ("POST", "/api/community/community-1/view"),
    ("POST", "/api/community/community-1/collect"), ("PUT", "/api/community/community-1/like"),
])
def test_every_business_route_requires_session(workspace, method, path):
    client, _, _ = workspace
    client.cookies.clear()
    error(client.request(method, path, headers=WRITE_HEADERS), 401)


def test_old_bootstrap_route_is_removed(client):
    assert client.post("/api/bootstrap", headers=WRITE_HEADERS, json={}).status_code == 404


def test_initial_accounts_have_independent_records_and_shared_immutable_files(workspace):
    client, app, _ = workspace
    alice = state(client.get("/api/state"))
    assert alice["data"]["profile"]["name"] == "Alice"
    item = alice["data"]["collections"][0]
    assert item["generation"] == "ready" and "generationJobId" not in item
    assert client.get(item["customImage"]).status_code == 200
    assert client.get(item["modelUrl"]).status_code == 200
    state(client.patch("/api/collections/initial-collection", headers=WRITE_HEADERS, json=edit_payload(item, title="Alice修改")))
    state(client.post("/api/shares/read-all", headers=WRITE_HEADERS))
    state(client.put("/api/community/community-1/like", headers=WRITE_HEADERS, json={"liked": True}))
    signed_in(client, "bob")
    bob = state(client.get("/api/state"))
    assert bob["data"]["profile"]["name"] == "Bob"
    assert bob["data"]["collections"][0]["title"] == "初始收藏"
    assert bob["data"]["shares"][0]["unread"] is True
    assert bob["data"]["likedCommunityIds"] == []
    assert bob["data"]["collections"][0]["modelUrl"] == item["modelUrl"]
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM collections").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_concurrent_initialization_is_atomic_and_repeated_login_preserves_edits(workspace):
    client, app, _ = workspace
    bob = USERS["bob"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: accounts.ensure_account(app.state.store, bob["id"], bob["email"], bob["name"]), range(8)))
    signed_in(client, "bob")
    state(client.patch("/api/profile", headers=WRITE_HEADERS, json={"name": "修改后的Bob", "bio": "保留"}))
    assert state(client.get("/api/state"))["data"]["profile"]["name"] == "修改后的Bob"
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM collections WHERE user_id=?", (bob["id"],)).fetchone()[0] == 1


def test_private_ids_cannot_be_read_attached_edited_or_deleted_by_another_user(workspace):
    client, _, _ = workspace
    original = image_bytes("purple")
    asset = upload(client, original)
    item = create_collection(client, create_payload(asset["assetId"], title="Alice私有记录"))
    signed_in(client, "bob")
    state(client.get("/api/state"))
    error(client.get(item["customImage"]), 404)
    error(client.get(f"/api/jobs/{item['generationJobId']}"), 404)
    error(client.post("/api/collections", headers=WRITE_HEADERS, json=create_payload(asset["assetId"])), 404)
    error(client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS, json=edit_payload(item)), 404)
    error(client.request("DELETE", f"/api/collections/{item['id']}", headers=WRITE_HEADERS, json={"version": item["version"]}), 404)
    error(client.post(f"/api/collections/{item['id']}/retry-generation", headers=WRITE_HEADERS), 404)
    signed_in(client, "alice")
    assert get_item(state(client.get("/api/state")), item["id"])["title"] == "Alice私有记录"
    assert client.get(item["customImage"]).content == original


def test_logout_revokes_old_media_model_and_api_urls(workspace):
    client, _, auth = workspace
    item = state(client.get("/api/state"))["data"]["collections"][0]
    auth.revoke("alice")
    for path in ("/api/state", item["customImage"], item["modelUrl"], "/api/backup"):
        error(client.get(path), 401)


def test_account_ids_from_request_body_cannot_override_cookie_owner(client):
    asset = upload(client)
    error(client.post("/api/collections", headers=WRITE_HEADERS, json={**create_payload(asset["assetId"]), "userId": "user-bob"}), 422)


def test_create_same_client_id_is_scoped_to_each_account(workspace):
    client, _, _ = workspace
    a = create_collection(client, create_payload(upload(client)["assetId"], "Alice", "same-id"))
    signed_in(client, "bob")
    b = create_collection(client, create_payload(upload(client, image_bytes("green"))["assetId"], "Bob", "same-id"))
    assert b["title"] == "Bob" and a["assetId"] != b["assetId"]
    signed_in(client, "alice")
    assert get_item(state(client.get("/api/state")), "same-id")["title"] == "Alice"


def test_create_is_idempotent_and_versions_protect_updates(client):
    body = create_payload(upload(client)["assetId"])
    first = state(client.post("/api/collections", headers=WRITE_HEADERS, json=body))
    repeat = state(client.post("/api/collections", headers=WRITE_HEADERS, json=body))
    assert repeat["revision"] == first["revision"]
    error(client.post("/api/collections", headers=WRITE_HEADERS, json={**body, "title": "冲突"}), 409)
    item = get_item(first, body["id"])
    updated = state(client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS, json=edit_payload(item, title="新标题")))
    assert get_item(updated, item["id"])["generationJobId"] == item["generationJobId"]
    error(client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS, json=edit_payload(item)), 409)
    error(client.request("DELETE", f"/api/collections/{item['id']}", headers=WRITE_HEADERS, json={"version": item["version"]}), 409)


def test_template_deletion_does_not_damage_other_accounts_or_shared_objects(workspace):
    client, _, _ = workspace
    item = state(client.get("/api/state"))["data"]["collections"][0]
    signed_in(client, "bob")
    state(client.get("/api/state"))
    signed_in(client, "alice")
    deleted = state(client.request("DELETE", "/api/collections/initial-collection", headers=WRITE_HEADERS, json={"version": item["version"]}))
    assert deleted["data"]["collections"] == []
    signed_in(client, "bob")
    assert len(state(client.get("/api/state"))["data"]["collections"]) == 1
    assert client.get(item["modelUrl"]).status_code == 200
    assert client.get(item["customImage"]).status_code == 200


def test_shared_copy_reuses_ready_model_without_rerunning_generation(workspace):
    client, app, _ = workspace
    first = state(client.post("/api/shares/share-1/collect", headers=WRITE_HEADERS))
    repeated = state(client.post("/api/shares/share-1/collect", headers=WRITE_HEADERS))
    assert len(first["data"]["collections"]) == len(repeated["data"]["collections"]) == 2
    copied = get_item(repeated, "shared-collection")
    assert copied["generation"] == "ready" and "generationJobId" not in copied
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_community_media_and_interactions_are_account_scoped(workspace):
    client, _, _ = workspace
    item = state(client.get("/api/state"))["data"]["community"][0]
    assert item["generation"] == "ready" and client.get(item["modelUrl"]).status_code == 200
    for _ in range(2):
        value = state(client.put("/api/community/community-1/like", headers=WRITE_HEADERS, json={"liked": True}))
    assert value["data"]["community"][0]["likes"] == 4
    for _ in range(2):
        value = state(client.post("/api/community/community-1/collect", headers=WRITE_HEADERS))
    assert value["data"]["community"][0]["saves"] == 3
    assert get_item(value, "community-community-1")["generation"] == "ready"
    signed_in(client, "bob")
    value = state(client.get("/api/state"))
    assert value["data"]["community"][0]["likes"] == 3
    assert value["data"]["community"][0]["saves"] == 2


@pytest.mark.parametrize("raw,mime,status_code", [(b"not-image", "image/png", 422), (b"<svg/>", "image/svg+xml", 422), (b"x" * (12 * 1024 * 1024 + 1), "image/png", 413)], ids=["non-image", "svg", "oversized"])
def test_invalid_uploads_create_no_asset_grant(workspace, raw, mime, status_code):
    client, app, _ = workspace
    before = state(client.get("/api/state"))
    error(client.post("/api/media", headers=WRITE_HEADERS, files={"file": ("bad", raw, mime)}), status_code)
    assert state(client.get("/api/state"))["revision"] == before["revision"]
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1


def test_uploaded_bytes_hash_and_reopen_persistence(tmp_path):
    app, _ = build_app(tmp_path)
    original = image_bytes("purple", format="JPEG")
    with TestClient(app, base_url=ORIGIN) as first:
        signed_in(first)
        media = upload(first, original, "image/jpeg")
        assert media["sha256"] == hashlib.sha256(original).hexdigest()
        item = create_collection(first, create_payload(media["assetId"]))
        saved = state(first.patch("/api/profile", headers=WRITE_HEADERS, json={"name": "持久昵称", "bio": "保留"}))
    reopened, _ = build_app(tmp_path)
    with TestClient(reopened, base_url=ORIGIN) as second:
        signed_in(second)
        value = state(second.get("/api/state"))
        assert value["serverId"] == saved["serverId"]
        assert value["data"]["profile"]["name"] == "持久昵称"
        assert get_item(value, item["id"])["assetId"] == media["assetId"]
        assert second.get(f"/api/media/{media['assetId']}").content == original


def test_storage_upload_failure_is_visible_and_not_committed(workspace, monkeypatch):
    client, app, _ = workspace
    def fail(*args):
        raise StorageError("simulated object outage")
    monkeypatch.setattr(app.state.store.storage, "put_bytes", fail)
    error(client.post("/api/media", headers=WRITE_HEADERS, files={"file": ("photo.png", image_bytes("purple"), "image/png")}), 503)
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1


def test_upload_object_io_does_not_hold_sql_write_transaction(workspace, monkeypatch):
    client, app, _ = workspace
    original = app.state.store.storage.put_bytes
    def inspect(*args):
        with app.state.store.transaction(True):
            pass
        return original(*args)
    monkeypatch.setattr(app.state.store.storage, "put_bytes", inspect)
    upload(client)


def test_backup_contains_own_community_files_but_no_other_user_data(workspace):
    client, app, _ = workspace
    private_a = image_bytes("red")
    a = create_collection(client, create_payload(upload(client, private_a)["assetId"], "Alice-only"))
    signed_in(client, "bob")
    b = create_collection(client, create_payload(upload(client, image_bytes("green"))["assetId"], "Bob-secret"))
    signed_in(client, "alice")
    response = client.get("/api/backup")
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["userId"] == "user-alice"
        assert "Bob-secret" not in json.dumps(manifest)
        assert all(b["assetId"] not in name for name in archive.namelist())
        assert any(archive.read(name) == private_a for name in archive.namelist() if name.startswith("media/"))
        assert manifest["data"]["community"][0]["modelUrl"] in archive.namelist()
        assert any(row["id"] == a["id"] and row["input_revision"] == 1 for row in manifest["collections"])
        assert not any("reference/" in name or name.endswith(".sqlite3") for name in archive.namelist())
    assert list((app.state.store.root / "exports").iterdir()) == []


def test_csrf_and_foreign_origin_do_not_mutate(client):
    before = state(client.get("/api/state"))
    error(client.patch("/api/profile", json={"name": "bad", "bio": ""}), 403)
    error(client.patch("/api/profile", headers={**WRITE_HEADERS, "Origin": "https://evil.test"}, json={"name": "bad", "bio": ""}), 403)
    assert state(client.get("/api/state"))["data"] == before["data"]


def test_legacy_schema_refusal_preserves_database_and_directory(tmp_path):
    root = tmp_path / "old"
    root.mkdir()
    database = root / "memoverse.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO meta VALUES ('schema_version','1')")
    db.close()
    before = database.read_bytes()
    with pytest.raises(RuntimeError, match="Legacy schema"):
        create_app(data_dir=root, run_worker=False, auth_provider=FakeAuthFactory())
    assert database.read_bytes() == before
    assert [path.name for path in root.iterdir()] == ["memoverse.sqlite3"]


def test_template_tampering_fails_before_creating_account_database(tmp_path):
    template = tmp_path / "template"
    manifest = template_fixture(template)
    (template / manifest["assets"][0]["path"]).write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        create_app(data_dir=tmp_path / "new", run_worker=False, auth_provider=FakeAuthFactory(),
                   object_store=LocalObjectStore(tmp_path / "objects"), template_dir=template)
    assert not (tmp_path / "new/memoverse.sqlite3").exists()


@pytest.mark.parametrize("with_database", [False, True])
def test_incomplete_restoration_is_rejected_before_startup_writes(tmp_path, with_database):
    root = tmp_path / "incomplete"
    root.mkdir()
    if with_database:
        with sqlite3.connect(root / "memoverse.sqlite3") as db:
            db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
            db.execute("INSERT INTO meta VALUES ('schema_version','2')")
    (root / "RESTORE_INCOMPLETE").write_text("interrupted publication")
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    with pytest.raises(RuntimeError, match="Incomplete account restoration"):
        create_app(data_dir=root, run_worker=False, auth_provider=FakeAuthFactory())
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before


def test_existing_account_without_template_survives_but_new_account_is_rejected(workspace):
    from backend.store import StoreError
    client, app, _ = workspace
    before = state(client.get("/api/state"))
    app.state.store.template = None
    accounts.ensure_account(app.state.store, "user-alice", "alice@example.test", "Alice")
    with pytest.raises(StoreError) as failure:
        accounts.ensure_account(app.state.store, "user-bob", "bob@example.test", "Bob")
    assert failure.value.status_code == 503
    assert state(client.get("/api/state"))["data"] == before["data"]
    with app.state.store.transaction() as db:
        assert not db.execute("SELECT 1 FROM users WHERE id='user-bob'").fetchone()


def test_backup_validation_failure_returns_safe_actionable_error(workspace, monkeypatch):
    from backend import api
    from backend.backups import BackupError
    client, _, _ = workspace
    before = state(client.get("/api/state"))

    def fail(*args):
        raise BackupError("fixture internal archive detail")

    monkeypatch.setattr(api, "create_account_backup", fail)
    response = client.get("/api/backup")
    assert response.status_code == 503
    assert "记录仍保留" in response.json()["detail"]
    assert "fixture" not in response.text
    assert state(client.get("/api/state"))["data"] == before["data"]


def test_failed_account_clone_rolls_back_all_user_records(workspace, monkeypatch):
    _, app, _ = workspace
    original = app.state.store.register_model
    def fail(*args):
        raise RuntimeError("fixture initialization failure")
    monkeypatch.setattr(app.state.store, "register_model", fail)
    with pytest.raises(RuntimeError, match="initialization"):
        accounts.ensure_account(app.state.store, "user-bob", "bob@example.test", "Bob")
    with app.state.store.transaction() as db:
        for table in ("users", "profiles", "collections", "asset_grants", "model_grants", "shares", "community"):
            column = "id" if table == "users" else "user_id"
            assert db.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?", ("user-bob",)).fetchone()[0] == 0
    monkeypatch.setattr(app.state.store, "register_model", original)
    accounts.ensure_account(app.state.store, "user-bob", "bob@example.test", "Bob")
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM collections WHERE user_id='user-bob'").fetchone()[0] == 1

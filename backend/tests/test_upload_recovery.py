"""M2 recovery uses persisted intentions and verified object-store facts only."""
import sqlite3
import threading

import pytest
from botocore.exceptions import ReadTimeoutError
from fastapi.testclient import TestClient

from backend import media
from backend.app import create_app
from backend.contracts import Operation, UpdateTicket
from backend.media import recover_pending_uploads
from backend.object_storage import LocalObjectStore, S3ObjectStore, StorageError
from backend.services import tickets
from backend.shared_state import RedisSharedState
from account_fixtures import ORIGIN, USERS, WRITE_HEADERS, FakeAuthFactory, image_bytes, signed_in
from test_api import make_app, upload, create, delete
from test_object_storage import FakeS3Client


def post_image(client, upload_id="upload-1", color="red"):
    return client.post("/api/media", headers=WRITE_HEADERS, data={"uploadId": upload_id},
                       files={"file": ("original.png", image_bytes(color), "image/png")})


@pytest.fixture(params=["local", "s3"])
def workspace(tmp_path, request):
    storage = S3ObjectStore("test-bucket", "ap-northeast-1", client=FakeS3Client()) if request.param == "s3" else LocalObjectStore(tmp_path / "objects")
    app = make_app(tmp_path, storage)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        yield client, app, storage


def test_upload_id_is_required_and_validated(workspace):
    client, _, _ = workspace
    assert client.post("/api/media", headers=WRITE_HEADERS, files={"file": ("a.png", image_bytes(), "image/png")}).status_code == 422
    assert post_image(client, "not.valid").status_code == 422
    assert client.get("/api/uploads/unknown").status_code == 404
    assert client.get("/api/uploads/bad.id").status_code == 422


def test_stable_id_replay_conflict_and_account_isolation(workspace):
    client, app, _ = workspace
    first = post_image(client)
    assert first.status_code == 200
    assert post_image(client).json() == first.json()
    assert post_image(client, color="blue").status_code == 409
    status = client.get("/api/uploads/upload-1").json()
    assert status == {"uploadId": "upload-1", "status": "stored", "asset": {key: value for key, value in first.json().items() if key != "uploadId"}}
    assert client.get(first.json()["url"]).status_code == 404
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM upload_intents").fetchone()[0] == 1
    signed_in(client, "bob")
    assert client.get("/api/uploads/upload-1").status_code == 404
    assert post_image(client, color="blue").status_code == 200
    assert client.get(first.json()["url"]).status_code == 404


def test_object_written_then_database_failure_is_reconciled(workspace, monkeypatch):
    client, app, storage = workspace
    register = app.state.store.register_asset
    def fail(*args):
        raise sqlite3.OperationalError("injected publication failure")
    monkeypatch.setattr(app.state.store, "register_asset", fail)
    with pytest.raises(sqlite3.OperationalError, match="injected"):
        post_image(client)
    with app.state.store.transaction() as db:
        assert db.execute("SELECT status FROM upload_intents").fetchone()[0] == "pending"
        assert db.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
    monkeypatch.setattr(app.state.store, "register_asset", register)
    recovered = client.get("/api/uploads/upload-1")
    assert recovered.status_code == 200 and recovered.json()["status"] == "stored"
    state, _ = create(client, asset=recovered.json()["asset"])
    assert state["data"]["tickets"][0]["media"]["original"]["assetId"] == recovered.json()["asset"]["assetId"]


def test_missing_object_remains_pending_without_authorization(workspace, monkeypatch):
    client, app, storage = workspace
    def fail(*args):
        raise StorageError("storage offline")
    monkeypatch.setattr(storage, "put_bytes", fail)
    assert post_image(client).status_code == 503
    assert client.get("/api/uploads/upload-1").json() == {"uploadId": "upload-1", "status": "pending", "asset": None}
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM upload_receipts").fetchone()[0] == 0


def test_consumed_intent_never_regrants_deleted_media_but_new_id_can(workspace):
    client, app, _ = workspace
    asset = post_image(client).json()
    _, payload = create(client, asset=asset)
    assert client.get("/api/uploads/upload-1").json()["status"] == "attached"
    delete(client)
    assert post_image(client).status_code == 200
    assert client.get("/api/uploads/upload-1").json()["status"] == "attached"
    new_record = {**payload, "id": "another", "operationId": "another-create"}
    assert client.post("/api/tickets", headers=WRITE_HEADERS, json=new_record).status_code == 404
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM upload_receipts").fetchone()[0] == 0
    assert post_image(client, "new-explicit-upload").status_code == 200
    assert client.post("/api/tickets", headers=WRITE_HEADERS, json=new_record).status_code == 200


def test_response_lost_after_s3_put_recovers_without_second_write(tmp_path, monkeypatch):
    fake = FakeS3Client()
    storage = S3ObjectStore("test-bucket", "ap-northeast-1", client=fake)
    app = make_app(tmp_path, storage)
    put = fake.put_object
    def lost(**kwargs):
        put(**kwargs)
        raise ReadTimeoutError(endpoint_url="https://fixture.invalid")
    monkeypatch.setattr(fake, "put_object", lost)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        assert post_image(client).status_code == 503
        assert client.get("/api/uploads/upload-1").json()["status"] == "stored"
        assert post_image(client).status_code == 200
        assert fake.writes == 1
        assert fake.readers == [], "Reconciliation uses verified S3 checksum headers, not full downloads"


def test_recovery_checks_checksum_not_user_metadata(tmp_path, monkeypatch):
    fake = FakeS3Client()
    storage = S3ObjectStore("test-bucket", "ap-northeast-1", client=fake)
    app = make_app(tmp_path, storage)
    real_head = fake.head_object
    def corrupt(**kwargs):
        result = real_head(**kwargs)
        result["ChecksumSHA256"] = "forged-checksum"
        return result
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        monkeypatch.setattr(fake, "head_object", corrupt)
        assert post_image(client).status_code == 503
        assert client.get("/api/uploads/upload-1").status_code == 503
        with app.state.store.transaction() as db:
            assert db.execute("SELECT status FROM upload_intents").fetchone()[0] == "pending"
            assert db.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0
        monkeypatch.setattr(fake, "head_object", real_head)
        assert recover_pending_uploads(app.state.store)["stored"] == 1


def test_recovery_batch_is_bounded_and_storage_io_has_no_sql_lock(workspace, monkeypatch):
    client, app, storage = workspace
    complete = media._complete_upload
    monkeypatch.setattr(media, "_complete_upload", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("paused")))
    for index in range(3):
        with pytest.raises(sqlite3.OperationalError):
            post_image(client, f"pending-{index}")
    monkeypatch.setattr(media, "_complete_upload", complete)
    original_verify = storage.verify
    def verify(*args, **kwargs):
        with app.state.store.transaction(True, timeout=0.01):
            pass
        return original_verify(*args, **kwargs)
    monkeypatch.setattr(storage, "verify", verify)
    assert recover_pending_uploads(app.state.store, limit=1) == {"checked": 1, "stored": 1, "pending": 0, "failed": 0}
    assert recover_pending_uploads(app.state.store, limit=1)["stored"] == 1
    assert recover_pending_uploads(app.state.store, limit=1)["stored"] == 1
    assert recover_pending_uploads(app.state.store, limit=1)["checked"] == 0


def test_stop_before_publication_preserves_pending(workspace, monkeypatch):
    client, app, storage = workspace
    complete = media._complete_upload
    monkeypatch.setattr(media, "_complete_upload", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("paused")))
    with pytest.raises(sqlite3.OperationalError):
        post_image(client)
    monkeypatch.setattr(media, "_complete_upload", complete)
    stopped = threading.Event()
    def verified_then_stopped(*args, **kwargs):
        stopped.set()
        return True
    monkeypatch.setattr(storage, "verify", verified_then_stopped)
    assert recover_pending_uploads(app.state.store, stopping=stopped.is_set)["pending"] == 1
    with app.state.store.transaction() as db:
        assert db.execute("SELECT status FROM upload_intents").fetchone()[0] == "pending"
        assert db.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 0


def test_recovery_cannot_regrant_when_concurrent_edit_consumes_then_deletes(workspace, monkeypatch):
    client, app, storage = workspace
    original = upload(client)
    create(client, asset=original)
    complete = media._complete_upload
    monkeypatch.setattr(media, "_complete_upload", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("paused")))
    with pytest.raises(sqlite3.OperationalError):
        post_image(client, "pending-before-delete")
    monkeypatch.setattr(media, "_complete_upload", complete)
    verify = storage.verify
    def raced(*args, **kwargs):
        result = verify(*args, **kwargs)
        owner = USERS["alice"]["id"]
        tickets.apply(app.state.store, owner, "ticket-1", "update", UpdateTicket(operationId="race-update", fields={"title": "changed"}))
        tickets.apply(app.state.store, owner, "ticket-1", "delete", Operation(operationId="race-delete"))
        return result
    monkeypatch.setattr(storage, "verify", raced)
    result = client.get("/api/uploads/pending-before-delete")
    assert result.json()["status"] == "attached"
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM upload_receipts").fetchone()[0] == 0
    assert client.get(original["url"]).status_code == 404


def test_recovery_thread_stops_with_application_lifecycle(tmp_path):
    app = create_app(tmp_path / "data", object_store=LocalObjectStore(tmp_path / "objects"),
                     auth_provider=FakeAuthFactory(), shared_state=RedisSharedState())
    with TestClient(app, base_url=ORIGIN):
        assert app.state.upload_recovery.thread.is_alive()
    assert not app.state.upload_recovery.thread.is_alive()


def test_transient_s3_head_failure_keeps_recovery_available(tmp_path, monkeypatch):
    fake = FakeS3Client()
    storage = S3ObjectStore("test-bucket", "ap-northeast-1", client=fake)
    app = make_app(tmp_path, storage)
    complete = media._complete_upload
    monkeypatch.setattr(media, "_complete_upload", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("paused")))
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        with pytest.raises(sqlite3.OperationalError):
            post_image(client)
    monkeypatch.setattr(media, "_complete_upload", complete)
    head = fake.head_object
    def offline(**kwargs):
        raise ReadTimeoutError(endpoint_url="https://fixture.invalid")
    monkeypatch.setattr(fake, "head_object", offline)
    assert recover_pending_uploads(app.state.store)["failed"] == 1
    monkeypatch.setattr(fake, "head_object", head)
    assert recover_pending_uploads(app.state.store)["stored"] == 1


def test_upload_intention_storage_identity_cannot_be_reused_after_local_move(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        post_image(client)
    # The database binding permits verified local directory relocation, but an
    # old incomplete receipt cannot authorize a different object root.
    moved = make_app(tmp_path, LocalObjectStore(tmp_path / "other-objects"))
    with TestClient(moved, base_url=ORIGIN) as client:
        signed_in(client)
        assert client.get("/api/uploads/upload-1").status_code == 409
        assert post_image(client).status_code == 409


def test_old_pending_uploads_stop_automatic_io_but_remain_manually_recoverable(workspace, monkeypatch):
    client, app, storage = workspace
    complete = media._complete_upload
    monkeypatch.setattr(media, "_complete_upload", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("paused")))
    with pytest.raises(sqlite3.OperationalError):
        post_image(client)
    monkeypatch.setattr(media, "_complete_upload", complete)
    with app.state.store.transaction(True) as db:
        db.execute("UPDATE upload_intents SET created_at=? WHERE user_id=? AND id=?",
                   (media.now_ms() - media.AUTO_RECOVERY_WINDOW_MS - 1, USERS["alice"]["id"], "upload-1"))
    calls = []
    verify = storage.verify
    def checked(*args, **kwargs):
        calls.append(True)
        return verify(*args, **kwargs)
    monkeypatch.setattr(storage, "verify", checked)
    assert recover_pending_uploads(app.state.store) == {"checked": 0, "stored": 0, "pending": 0, "failed": 0}
    assert calls == [], "An expired automatic recovery window must not issue storage requests"
    with app.state.store.transaction() as db:
        assert db.execute("SELECT status FROM upload_intents").fetchone()[0] == "pending"
    response = client.get("/api/uploads/upload-1")
    assert response.status_code == 200 and response.json()["status"] == "stored"
    assert calls == [True]

"""Owned job-state tests use a tiny test GLB, not real AnyImage inference."""
from __future__ import annotations

import io
import json
import sqlite3
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend import jobs, relief_engine
from backend.object_storage import StorageError
from account_fixtures import ORIGIN, WRITE_HEADERS, image_bytes, signed_in, write_model
from test_api import build_app, create_collection, create_payload, edit_payload, get_item, state, upload


def fixture_generate(input_path, output_dir, **_kwargs):
    assert input_path.is_file()
    return write_model(output_dir)


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "engine_status", lambda: {"available": True, "reason": "test engine"})
    monkeypatch.setattr(relief_engine, "generate_relief", fixture_generate)
    app, auth = build_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        state(client.get("/api/state"))
        yield client, app.state.worker, app, auth


def current(client, item):
    return get_item(state(client.get("/api/state")), item["id"])


def test_model_is_published_only_after_all_files_are_stored(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    put = app.state.store.storage.put_file
    download = app.state.store.storage.download
    calls = []
    def assert_processing_without_write_lock():
        assert current(client, item)["generation"] == "processing"
        assert "modelUrl" not in current(client, item)
        with app.state.store.transaction(True):
            pass
    def observe_download(*args):
        assert_processing_without_write_lock()
        return download(*args)
    def observe_inference(*args, **kwargs):
        assert_processing_without_write_lock()
        return fixture_generate(*args, **kwargs)
    def observe(key, path, digest, mime):
        assert_processing_without_write_lock()
        calls.append(key)
        return put(key, path, digest, mime)
    monkeypatch.setattr(app.state.store.storage, "download", observe_download)
    monkeypatch.setattr(relief_engine, "generate_relief", observe_inference)
    monkeypatch.setattr(app.state.store.storage, "put_file", observe)
    assert worker.run_one()
    ready = current(client, item)
    assert ready["generation"] == "ready"
    assert len(calls) == 3
    assert client.get(ready["modelUrl"]).status_code == 200
    assert list((app.state.store.root / "tmp").iterdir()) == []
    assert worker.run_one() is False


def test_generated_model_is_private_to_its_account(workspace):
    client, worker, _, _ = workspace
    item = create_collection(client)
    worker.run_one()
    ready = current(client, item)
    signed_in(client, "bob")
    state(client.get("/api/state"))
    assert client.get(ready["modelUrl"]).status_code == 404
    assert client.get(f"/api/jobs/{ready['generationJobId']}").status_code == 404
    assert client.get(ready["customImage"]).status_code == 404


def test_inference_failure_preserves_original_and_records_failure(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    original = client.get(item["customImage"]).content
    def fail(*args, **kwargs):
        raise RuntimeError("fixture inference error")
    monkeypatch.setattr(relief_engine, "generate_relief", fail)
    worker.run_one()
    failed = current(client, item)
    assert failed["generation"] == "failed"
    assert "fixture inference error" in failed["generationError"]
    assert "modelUrl" not in failed
    assert client.get(failed["customImage"]).content == original
    assert list((app.state.store.root / "tmp").iterdir()) == []


def test_upload_failure_never_marks_partial_model_ready_and_can_retry(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    put = app.state.store.storage.put_file
    def fail_depth(key, *args):
        if key.endswith("depth.npy"):
            raise StorageError("fixture output upload failure")
        return put(key, *args)
    monkeypatch.setattr(app.state.store.storage, "put_file", fail_depth)
    worker.run_one()
    assert current(client, item)["generation"] == "failed"
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 1
    monkeypatch.setattr(app.state.store.storage, "put_file", put)
    state(client.post(f"/api/collections/{item['id']}/retry-generation", headers=WRITE_HEADERS))
    worker.run_one()
    assert current(client, item)["generation"] == "ready"


def test_blocked_jobs_recover_without_touching_other_accounts_revision(workspace, monkeypatch):
    client, worker, _, _ = workspace
    item = create_collection(client)
    signed_in(client, "bob")
    before_b = state(client.get("/api/state"))
    signed_in(client, "alice")
    monkeypatch.setattr(jobs, "engine_status", lambda: {"available": False, "reason": "offline"})
    assert worker.run_one() is False
    assert current(client, item)["generation"] == "blocked"
    signed_in(client, "bob")
    assert state(client.get("/api/state"))["revision"] == before_b["revision"]
    signed_in(client, "alice")
    monkeypatch.setattr(jobs, "engine_status", lambda: {"available": True, "reason": "restored"})
    assert worker.run_one()
    assert current(client, item)["generation"] == "ready"


def test_start_recovers_interrupted_processing_by_owner(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    with app.state.store.transaction(True) as db:
        db.execute("UPDATE jobs SET status='processing' WHERE id=?", (item["generationJobId"],))
    # Exercise startup transition without launching a real background thread.
    monkeypatch.setattr(worker.thread, "start", lambda: None)
    worker.start()
    assert current(client, item)["generation"] == "queued"


def test_replaced_image_cannot_receive_old_result(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    replacement = upload(client, image_bytes("green"))
    def replace(input_path, output_dir, **kwargs):
        state(client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS, json=edit_payload(item, assetId=replacement["assetId"])))
        return fixture_generate(input_path, output_dir, **kwargs)
    monkeypatch.setattr(relief_engine, "generate_relief", replace)
    worker.run_one()
    updated = current(client, item)
    assert updated["assetId"] == replacement["assetId"]
    assert updated["generation"] == "queued"
    assert updated["generationJobId"] != item["generationJobId"]
    with app.state.store.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM models").fetchone()[0] == 1


def test_deleted_collection_cannot_be_revived(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    def remove(input_path, output_dir, **kwargs):
        state(client.request("DELETE", f"/api/collections/{item['id']}", headers=WRITE_HEADERS, json={"version": item["version"]}))
        return fixture_generate(input_path, output_dir, **kwargs)
    monkeypatch.setattr(relief_engine, "generate_relief", remove)
    worker.run_one()
    assert not any(row["id"] == item["id"] for row in state(client.get("/api/state"))["data"]["collections"])
    with app.state.store.transaction() as db:
        assert db.execute("SELECT status FROM jobs WHERE id=?", (item["generationJobId"],)).fetchone()[0] == "cancelled"


def test_same_collection_id_in_two_accounts_does_not_confuse_worker(workspace):
    client, worker, _, _ = workspace
    a = create_collection(client, create_payload(upload(client)["assetId"], identifier="same-id"))
    signed_in(client, "bob")
    b = create_collection(client, create_payload(upload(client, image_bytes("green"))["assetId"], identifier="same-id"))
    signed_in(client, "alice")
    state(client.request("DELETE", "/api/collections/same-id", headers=WRITE_HEADERS, json={"version": a["version"]}))
    worker.run_one()
    signed_in(client, "bob")
    assert current(client, b)["generation"] == "ready"


def test_foreign_key_prevents_cross_account_asset_job_reference(workspace):
    client, _, app, _ = workspace
    item = create_collection(client)
    signed_in(client, "bob")
    state(client.get("/api/state"))
    with pytest.raises(sqlite3.IntegrityError):
        with app.state.store.transaction(True) as db:
            db.execute("INSERT INTO jobs(id,user_id,collection_id,asset_id,input_revision,status,created_at,updated_at) VALUES ('bad','user-bob','bad',?,1,'queued',0,0)", (item["assetId"],))


def test_account_backup_preserves_generated_depth_geometry_and_owner(workspace):
    client, worker, app, _ = workspace
    item = create_collection(client)
    worker.run_one()
    ready = current(client, item)
    response = client.get("/api/backup")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["userId"] == "user-alice"
        names = bundle.namelist()
        assert f"models/{ready['modelId']}/model.glb" in names
        assert f"models/{ready['modelId']}/depth.npy" in names
        assert f"models/{ready['modelId']}/geometry.json" in names
        assert any(job["id"] == item["generationJobId"] and job["user_id"] == "user-alice" for job in manifest["jobs"])
        assert not any("source.sqlite3" in name or "reference/" in name for name in names)
    assert list((app.state.store.root / "exports").iterdir()) == []


def test_missing_ready_object_aborts_backup_and_removes_partial_zip(workspace):
    client, _, app, _ = workspace
    item = state(client.get("/api/state"))["data"]["collections"][0]
    with app.state.store.transaction() as db:
        model = app.state.store.model(db, "user-alice", item["modelId"])
    (app.state.store.storage.root / model["model_key"]).unlink()
    assert client.get("/api/backup").status_code == 404
    assert list((app.state.store.root / "exports").iterdir()) == []


def test_template_edit_keeps_model_and_replacing_image_enqueues_owned_job(workspace):
    client, _, _, _ = workspace
    item = state(client.get("/api/state"))["data"]["collections"][0]
    edited = get_item(state(client.patch("/api/collections/initial-collection", headers=WRITE_HEADERS, json=edit_payload(item, note="文字修改"))), item["id"])
    assert edited["modelId"] == item["modelId"] and edited["generation"] == "ready"
    replacement = upload(client)
    changed = get_item(state(client.patch("/api/collections/initial-collection", headers=WRITE_HEADERS, json=edit_payload(edited, assetId=replacement["assetId"]))), item["id"])
    assert changed["generation"] == "queued" and "modelUrl" not in changed
    assert client.get(f"/api/jobs/{changed['generationJobId']}").json()["inputRevision"] == 2

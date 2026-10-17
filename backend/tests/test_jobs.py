"""Task-state tests use dummy GLB bytes, never claim to validate real geometry."""
from __future__ import annotations

import io
import struct
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend import jobs, relief_engine
from backend.app import create_app
from test_api import (WRITE_HEADERS, bootstrap, create_collection, edit_payload,
                      get_item, image_bytes, state, upload)


DUMMY_GLB = b"glTF" + struct.pack("<II", 2, 12)


def write_dummy_model(input_path, output_dir, **_kwargs):
    assert input_path.is_file()
    output_dir.mkdir(parents=True, exist_ok=True)
    model = output_dir / "model.glb"
    model.write_bytes(DUMMY_GLB)
    return {"model_path": model, "test_only": True}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "engine_status", lambda: {"available": True, "reason": "test engine"})
    monkeypatch.setattr(relief_engine, "generate_relief", write_dummy_model)
    app = create_app(data_dir=tmp_path, run_worker=False)
    with TestClient(app) as client:
        state(bootstrap(client))
        yield client, app.state.worker


def current(client, item):
    return get_item(client.get("/api/state").json(), item["id"])


def test_success_publishes_model_only_after_job_finishes(workspace, monkeypatch):
    client, worker = workspace
    item = create_collection(client)
    before = client.get("/api/state").json()["revision"]

    def generate(input_path, output_dir, **kwargs):
        assert kwargs["cancel_event"] is worker.stop_event
        in_progress = current(client, item)
        assert in_progress["generation"] == "processing"
        assert "modelUrl" not in in_progress
        return write_dummy_model(input_path, output_dir)

    monkeypatch.setattr(relief_engine, "generate_relief", generate)
    assert worker.run_one() is True
    ready = current(client, item)
    assert ready["generation"] == "ready"
    assert client.get(ready["modelUrl"]).content == DUMMY_GLB
    assert client.get("/api/state").json()["revision"] > before
    assert worker.run_one() is False


def test_generation_failure_keeps_original_and_reports_failure(workspace, monkeypatch):
    client, worker = workspace
    item = create_collection(client)
    original = client.get(item["customImage"]).content

    def fail(*_args, **_kwargs):
        raise RuntimeError("fixture generation failure")

    monkeypatch.setattr(relief_engine, "generate_relief", fail)
    assert worker.run_one() is True
    failed = current(client, item)
    assert failed["generation"] == "failed"
    assert "fixture generation failure" in failed["generationError"]
    assert "modelUrl" not in failed
    assert client.get(failed["customImage"]).content == original
    assert worker.run_one() is False


def test_blocked_task_automatically_runs_after_service_recovers(workspace, monkeypatch):
    client, worker = workspace
    item = create_collection(client)
    monkeypatch.setattr(jobs, "engine_status", lambda: {"available": False, "reason": "test service offline"})
    assert worker.run_one() is False
    blocked = current(client, item)
    assert blocked["generation"] == "blocked"
    assert "test service offline" in blocked["generationError"]
    assert blocked["generationJobId"] == item["generationJobId"]
    assert client.get(blocked["customImage"]).status_code == 200
    monkeypatch.setattr(jobs, "engine_status", lambda: {"available": True, "reason": "recovered"})
    assert worker.run_one() is True
    ready = current(client, item)
    assert ready["generation"] == "ready"
    assert ready["generationJobId"] == item["generationJobId"]
    assert not ready.get("generationError")


def test_interrupted_processing_job_is_recovered_after_reopen(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "engine_status", lambda: {"available": True, "reason": "test engine"})
    monkeypatch.setattr(relief_engine, "generate_relief", write_dummy_model)
    original_app = create_app(data_dir=tmp_path, run_worker=False)
    with TestClient(original_app) as client:
        state(bootstrap(client))
        item = create_collection(client)
        with original_app.state.store.transaction(True) as db:
            db.execute("UPDATE jobs SET status='processing' WHERE id=?", (item["generationJobId"],))
    reopened_app = create_app(data_dir=tmp_path, run_worker=False)
    with TestClient(reopened_app) as client:
        assert current(client, item)["generation"] == "processing"
        worker = reopened_app.state.worker
        # Exercise startup recovery synchronously; do not start its real background thread.
        monkeypatch.setattr(worker.thread, "start", lambda: None)
        worker.start()
        recovered = current(client, item)
        assert recovered["generation"] == "queued"
        assert recovered["generationJobId"] == item["generationJobId"]
        assert worker.run_one() is True
        assert current(client, item)["generation"] == "ready"


def test_replaced_image_rejects_old_running_result(workspace, monkeypatch):
    client, worker = workspace
    item = create_collection(client)
    replacement = upload(client, image_bytes("blue"))

    def generate_and_replace(input_path, output_dir, **_kwargs):
        state(client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                           json=edit_payload(item, assetId=replacement["assetId"])))
        return write_dummy_model(input_path, output_dir)

    monkeypatch.setattr(relief_engine, "generate_relief", generate_and_replace)
    assert worker.run_one() is True
    changed = current(client, item)
    assert changed["assetId"] == replacement["assetId"]
    assert changed["generationJobId"] != item["generationJobId"]
    assert changed["generation"] != "ready"
    assert "modelUrl" not in changed
    assert client.get(f"/api/jobs/{item['generationJobId']}").json()["status"] == "cancelled"
    monkeypatch.setattr(relief_engine, "generate_relief", write_dummy_model)
    assert worker.run_one() is True
    assert current(client, item)["generation"] == "ready"


def test_deleted_collection_cannot_be_revived_by_running_job(workspace, monkeypatch):
    client, worker = workspace
    item = create_collection(client)

    def generate_and_delete(input_path, output_dir, **_kwargs):
        state(client.request("DELETE", f"/api/collections/{item['id']}",
                             headers=WRITE_HEADERS, json={"version": item["version"]}))
        return write_dummy_model(input_path, output_dir)

    monkeypatch.setattr(relief_engine, "generate_relief", generate_and_delete)
    assert worker.run_one() is True
    assert client.get("/api/state").json()["data"]["collections"] == []
    job = client.get(f"/api/jobs/{item['generationJobId']}").json()
    assert job["status"] == "cancelled"
    assert "modelUrl" not in job
    assert client.get(f"/api/models/{item['generationJobId']}/model.glb").status_code == 404


def test_backup_preserves_generated_model_and_depth_array(workspace, monkeypatch):
    client, worker = workspace
    item = create_collection(client)
    depth_bytes = b"dummy depth file for archive state test only"

    def generate(input_path, output_dir, **kwargs):
        result = write_dummy_model(input_path, output_dir, **kwargs)
        (output_dir / "depth.npy").write_bytes(depth_bytes)
        return result

    monkeypatch.setattr(relief_engine, "generate_relief", generate)
    worker.run_one()
    response = client.get("/api/backup")
    assert response.status_code == 200, response.text
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        prefix = f"models/{item['generationJobId']}/"
        assert archive.read(prefix + "model.glb") == DUMMY_GLB
        assert archive.read(prefix + "depth.npy") == depth_bytes


def test_backup_rejects_missing_ready_model_without_losing_original(workspace):
    client, worker = workspace
    item = create_collection(client)
    original = client.get(item["customImage"]).content
    worker.run_one()
    assert current(client, item)["generation"] == "ready"
    # Remove only this test's generated fixture to simulate an interrupted/missing asset.
    model = worker.store.root / "models" / item["generationJobId"] / "model.glb"
    assert model.resolve().is_relative_to(worker.store.root.resolve())
    model.unlink()
    response = client.get("/api/backup")
    assert response.status_code == 409, response.text
    assert isinstance(response.json().get("detail"), str)
    assert client.get(item["customImage"]).content == original
    assert list((worker.store.root / "exports").glob("*.zip")) == []

"""Exercise actual subprocess termination through collection mutations."""
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from backend import jobs, relief_engine
from backend.relief_engine import generate_relief as real_generate_relief
from account_fixtures import WRITE_HEADERS, image_bytes
from test_api import create_collection, edit_payload, state, upload
from test_jobs import current, fixture_generate, workspace


def controlled_processes(monkeypatch):
    """Replace only the command payload; Popen/communicate/signals remain real."""
    original_popen = subprocess.Popen
    processes = []
    started = threading.Event()
    test_directory = str(Path(__file__).parent)
    script = ("import sys,time,json; from pathlib import Path; "
              f"sys.path.insert(0, {test_directory!r}); "
              "from account_fixtures import write_model; "
              "Path(sys.argv[1]).with_suffix('.started').write_text('running'); "
              "time.sleep(float(sys.argv[2])); "
              "print(json.dumps(write_model(Path(sys.argv[1])), default=str), flush=True)")

    def popen(args, **kwargs):
        delay = "60" if not processes else "0"
        process = original_popen([sys.executable, "-B", "-c", script, args[-1], delay], **kwargs)
        processes.append(process)
        ready = Path(args[-1]).with_suffix(".started")
        deadline = time.monotonic() + 5
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "real child did not reach its controlled workload"
        started.set()
        return process

    monkeypatch.setattr(relief_engine, "generate_relief", real_generate_relief)
    monkeypatch.setattr(relief_engine, "engine_status", lambda: {"available": True})
    monkeypatch.setattr(relief_engine.subprocess, "Popen", popen)
    return processes, started


@pytest.mark.parametrize("mutation", ["replace", "delete"])
def test_collection_cancel_terminates_real_process_and_runs_next_job(workspace, monkeypatch, mutation):
    client, worker, app, _ = workspace
    first = create_collection(client)
    replacement = upload(client, image_bytes("green"))
    second = create_collection(client)
    processes, started = controlled_processes(monkeypatch)
    uploads = []
    put = app.state.store.storage.put_file

    def observe_upload(*args):
        assert processes[0].poll() is not None
        uploads.append(args[0])
        return put(*args)

    monkeypatch.setattr(app.state.store.storage, "put_file", observe_upload)
    worker.start()
    try:
        assert started.wait(5), "real child was not started"
        cancelled_at = time.monotonic()
        if mutation == "replace":
            state(client.patch(f"/api/collections/{first['id']}", headers=WRITE_HEADERS,
                               json=edit_payload(first, assetId=replacement["assetId"])))
        else:
            state(client.request("DELETE", f"/api/collections/{first['id']}", headers=WRITE_HEADERS,
                                 json={"version": first["version"]}))
        processes[0].wait(timeout=max(0.1, 6 - (time.monotonic() - cancelled_at)))
        assert time.monotonic() - cancelled_at < 6
        deadline = time.monotonic() + 5
        while len(processes) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(processes) >= 2, "next job did not start immediately after cancellation"
        assert not worker.stop_event.is_set()
        deadline = time.monotonic() + 5
        while current(client, second)["generation"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert current(client, second)["generation"] == "ready"
        with app.state.store.transaction() as db:
            assert db.execute("SELECT status FROM jobs WHERE id=?", (first["generationJobId"],)).fetchone()[0] == "cancelled"
        assert uploads
    finally:
        worker.stop()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
    assert all(process.poll() is not None for process in processes)


def test_service_stop_terminates_real_process_and_restart_recovers(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    processes, started = controlled_processes(monkeypatch)
    worker.start()
    try:
        assert started.wait(5)
        stopped_at = time.monotonic()
        worker.stop()
        assert time.monotonic() - stopped_at < 6
        assert processes[0].poll() is not None
        assert current(client, item)["generation"] == "queued"
    finally:
        worker.stop()
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
    replacement = jobs.JobWorker(app.state.store)
    assert replacement.run_one()
    assert current(client, item)["generation"] == "ready"


@pytest.mark.parametrize("stage", ["download", "inference", "upload"])
def test_cancellation_at_stage_boundary_prevents_remaining_work(workspace, monkeypatch, stage):
    client, worker, app, _ = workspace
    item = create_collection(client)
    storage = app.state.store.storage
    download, put = storage.download, storage.put_file
    counts = {"inference": 0, "uploads": 0}

    def remove():
        state(client.request("DELETE", f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                             json={"version": item["version"]}))

    def downloading(*args):
        result = download(*args)
        if stage == "download":
            remove()
        return result

    def generating(*args, **kwargs):
        counts["inference"] += 1
        result = fixture_generate(*args, **kwargs)
        if stage == "inference":
            remove()
        return result

    def uploading(*args):
        counts["uploads"] += 1
        result = put(*args)
        if stage == "upload":
            remove()
        return result

    monkeypatch.setattr(storage, "download", downloading)
    monkeypatch.setattr(relief_engine, "generate_relief", generating)
    monkeypatch.setattr(storage, "put_file", uploading)
    worker.run_one()
    assert counts["inference"] == (0 if stage == "download" else 1)
    assert counts["uploads"] == (1 if stage == "upload" else 0)
    assert not worker.stop_event.is_set()
    assert state(client.get("/api/state"))["generationRecovery"] == []


def test_cancel_before_task_registration_never_starts_inference(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    claim_next = jobs.claim_next

    def claim_then_delete(*args):
        result = claim_next(*args)
        state(client.request("DELETE", f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                             json={"version": item["version"]}))
        return result

    def unexpected(*args, **kwargs):
        pytest.fail("cancelled task reached inference")

    monkeypatch.setattr(jobs, "claim_next", claim_then_delete)
    monkeypatch.setattr(relief_engine, "generate_relief", unexpected)
    assert worker.run_one()
    assert not worker.active and not worker.pending

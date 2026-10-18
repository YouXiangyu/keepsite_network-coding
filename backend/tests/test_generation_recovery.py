"""Publication recovery never reruns inference or immutable uploads."""
from contextlib import contextmanager
import sqlite3
import time

import pytest

from backend import jobs, relief_engine
from account_fixtures import WRITE_HEADERS, image_bytes, signed_in
from test_api import create_collection, edit_payload, state, upload
from test_jobs import current, fixture_generate, workspace


def count_pipeline(app, monkeypatch, *, failure=None):
    counts = {"inference": 0, "uploads": 0}
    put = app.state.store.storage.put_file

    def generate(*args, **kwargs):
        counts["inference"] += 1
        if failure:
            raise RuntimeError(failure)
        return fixture_generate(*args, **kwargs)

    def publish(*args):
        counts["uploads"] += 1
        return put(*args)

    monkeypatch.setattr(relief_engine, "generate_relief", generate)
    monkeypatch.setattr(app.state.store.storage, "put_file", publish)
    return counts


def fail_publication(store, monkeypatch, stage, *, failures=1, on_failure=None):
    transaction = store.transaction
    attempts = []

    @contextmanager
    def injected(write=False, *, timeout=15):
        publication = write and timeout <= 1
        fail = False
        if publication:
            attempts.append(timeout)
            fail = len(attempts) <= failures
        if fail and stage == "begin":
            if on_failure:
                on_failure()
            raise sqlite3.OperationalError("fixture begin failure")
        with transaction(write, timeout=timeout) as db:
            yield db
            if fail and stage == "commit":
                raise sqlite3.OperationalError("fixture commit failure before commit")
        if fail and stage == "committed":
            raise sqlite3.OperationalError("fixture connection failure after successful commit")

    monkeypatch.setattr(store, "transaction", injected)
    return attempts


@pytest.mark.parametrize("stage", ["begin", "commit", "committed"])
def test_single_publication_failure_replays_only_completion(workspace, monkeypatch, stage):
    client, worker, app, _ = workspace
    item = create_collection(client)
    before = state(client.get("/api/state"))["revision"]
    counts = count_pipeline(app, monkeypatch)
    attempts = fail_publication(app.state.store, monkeypatch, stage)
    assert worker.run_one()
    response = state(client.get("/api/state"))
    assert current(client, item)["generation"] == "ready"
    assert response["revision"] == before + 2
    assert response["generationRecovery"] == []
    assert len(attempts) == 2
    assert counts == {"inference": 1, "uploads": 3}
    assert not worker.run_one()


def test_failed_generation_error_survives_failed_failure_publication(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    original = client.get(item["customImage"]).content
    counts = count_pipeline(app, monkeypatch, failure="original inference failure")
    attempts = fail_publication(app.state.store, monkeypatch, "commit")
    worker.run_one()
    result = current(client, item)
    assert result["generation"] == "failed"
    assert result["generationError"] == "original inference failure"
    assert client.get(result["customImage"]).content == original
    assert counts == {"inference": 1, "uploads": 0}
    assert len(attempts) == 2


@pytest.mark.parametrize("mutation", ["replace", "delete"])
def test_mutation_during_publication_recovery_discards_old_completion(workspace, monkeypatch, mutation):
    client, worker, app, _ = workspace
    item = create_collection(client)
    replacement = upload(client, image_bytes("green"))
    counts = count_pipeline(app, monkeypatch)

    def mutate():
        if mutation == "replace":
            state(client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                               json=edit_payload(item, assetId=replacement["assetId"])))
        else:
            state(client.request("DELETE", f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                                 json={"version": item["version"]}))

    fail_publication(app.state.store, monkeypatch, "begin", on_failure=mutate)
    worker.run_one()
    response = state(client.get("/api/state"))
    assert response["generationRecovery"] == []
    if mutation == "replace":
        result = current(client, item)
        assert result["generation"] == "queued" and result["assetId"] == replacement["assetId"]
        assert "modelUrl" not in result
    else:
        assert not any(row["id"] == item["id"] for row in response["data"]["collections"])
    with app.state.store.transaction() as db:
        assert db.execute("SELECT status FROM jobs WHERE id=?", (item["generationJobId"],)).fetchone()[0] == "cancelled"
    assert counts == {"inference": 1, "uploads": 3}


def test_exhausted_recovery_is_private_bounded_and_explicitly_recoverable(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    counts = count_pipeline(app, monkeypatch)
    attempts = fail_publication(app.state.store, monkeypatch, "begin", failures=3)
    started = time.monotonic()
    worker.run_one()
    assert time.monotonic() - started < jobs.PUBLICATION_BUDGET_SECONDS
    assert len(attempts) == 3
    assert current(client, item)["generation"] == "processing"
    recovery = state(client.get("/api/state"))["generationRecovery"]
    assert len(recovery) == 1 and recovery[0]["jobId"] == item["generationJobId"]
    assert "重试" in recovery[0]["error"]
    for _ in range(3):
        assert worker.run_one() is False
    assert len(attempts) == 3
    signed_in(client, "bob")
    assert state(client.get("/api/state"))["generationRecovery"] == []
    assert client.post(f"/api/collections/{item['id']}/retry-generation", headers=WRITE_HEADERS).status_code == 404
    signed_in(client, "alice")
    state(client.post(f"/api/collections/{item['id']}/retry-generation", headers=WRITE_HEADERS))
    worker.run_one()
    assert current(client, item)["generation"] == "ready"
    assert state(client.get("/api/state"))["generationRecovery"] == []
    assert counts == {"inference": 1, "uploads": 3}


def test_real_sqlite_busy_wait_is_inside_publication_budget(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    store = app.state.store
    put = store.storage.put_file
    blocker = store.connect()

    def lock_after_last_upload(key, *args):
        result = put(key, *args)
        if key.endswith("geometry.json"):
            blocker.execute("BEGIN IMMEDIATE")
        return result

    monkeypatch.setattr(store.storage, "put_file", lock_after_last_upload)
    try:
        started = time.monotonic()
        worker.run_one()
        elapsed = time.monotonic() - started
        assert 3 <= elapsed < jobs.PUBLICATION_BUDGET_SECONDS
        assert worker.recovery_for("user-alice")
    finally:
        blocker.rollback()
        blocker.close()
    state(client.post(f"/api/collections/{item['id']}/retry-generation", headers=WRITE_HEADERS))
    worker.run_one()
    assert current(client, item)["generation"] == "ready"


def test_total_budget_stops_before_attempt_limit(workspace, monkeypatch):
    client, worker, app, _ = workspace
    create_collection(client)
    monkeypatch.setattr(jobs, "PUBLICATION_BUDGET_SECONDS", 0.05)
    attempts = fail_publication(app.state.store, monkeypatch, "begin", failures=100)
    worker.run_one()
    assert len(attempts) == 1
    assert worker.recovery_for("user-alice")


def test_retried_publication_rereads_persisted_cancellation(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    finalize = jobs.finalize
    calls = []

    def cancel_in_database_then_retry(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            # No in-memory signal: the persisted state must independently fence
            # publication, including when the collection version is unchanged.
            with app.state.store.transaction(True) as db:
                db.execute("UPDATE jobs SET status='cancelled' WHERE id=?", (item["generationJobId"],))
            raise sqlite3.OperationalError("fixture publication failure")
        return finalize(*args, **kwargs)

    monkeypatch.setattr(jobs, "finalize", cancel_in_database_then_retry)
    worker.run_one()
    result = current(client, item)
    assert result["generation"] == "cancelled"
    assert "modelUrl" not in result
    assert len(calls) == 2
    assert state(client.get("/api/state"))["generationRecovery"] == []


def test_retry_after_visible_exhaustion_before_active_cleanup_is_not_lost(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    counts = count_pipeline(app, monkeypatch)
    attempts = fail_publication(app.state.store, monkeypatch, "begin", failures=3)
    publish = worker._publish
    key = ("user-alice", item["generationJobId"])

    def retry_before_cleanup(completion):
        publish(completion)
        if worker.recovery_for("user-alice"):
            assert key in worker.active
            response = state(client.post(f"/api/collections/{item['id']}/retry-generation", headers=WRITE_HEADERS))
            assert response["generationRecovery"] == []
            assert worker.pending[key].retry_requested

    monkeypatch.setattr(worker, "_publish", retry_before_cleanup)
    assert worker.run_one()
    assert key not in worker.active
    assert worker.run_one()
    assert current(client, item)["generation"] == "ready"
    assert len(attempts) == 4
    assert counts == {"inference": 1, "uploads": 3}

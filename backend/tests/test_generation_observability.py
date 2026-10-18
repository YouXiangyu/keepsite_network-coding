"""Operational measurements use synthetic jobs and private, test-owned databases."""
import json
import logging
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from backend import jobs, relief_engine
from backend.generation.observability import RunObservations, emit_event, inspect_queue
from account_fixtures import WRITE_HEADERS
from test_api import create_collection, state
from test_generation_recovery import fail_publication
from test_jobs import current, fixture_generate, workspace


def queue_database(tmp_path, rows=(), version="2"):
    root = tmp_path / "data"
    root.mkdir()
    with sqlite3.connect(root / "memoverse.sqlite3") as db:
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
        db.execute("INSERT INTO meta VALUES ('schema_version',?)", (version,))
        db.execute("CREATE TABLE jobs(user_id TEXT,status TEXT,created_at INTEGER)")
        db.executemany("INSERT INTO jobs VALUES (?,?,?)", rows)
    return root


def test_queue_aggregates_without_identities_or_writes(tmp_path):
    root = queue_database(tmp_path, [("private-alice", "queued", 1000),
        ("private-bob", "blocked", 2000), ("private-bob", "processing", 500),
        ("private-alice", "ready", 0), ("private-bob", "failed", 0)])
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    result = inspect_queue(root, sampled_at_ms=4000)
    assert result == {"sampledAt": 4000, "counts": {"queued": 1, "blocked": 1,
        "processing": 1, "ready": 1, "failed": 1, "cancelled": 0},
        "waitingDepth": 2, "oldestWaitingJobAgeSeconds": 3}
    assert "private" not in json.dumps(result)
    assert before == {path.name: path.read_bytes() for path in root.iterdir()}


def test_empty_and_future_waiting_timestamps(tmp_path):
    root = queue_database(tmp_path)
    assert inspect_queue(root)["oldestWaitingJobAgeSeconds"] is None
    with sqlite3.connect(root / "memoverse.sqlite3") as db:
        db.execute("INSERT INTO jobs VALUES ('alice','queued',9000)")
    assert inspect_queue(root, sampled_at_ms=1000)["oldestWaitingJobAgeSeconds"] == 0


def test_live_committed_wal_is_included(tmp_path):
    root = queue_database(tmp_path)
    db = sqlite3.connect(root / "memoverse.sqlite3")
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("INSERT INTO jobs VALUES ('alice','queued',1000)")
        db.commit()
        assert inspect_queue(root, sampled_at_ms=2000)["waitingDepth"] == 1
        db.execute("INSERT INTO jobs VALUES ('bob','queued',1000)")
        assert inspect_queue(root, sampled_at_ms=2000)["waitingDepth"] == 1
    finally:
        db.close()


def test_closed_wal_database_can_be_inspected_without_changing_business_data(tmp_path):
    root = queue_database(tmp_path)
    with sqlite3.connect(root / "memoverse.sqlite3") as db:
        db.execute("PRAGMA journal_mode=WAL")
    db.close()
    database = root / "memoverse.sqlite3"
    before = database.read_bytes()
    assert inspect_queue(root)["waitingDepth"] == 0
    assert database.read_bytes() == before


@pytest.mark.parametrize("kind", ["missing", "empty", "legacy", "incomplete"])
def test_invalid_targets_are_refused_without_creation(tmp_path, kind):
    root = tmp_path / "missing"
    if kind == "empty":
        root.mkdir()
    elif kind in ("legacy", "incomplete"):
        root = queue_database(tmp_path, version="1" if kind == "legacy" else "2")
        if kind == "incomplete":
            (root / "RESTORE_INCOMPLETE").write_text("test")
    before = {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(ValueError):
        inspect_queue(root)
    assert before == {str(path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    if kind == "missing":
        assert not root.exists()


def test_cli_requires_explicit_directory_and_returns_safe_json(tmp_path):
    root = queue_database(tmp_path, [("secret-owner", "failed", 0)])
    script = Path(__file__).resolve().parents[2] / "scripts/inspect-generation.py"
    response = subprocess.run([sys.executable, "-B", str(script), "--data-dir", str(root)],
                              capture_output=True, text=True, timeout=10)
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout)["counts"]["failed"] == 1
    assert "secret-owner" not in response.stdout
    missing = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, text=True, timeout=10)
    assert missing.returncode != 0


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "cancelled", "interrupted"])
def test_attempt_outcomes_and_duration_are_recorded_once(workspace, monkeypatch, outcome):
    client, worker, app, _ = workspace
    item = create_collection(client)
    events = []
    clock = iter((10, 12.5))
    worker.observations = RunObservations(emit=events.append, clock=lambda: next(clock))

    def generate(*args, **kwargs):
        if outcome == "failed":
            raise RuntimeError("secret-error-path-alice@example.test")
        if outcome == "cancelled":
            state(client.request("DELETE", f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                                 json={"version": item["version"]}))
            kwargs["cancel_event"].check()
        if outcome == "interrupted":
            worker.stop_event.set()
            kwargs["cancel_event"].check()
        return fixture_generate(*args, **kwargs)

    monkeypatch.setattr(relief_engine, "generate_relief", generate)
    assert worker.run_one()
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == outcome and event["pipelineDurationSeconds"] == 2.5
    assert event["totals"][outcome] == 1 and sum(event["totals"].values()) == 1
    encoded = json.dumps(event)
    for private in ("user-alice", item["id"], item["generationJobId"], "secret-error", "alice@example.test", str(app.state.store.root)):
        assert private not in encoded


def test_publication_recovery_does_not_count_another_attempt(workspace, monkeypatch):
    client, worker, app, _ = workspace
    item = create_collection(client)
    events = []
    worker.observations = RunObservations(emit=events.append)
    fail_publication(app.state.store, monkeypatch, "begin", failures=3)
    worker.run_one()
    assert [event["event"] for event in events] == ["generation_attempt_finished", "publication_paused"]
    state(client.post(f"/api/collections/{item['id']}/retry-generation", headers=WRITE_HEADERS))
    worker.run_one()
    assert current(client, item)["generation"] == "ready"
    assert [event["event"] for event in events] == ["generation_attempt_finished", "publication_paused", "publication_recovered"]
    assert len({event["attemptId"] for event in events}) == 1
    assert worker.observations.totals["succeeded"] == 1


def test_logging_failure_cannot_prevent_publication(workspace):
    client, worker, _, _ = workspace
    item = create_collection(client)
    def broken(event):
        raise OSError("unavailable log sink")
    worker.observations = RunObservations(emit=broken)
    worker.run_one()
    assert current(client, item)["generation"] == "ready"


def test_info_sink_is_visible_and_handler_is_not_duplicated(capsys, monkeypatch):
    logger = logging.getLogger("memoverse.generation.metrics")
    monkeypatch.setattr(logger, "handlers", [])
    monkeypatch.setattr(logger, "level", logging.NOTSET)
    monkeypatch.setattr(logger, "propagate", True)
    emit_event({"event": "fixture"})
    emit_event({"event": "fixture"})
    assert len(logger.handlers) == 1
    assert [json.loads(line) for line in capsys.readouterr().err.splitlines()] == [{"event": "fixture"}] * 2


def test_stale_claim_is_not_an_execution(workspace, monkeypatch):
    client, worker, _, _ = workspace
    create_collection(client)
    events = []
    worker.observations = RunObservations(emit=events.append)
    monkeypatch.setattr(jobs, "is_current", lambda *args: False)
    worker.run_one()
    assert events == []

"""The benchmark must measure actual committed lifecycles, including batched emits."""
import importlib.util
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("processing_benchmark", ROOT / "scripts/benchmark-processing.py")
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


def test_real_http_and_worker_produce_complete_timed_lifecycle(tmp_path):
    from account_fixtures import image_bytes

    expected = {"title": "Synthetic example", "cinema": "Synthetic cinema", "hall": "1", "seat": "A1",
                "showtime": "2026-11-01T18:30"}
    samples = [{"raw": image_bytes(color), "expected": expected} for color in ("red", "blue")]
    report = benchmark.run_pipeline(tmp_path, samples, recognition_runner=lambda raw, **kwargs: expected)
    assert report["recognitionMode"] == "offline-test-injected"
    assert report["statusCounts"] == {"ready": 2}
    assert report["checks"] == {"completeJobCount": 2, "durableChangesObserved": 8, "observerOverflow": False,
                                "groundTruthNeverSubmitted": True, "retries": 0,
                                "temporaryAccountRemoved": True, "posterProviderClosed": True}
    for row in report["samples"]:
        assert [event["status"] for event in row["events"]] == ["queued", "ocr", "poster", "ready"]
        assert all(event["notificationBatchSize"] == 1 for event in row["events"])
        assert row["actual"] == row["expected"]
        assert row["queuedToFirstClaimMs"] + row["firstClaimToTerminalMs"] == pytest.approx(row["queuedToTerminalMs"])
        assert row["admissionBatchWaitMs"] + row["postConsumerToFirstClaimMs"] == pytest.approx(row["queuedToFirstClaimMs"])
        assert len(row["recognitionAttempts"]) == 1 and row["recognitionAttempts"][0]["success"]
    assert report["failureRate"] == 0
    assert all(result["exact"] == 2 for result in report["fieldAccuracy"].values())
    assert report["memory"]["samples"] > 0
    assert not list(tmp_path.iterdir())


def test_post_commit_interval_covers_every_change_in_expiry_transaction(tmp_path):
    from backend.accounts import ensure_account
    from backend.contracts import CreateTicket
    from backend.media import persist_image
    from backend.object_storage import S3ObjectStore
    from backend import processing_repository as repository
    from backend.services import tickets
    from backend.store import Store
    from account_fixtures import image_bytes
    from test_object_storage import FakeS3Client

    # This tests a SQL notification boundary. HTTP/worker lifecycles are covered
    # above; do not start unrelated PubSub and peer services for this assertion.
    storage = S3ObjectStore("synthetic-benchmark", "ap-northeast-1", client=FakeS3Client())
    try:
        store = Store(tmp_path / "account", storage=storage)
        ensure_account(store, benchmark.USER_ID, "synthetic@example.test", "Synthetic")
        asset = persist_image(store, benchmark.USER_ID, "synthetic-upload", image_bytes())
        store.processing_enabled = True
        for identifier in ("first", "second"):
            tickets.apply(store, benchmark.USER_ID, identifier, "create", CreateTicket(
                id=identifier, operationId="create-" + identifier, originalAssetId=asset["assetId"], fields={}))
        baseline = store.watermark(benchmark.USER_ID)["cursor"]
        observer = benchmark.CommitObserver(baseline)
        unsubscribe = store.subscribe_changes(observer.observe)
        origin = time.perf_counter_ns()
        try:
            with store.transaction(True) as db:
                db.execute("UPDATE processing_jobs SET deadline_at=0")
            assert repository.expire_jobs(store) == 2
            assert len(observer.events) == 1
            events = observer.materialize(store, origin)
            assert {event["ticketId"] for event in events} == {"first", "second"}
            assert [event["cursor"] for event in events] == [baseline + 1, baseline + 2]
            assert all(event["status"] == "failed" and event["notificationBatchSize"] == 2 for event in events)
            assert events[0]["observedCommitMs"] == events[1]["observedCommitMs"]
            observer.events.clear()
            with pytest.raises(RuntimeError, match="missing post-commit"):
                observer.materialize(store, origin)
        finally:
            unsubscribe()
    finally:
        storage.close()


def test_injected_recognition_failure_is_reported_as_failure_without_hiding_retry(tmp_path):
    from account_fixtures import image_bytes
    from backend.processing import ProcessingFailed

    calls = []

    def flaky(raw, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ProcessingFailed()
        return {"title": "Recovered"}

    report = benchmark.run_pipeline(tmp_path, [{"raw": image_bytes(), "expected": {"title": "Recovered"}}],
                                    recognition_runner=flaky, timeout=20)
    row = report["samples"][0]
    assert row["status"] == "ready" and row["retryCount"] == 1
    assert [attempt["success"] for attempt in row["recognitionAttempts"]] == [False, True]
    assert [event["status"] for event in row["events"]] == ["queued", "ocr", "queued", "ocr", "poster", "ready"]
    assert report["timings"]["recognitionAttemptMs"]["count"] == 2
    assert row["queuedToFirstClaimMs"] + row["firstClaimToTerminalMs"] == pytest.approx(row["queuedToTerminalMs"])


def test_deadline_stops_owned_worker_sampler_and_removes_scratch(tmp_path):
    from account_fixtures import image_bytes
    from backend.processing import ProcessingInterrupted

    existing_threads = set(threading.enumerate())
    entered = threading.Event()

    def wait_until_stopped(raw, *, stopping, **kwargs):
        entered.set()
        while not stopping():
            time.sleep(.005)
        raise ProcessingInterrupted()

    with pytest.raises(RuntimeError, match="benchmark deadline"):
        benchmark.run_pipeline(tmp_path, [{"raw": image_bytes(), "expected": {}}],
                               recognition_runner=wait_until_stopped, timeout=.2)
    assert entered.is_set()
    assert not list(tmp_path.iterdir())
    assert not [thread for thread in threading.enumerate() if thread not in existing_threads
                and thread.name in {"keepsite-ticket-processing", "benchmark-owned-memory"}]


def test_output_guard_never_overwrites_existing_evidence(tmp_path):
    existing = tmp_path / "evidence"
    existing.mkdir()
    sentinel = existing / "report.json"
    sentinel.write_text(json.dumps({"keep": True}), encoding="utf-8")
    with pytest.raises(ValueError):
        benchmark.safe_destination(existing)
    with pytest.raises(ValueError):
        benchmark.safe_destination(Path("relative-output"))
    assert json.loads(sentinel.read_text(encoding="utf-8")) == {"keep": True}


def test_p95_uses_nearest_rank_for_twenty_samples():
    assert benchmark.percentiles(list(range(1, 21))) == {"count": 20, "p50Ms": 10.5, "p95Ms": 19, "maxMs": 20}


def test_remote_api_is_rejected_without_explicit_authorization(tmp_path):
    with pytest.raises(ValueError, match="paid-API authorization"):
        benchmark.run_pipeline(tmp_path, [])
    with pytest.raises(ValueError, match="allow-remote-recognition"):
        benchmark.run(SimpleNamespace(allow_remote_recognition=False))
    assert not list(tmp_path.iterdir())


def test_default_cli_plan_is_explicit_about_external_api_and_does_not_write(tmp_path, monkeypatch, capsys):
    destination = tmp_path / "not-created"
    monkeypatch.setattr(benchmark.sys, "argv", ["benchmark-processing.py", "--output", str(destination)])
    benchmark.main()
    plan = json.loads(capsys.readouterr().out)
    assert plan["usesExternalRecognitionApi"] is True and plan["mayIncurApiCharges"] is True
    assert plan["requiredExecutionFlags"] == ["--execute", "--allow-remote-recognition"]
    assert not destination.exists()

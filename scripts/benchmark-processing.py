"""Measure a synthetic, single-consumer pipeline without opening user data or AWS.

The CLI uses the production remote recognition runner only with an explicit
paid-API opt-in. Test-only callers inject an offline runner into run_pipeline;
such reports are explicitly labelled and do not measure model accuracy.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib import metadata, util
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
for location in (ROOT, ROOT / "backend/tests"):
    if str(location) not in sys.path:
        sys.path.insert(0, str(location))

USER_ID = "user-alice"
ACTIVE = {"queued", "ocr", "poster"}


def source_hashes():
    files = {*ROOT.joinpath("backend").glob("*.py"), *ROOT.joinpath("backend/services").glob("*.py"),
             *ROOT.joinpath("backend").glob("requirements*.txt")}
    files.update(ROOT / path for path in ("scripts/benchmark-processing.py",
                 "scripts/synthetic-tickets.py", "backend/tests/account_fixtures.py", "backend/tests/test_object_storage.py"))
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            for path in sorted(files)}


def percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "p50Ms": None, "p95Ms": None, "maxMs": None}
    return {"count": len(ordered), "p50Ms": statistics.median(ordered),
            "p95Ms": ordered[math.ceil(len(ordered) * .95) - 1], "maxMs": ordered[-1]}


class CommitObserver:
    """Do no SQL/I/O in Store callbacks; preserve the committed cursor intervals."""

    def __init__(self, baseline):
        self.baseline = baseline
        self.events = []
        self.lock = threading.Lock()
        self.waking = threading.Event()
        self.overflow = False

    def observe(self, user_id, cursor):
        if user_id != USER_ID:
            return
        stamp = time.perf_counter_ns()
        with self.lock:
            if len(self.events) >= 10_000:
                self.overflow = True
            else:
                self.events.append((cursor, stamp))
        self.waking.set()

    def materialize(self, store, origin_ns):
        with self.lock:
            events, overflow = tuple(self.events), self.overflow
        if overflow:
            raise RuntimeError("Post-commit observer overflow")
        with store.transaction() as db:
            changes = [dict(row) for row in db.execute(
                "SELECT cursor,ticket_id,payload FROM ticket_changes WHERE user_id=? AND cursor>? ORDER BY cursor",
                (USER_ID, self.baseline))]
        expected = list(range(self.baseline + 1, self.baseline + len(changes) + 1))
        if [row["cursor"] for row in changes] != expected:
            raise RuntimeError("Non-contiguous durable changes")
        result, previous, offset = [], self.baseline, 0
        for cursor, stamp in events:
            if cursor <= previous:
                raise RuntimeError("Out-of-order or duplicate post-commit notification")
            count = cursor - previous
            batch = changes[offset:offset + count]
            if len(batch) != count or batch[-1]["cursor"] != cursor:
                raise RuntimeError("Notification does not cover the durable log")
            for row in batch:
                payload = json.loads(row["payload"])
                result.append({"cursor": row["cursor"], "ticketId": row["ticket_id"],
                               "status": payload["processing"]["status"],
                               "observedCommitMs": (stamp - origin_ns) / 1e6,
                               "notificationCursor": cursor, "notificationBatchSize": count})
            offset += count
            previous = cursor
        if offset != len(changes):
            raise RuntimeError("Durable changes missing post-commit observations")
        return result


class SyntheticPoster:
    """A local generated image; Commons-shaped metadata is only a fixture."""

    def __init__(self):
        from account_fixtures import image_bytes
        from backend.posters import PosterCandidate
        self.candidate = PosterCandidate(content=image_bytes("yellow"), mime_type="image/png", width=9, height=7,
            source_page="https://commons.wikimedia.org/wiki/File:Synthetic_benchmark_fixture.png",
            source_file="File:Synthetic benchmark fixture.png", license_name="Public domain",
            license_url="https://commons.wikimedia.org/wiki/File:Synthetic_benchmark_fixture.png#Licensing",
            author="Synthetic fixture, not retrieved from Commons", fetched_at=int(time.time() * 1000))
        self.calls, self.closed = 0, False

    def availability(self):
        return {"available": True, "reason": None}

    def find(self, title, showtime="", *, stopping=lambda: False):
        self.calls += 1
        return self.candidate

    def close(self):
        self.closed = True


class MemorySampler:
    def __init__(self):
        import psutil
        self.psutil = psutil
        self.process = psutil.Process()
        self.stopping = threading.Event()
        self.samples, self.peak, self.max_processes, self.errors = 0, 0, 0, 0
        self.baseline = self.process.memory_info().rss
        self.thread = threading.Thread(target=self._loop, name="benchmark-owned-memory", daemon=True)

    def _loop(self):
        while not self.stopping.is_set():
            processes = [self.process, *self.process.children(recursive=True)]
            total, alive = 0, 0
            for process in processes:
                try:
                    total += process.memory_info().rss
                    alive += 1
                except (self.psutil.NoSuchProcess, self.psutil.AccessDenied):
                    self.errors += 1
            self.samples += 1
            self.peak = max(self.peak, total)
            self.max_processes = max(self.max_processes, alive)
            self.stopping.wait(.02)

    def finish(self):
        self.stopping.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            raise RuntimeError("Memory sampler did not stop")
        return {"baselineParentRssBytes": self.baseline, "peakSummedRssBytes": self.peak,
                "samples": self.samples, "maxProcessCount": self.max_processes, "unreadableProcessSamples": self.errors,
                "method": "20 ms sampling of this benchmark Python process and descendants during consumption; summed RSS can double-count shared pages"}


def run_pipeline(parent, inputs, *, recognition_runner=None, timeout=540, allow_remote_recognition=False):
    """Inputs are synthetic PNG bytes with labels; never writes ground truth fields."""
    if recognition_runner is None and not allow_remote_recognition:
        raise ValueError("Remote recognition requires explicit paid-API authorization")
    import fakeredis
    from fastapi.testclient import TestClient
    from backend.app import create_app
    from backend.object_storage import S3ObjectStore
    from backend.processing import ProcessingWorker, run_isolated_recognition
    from backend.shared_state import RedisSharedState
    from account_fixtures import FakeAuthFactory, ORIGIN, WRITE_HEADERS, signed_in
    from test_object_storage import FakeS3Client

    if not 1 <= len(inputs) <= 20 or timeout <= 0:
        raise ValueError("Expected 1..20 synthetic inputs and a positive timeout")
    digests = [hashlib.sha256(sample["raw"]).hexdigest() for sample in inputs]
    if len(set(digests)) != len(digests):
        raise ValueError("Each input must have distinct bytes for unambiguous recognition timing")
    origin_ns, attempts = time.perf_counter_ns(), []
    runner = recognition_runner or run_isolated_recognition

    def timed_recognition(raw, **kwargs):
        attempt = {"sha256": hashlib.sha256(raw).hexdigest(), "startedMs": (time.perf_counter_ns() - origin_ns) / 1e6,
                   "success": False}
        try:
            result = runner(raw, **kwargs)
            attempt["success"] = True
            return result
        finally:
            attempt["finishedMs"] = (time.perf_counter_ns() - origin_ns) / 1e6
            attempt["elapsedMs"] = attempt["finishedMs"] - attempt["startedMs"]
            attempts.append(attempt)

    provider, fake_s3 = SyntheticPoster(), FakeS3Client()
    fake_redis = fakeredis.FakeRedis()
    shared = RedisSharedState(client=fake_redis)

    def factory(store, cache):
        return ProcessingWorker(store, cache, provider=provider, recognition_runner=timed_recognition,
                                recognition_status={"available": True} if recognition_runner else None)

    with tempfile.TemporaryDirectory(prefix="processing-benchmark-", dir=parent) as scratch:
        workspace = Path(scratch).resolve()
        try:
            app = create_app(workspace / "account", auth_provider=FakeAuthFactory(), shared_state=shared,
                object_store=S3ObjectStore("synthetic-benchmark", "ap-northeast-1", client=fake_s3),
                run_processing=False, run_recovery=False, processing_factory=factory)
            with TestClient(app, base_url=ORIGIN) as client:
                signed_in(client)
                first = client.get("/api/sync/snapshot")
                if first.status_code != 200 or not first.json()["capabilities"]["processing"]:
                    raise RuntimeError("Pipeline is not available")
                store, worker = app.state.store, app.state.processing_worker
                uploads, admissions = [], []
                for index, sample in enumerate(inputs):
                    started = time.perf_counter_ns()
                    response = client.post("/api/media", headers=WRITE_HEADERS,
                        data={"uploadId": f"benchmark-upload-{index}"},
                        files={"file": ("synthetic.png", sample["raw"], "image/png")})
                    if response.status_code != 200 or response.json()["assetId"] != digests[index]:
                        raise RuntimeError("Synthetic upload failed integrity validation")
                    uploads.append({"sha256": digests[index], "bytes": len(sample["raw"]),
                                    "elapsedMs": (time.perf_counter_ns() - started) / 1e6})
                observer = CommitObserver(store.watermark(USER_ID)["cursor"])
                unsubscribe = store.subscribe_changes(observer.observe)
                try:
                    for index, digest in enumerate(digests):
                        started = time.perf_counter_ns()
                        response = client.post("/api/tickets", headers=WRITE_HEADERS, json={
                            "id": f"benchmark-ticket-{index}", "operationId": f"benchmark-create-{index}",
                            "originalAssetId": digest, "fields": {}})
                        if response.status_code != 200:
                            raise RuntimeError("Synthetic ticket admission failed")
                        admissions.append({"ticketId": f"benchmark-ticket-{index}", "sha256": digest,
                            "committedCursor": response.json()["committedCursor"],
                            "elapsedMs": (time.perf_counter_ns() - started) / 1e6})
                    sampler = MemorySampler()
                    sampler.thread.start()
                    consumer_started = (time.perf_counter_ns() - origin_ns) / 1e6
                    try:
                        worker.start()
                        deadline = time.monotonic() + timeout
                        while True:
                            observer.waking.clear()
                            with store.transaction() as db:
                                active = db.execute("SELECT COUNT(*) FROM processing_jobs WHERE status IN ('queued','ocr','poster')").fetchone()[0]
                            if active == 0:
                                break
                            if time.monotonic() >= deadline or not worker.thread.is_alive():
                                raise RuntimeError("Pipeline incomplete at benchmark deadline or worker exit")
                            observer.waking.wait(min(1, max(.01, deadline - time.monotonic())))
                    finally:
                        worker.stop()
                        memory = sampler.finish()
                    if worker.thread.is_alive():
                        raise RuntimeError("Benchmark worker did not stop")
                    events = observer.materialize(store, origin_ns)
                    with store.transaction() as db:
                        jobs = [dict(row) for row in db.execute("SELECT * FROM processing_jobs ORDER BY created_at,id")]
                        final = {row["id"]: {"fields": json.loads(row["fields"]), "fieldSources": json.loads(row["field_sources"])}
                                 for row in db.execute("SELECT id,fields,field_sources FROM tickets")}
                    report = summarize(jobs, events, admissions, attempts, inputs, final, consumer_started)
                    report.update({"uploadsBeforeAdmission": uploads, "admissions": admissions,
                                   "memory": memory, "posterFixtureCalls": provider.calls,
                                   "syntheticObjectWrites": fake_s3.writes,
                                   "recognitionMode": "offline-test-injected" if recognition_runner else "production-remote-api-isolated-per-attempt"})
                finally:
                    unsubscribe()
        finally:
            fake_redis.close()
    report["checks"].update({"temporaryAccountRemoved": not workspace.exists(), "posterProviderClosed": provider.closed})
    return report


def summarize(jobs, events, admissions, attempts, inputs, final, consumer_started):
    if len(jobs) != len(inputs) or len(admissions) != len(inputs):
        raise RuntimeError("Incomplete job admission")
    by_ticket = {row["ticketId"]: row for row in admissions}
    rows = []
    for job in jobs:
        ticket_id = job["ticket_id"]
        history = [event for event in events if event["ticketId"] == ticket_id]
        if not history or history[0]["status"] != "queued" or history[-1]["status"] != job["status"] or job["status"] in ACTIVE:
            raise RuntimeError("Incomplete pipeline lifecycle")
        queued, finished = history[0]["observedCommitMs"], history[-1]["observedCommitMs"]
        claims = [event for event in history if event["status"] in {"ocr", "poster"}]
        first_claim = claims[0]["observedCommitMs"] if claims else None
        digest = by_ticket[ticket_id]["sha256"]
        sample = inputs[int(ticket_id.removeprefix("benchmark-ticket-"))]
        if any(source == "user" for source in final[ticket_id]["fieldSources"].values()):
            raise RuntimeError("Ground-truth fields contaminated recognition processing")
        job_attempts = [attempt for attempt in attempts if attempt["sha256"] == digest]
        if job["attempts"] and not job_attempts:
            raise RuntimeError("A claimed job has no recognition timing")
        rows.append({"ticketId": ticket_id, "sha256": digest, "status": job["status"],
                     "attempts": job["attempts"], "retryCount": max(0, job["attempts"] - 1),
                     "admissionBatchWaitMs": consumer_started - queued,
                     "postConsumerToFirstClaimMs": None if first_claim is None else first_claim - consumer_started,
                     "queuedToFirstClaimMs": None if first_claim is None else first_claim - queued,
                     "firstClaimToTerminalMs": None if first_claim is None else finished - first_claim,
                     "queuedToTerminalMs": finished - queued,
                     "recognitionAttempts": job_attempts, "events": history,
                     "expected": sample["expected"], "actual": final[ticket_id]["fields"]})
    if any(value < 0 for row in rows for key, value in row.items() if key.endswith("Ms") and value is not None):
        raise RuntimeError("Negative lifecycle duration")
    accuracy = {}
    for field in ("title", "cinema", "hall", "seat", "showtime"):
        present = [row for row in rows if field in row["expected"]]
        exact = sum(row["actual"].get(field) == row["expected"][field] for row in present)
        wrong = sum(bool(row["actual"].get(field)) and row["actual"][field] != row["expected"][field] for row in present)
        accuracy[field] = {"expectedPresent": len(present), "exact": exact, "wrongNonempty": wrong,
                           "missed": len(present) - exact - wrong}
    timings = {key: percentiles([row[key] for row in rows if row[key] is not None]) for key in (
        "admissionBatchWaitMs", "postConsumerToFirstClaimMs", "queuedToFirstClaimMs",
        "firstClaimToTerminalMs", "queuedToTerminalMs")}
    timings["recognitionAttemptMs"] = percentiles([row["elapsedMs"] for row in attempts])
    counts = dict(Counter(row["status"] for row in rows))
    return {"consumerStartedMs": consumer_started, "samples": rows, "timings": timings,
            "statusCounts": counts, "failureRate": counts.get("failed", 0) / len(rows), "fieldAccuracy": accuracy,
            "checks": {"completeJobCount": len(rows), "durableChangesObserved": len(events),
                       "observerOverflow": False, "groundTruthNeverSubmitted": True,
                       "retries": sum(row["retryCount"] for row in rows)}}


def safe_destination(path):
    if not path.is_absolute() or (os.name == "nt" and path.drive.upper() != "D:"):
        raise ValueError("Use an absolute D: output path on Windows")
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
            raise ValueError("Output path must not traverse links or junctions")
    destination = path.resolve()
    if destination.exists() or not destination.parent.is_dir():
        raise ValueError("Output must be a new directory under an existing parent")
    return destination


def run(args):
    if not args.allow_remote_recognition:
        raise ValueError("Add --allow-remote-recognition to authorize sending synthetic images to the paid API")
    destination = safe_destination(args.output)
    before = source_hashes()
    font = args.font.resolve(strict=True)
    from backend.ticket_recognition import availability
    if not availability()["available"]:
        raise RuntimeError("Remote recognition configuration is unavailable")
    generator_spec = util.spec_from_file_location("synthetic_ticket_generator", ROOT / "scripts/synthetic-tickets.py")
    generator = util.module_from_spec(generator_spec)
    generator_spec.loader.exec_module(generator)
    destination.mkdir()
    with tempfile.TemporaryDirectory(prefix="synthetic-inputs-", dir=destination) as scratch:
        folder = Path(scratch)
        labels = [sample for sample in generator.generate(folder, font) if sample["condition"] == "clean"]
        if len(labels) != 20:
            raise RuntimeError("Expected exactly 20 clean labelled synthetic tickets")
        inputs = [{**sample, "raw": (folder / sample["path"]).read_bytes()} for sample in labels]
        report = run_pipeline(destination, inputs, allow_remote_recognition=True)
    after = source_hashes()
    if before != after:
        raise RuntimeError("Measured sources changed during this run")
    report.update({"schemaVersion": 2, "createdAt": datetime.now(timezone.utc).isoformat(),
        "sourceHashes": before, "sourceHashMethod": "SHA-256 after CRLF-to-LF normalization", "sourcesStable": True,
        "dataset": labels, "environment": {"python": sys.version, "platform": platform.platform(),
            "cpuCount": os.cpu_count(), "processor": platform.processor(), "fontName": font.name,
            "fontSha256": hashlib.sha256(font.read_bytes()).hexdigest(),
            "recognitionModel": os.environ["KEEPSITE_QWEN_MODEL"],
            "gitBaseHead": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "installedVersions": {name: metadata.version(name) for name in (
                "Pillow", "psutil", "fastapi", "starlette", "pydantic", "redis", "fakeredis", "httpx")}},
        "method": {"sampleCount": 20, "consumerCount": 1,
            "admission": "All synthetic originals uploaded, then 20 empty-field tickets admitted serially before consumer starts",
            "timing": "perf_counter_ns in Store post-commit notifications; one callback's final cursor covers every change in that transaction; observer overhead included",
            "queueWait": "Initial queued commit to first durable claim; includes remaining batch admission, reconciliation, Redis delivery and SQL claim, not pure Redis queue latency",
            "recognition": "Production spawn per attempt, including image preparation, HTTPS API request/response, validation and child cleanup; no local inference or model loading",
            "percentiles": "Empirical median p50 and nearest-rank ceil(0.95*n) p95; single run, no confidence or capacity claim",
            "fixtures": "In-process authenticated ASGI HTTP, fakeredis Streams, S3ObjectStore with in-memory FakeS3Client, local generated poster with fixture metadata",
            "limits": "Recognition sends synthetic images to the configured external API and may incur charges; no token/cost accounting, real-ticket accuracy, real Cognito, S3, Redis server, poster retrieval, physical devices or EC2 capacity measured"}})
    (destination / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(destination / "report.json"), "statusCounts": report["statusCounts"],
                      "checks": report["checks"], "timings": report["timings"]}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--font", type=Path, default=Path("C:/Windows/Fonts/msyh.ttc"))
    parser.add_argument("--allow-remote-recognition", action="store_true",
                        help="Explicitly authorize sending the 20 synthetic inputs to the configured paid recognition API")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    destination = safe_destination(args.output)
    if not args.execute:
        print(json.dumps({"plan": "20 clean synthetic tickets, remote recognition API, single production consumer, owned scratch cleanup",
                          "output": str(destination), "usesAws": False, "writesUserData": False,
                          "usesExternalRecognitionApi": True, "mayIncurApiCharges": True,
                          "requiredExecutionFlags": ["--execute", "--allow-remote-recognition"]}, ensure_ascii=False))
        return
    run(args)


if __name__ == "__main__":
    main()

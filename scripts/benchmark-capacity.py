"""Offline synthetic /api/state baseline; never opens an existing workspace.

Run with the project's development Python dependencies. All SQLite, template and
object files live in a newly owned TemporaryDirectory below --temp-root. This is
an in-process, serial TestClient measurement, not a network or browser load test.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.generation.repository import enqueue
from backend.object_storage import LocalObjectStore
from backend.store import encode
from backend.tests.account_fixtures import FakeAuthFactory, ORIGIN, USERS, image_bytes, signed_in, template_fixture


PROFILES = ("ready-shared-model", "queued-unique-originals")
CATEGORIES = ("movie", "travel", "daily", "exhibition")
USER_ID = USERS["alice"]["id"]
ENGINE = {"available": False, "reason": "Synthetic capacity baseline: inference disabled.", "pipeline": "not-run"}
LIMITATIONS = [
    "Serial in-process TestClient requests: no real network, TLS, Cognito, S3 or concurrent traffic.",
    "No browser was started. Image references and byte totals are derived from the current list code and synthetic asset metadata.",
    "Images are tiny synthetic 9x7 PNGs; their byte totals do not represent user photographs or measured browser transfer/decode cost.",
    "Ready records share one synthetic template model and source image; queued records have distinct originals and unstarted jobs.",
    "Warm request timings exclude fixture creation, account initialization, seeding and SQL trace instrumentation.",
    "No backups are created or read. No model inference, cloud authentication or object storage calls are performed.",
    "Local measurements are not a t3.small capacity guarantee; no EC2 CPU credits, memory limits or real workload are represented.",
]


def _absolute_path(value: Path, label: str) -> Path:
    value = Path(value)
    if not value.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if os.name == "nt" and value.drive.upper() != "D:":
        raise ValueError(f"{label} must be on D: on this Windows workspace")
    for parent in (value, *value.parents):
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError(f"{label} must not traverse a symbolic link or junction")
    return value.resolve()


def _scratch_root(value: Path) -> Path:
    root = _absolute_path(value, "temp-root")
    if root.exists() and not root.is_dir():
        raise ValueError("temp-root must be a directory")
    # Never accept a workspace itself, even though only a new child is used.
    if (root / "memoverse.sqlite3").exists() or (root / "workspace" / "memoverse.sqlite3").exists():
        raise ValueError("temp-root must not be an existing account workspace")
    return root


def _seed(store, manifest: dict, count: int, profile: str) -> dict[str, int]:
    """Prepare valid synthetic rows once; setup cost is outside the measurement."""
    asset = manifest["assets"][0]
    model_id = manifest["models"][0]["id"]
    assets = []
    if profile == "queued-unique-originals":
        for index in range(count):
            # Distinct RGB colors give distinct bytes/content IDs without random
            # inputs. None of these colors is the template's pure blue.
            raw = image_bytes((index & 255, (index >> 8) & 255, 127))
            identifier = hashlib.sha256(raw).hexdigest()
            descriptor = {"id": identifier, "path": f"media/{identifier}.png", "mimeType": "image/png",
                          "size": len(raw), "width": 9, "height": 7, "sha256": identifier}
            store.storage.put_bytes(descriptor["path"], raw, identifier, descriptor["mimeType"])
            assets.append(descriptor)
    else:
        assets = [asset] * count

    with store.transaction(True) as db:
        # Only this script's fresh template account exists in this directory.
        db.execute("DELETE FROM collections WHERE user_id=?", (USER_ID,))
        for index, descriptor in enumerate(assets):
            if profile == "queued-unique-originals":
                store.register_asset(db, descriptor)
                db.execute("INSERT INTO asset_grants VALUES (?,?)", (USER_ID, descriptor["id"]))
            identifier = f"capacity-{index:06d}"
            payload = {"id": identifier, "title": f"合成收藏 {index:06d}", "category": CATEGORIES[index % 4],
                       "tags": ["容量基线", "合成素材"], "note": "固定长度的合成备注，仅用于本机容量基线。",
                       "date": "2026-10-18", "addedAt": index, "source": "mine", "art": "cafe"}
            db.execute("INSERT INTO collections(user_id,id,payload,asset_id,model_id,version) VALUES (?,?,?,?,?,?)",
                       (USER_ID, identifier, encode(payload), descriptor["id"], model_id if profile == "ready-shared-model" else None, 1))
            if profile == "queued-unique-originals":
                enqueue(store, db, USER_ID, identifier, descriptor["id"], 1)
        store.bump(db, USER_ID)
        return {row["id"]: row["size"] for row in db.execute("SELECT id,size FROM assets")}


def _checked_response(response, count: int, expected_body: bytes | None = None) -> dict:
    if response.status_code != 200:
        raise RuntimeError(f"Synthetic state request failed with HTTP {response.status_code}")
    body = response.json()
    if not body.get("initialized") or body.get("userId") != USER_ID:
        raise RuntimeError("Synthetic account identity or initialization changed")
    items = body["data"]["collections"]
    if len(items) != count or len({item["id"] for item in items}) != count:
        raise RuntimeError("Synthetic collection count or identities do not match the requested case")
    if expected_body is not None and response.content != expected_body:
        raise RuntimeError("A read changed the state response during the measurement")
    return body


def _row_counts(store) -> dict[str, int]:
    tables = ("users", "profiles", "assets", "models", "asset_grants", "model_grants", "collections", "shares", "community", "likes", "jobs")
    with store.transaction() as db:
        return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in tables}


def _trace_request(client, store, count: int, expected_body: bytes) -> dict[str, int]:
    counts: Counter[str] = Counter()
    lock = threading.Lock()
    original_connect = store.connect

    def trace(statement):
        # Aggregate only; never retain expanded statements or data in a report.
        kind = statement.lstrip().split(None, 1)[0].upper()
        with lock:
            counts[kind] += 1

    def connect(**kwargs):
        connection = original_connect(**kwargs)
        connection.set_trace_callback(trace)
        return connection

    with patch.object(store, "connect", side_effect=connect):
        _checked_response(client.get("/api/state"), count, expected_body)
    return dict(sorted(counts.items()))


def _image_resources(items: list[dict], asset_sizes: dict[str, int]) -> dict:
    urls = [item["customImage"] for item in items if item.get("customImage")]
    unique = set(urls)
    sizes = {url: asset_sizes[url.rsplit("/", 1)[1]] for url in unique}
    return {"image_references": len(urls), "unique_media_urls": len(unique),
            "unique_synthetic_image_bytes": sum(sizes.values()),
            "synthetic_bytes_per_reference_sum": sum(sizes[url] for url in urls)}


def _list_resources(data: dict, asset_sizes: dict[str, int]) -> dict:
    """Code-derived card selections, not browser request/transfer measurements."""
    items = data["collections"]
    recent = sorted(items, key=lambda item: item["addedAt"], reverse=True)
    by_category = {category: [item for item in items if item["category"] == category] for category in CATEGORIES}
    home = recent[:3] + [item for category in ("movie", "daily", "travel", "exhibition") for item in by_category[category][:8]]
    return {
        "method": "code-derived; no browser, HTTP image fetch or decode measured",
        "source_files": ["demo/src/Prototype.tsx", "demo/src/features/collections/PhotoCard.tsx", "demo/src/art.tsx", "backend/api.py"],
        "image_policy": "customImage uses original /api/media URLs without srcset, a thumbnail endpoint or loading=lazy",
        "home": _image_resources(home, asset_sizes),
        "categories": {category: _image_resources(rows, asset_sizes) for category, rows in by_category.items()},
        "search_all": _image_resources(items, asset_sizes),
        "month_2026_10": _image_resources(items, asset_sizes),
        "all_state_content": _image_resources([*items, *[share["item"] for share in data["shares"]], *data["community"]], asset_sizes),
    }


def _sample_case(root: Path, profile: str, count: int, warmup: int, samples: int, sql_samples: int) -> dict:
    template = root / "template"
    manifest = template_fixture(template)
    storage = LocalObjectStore(root / "objects")
    # Fixed engine metadata avoids reading model caches. These guards fail closed
    # if a future refactor accidentally starts work or falls back to cloud storage.
    with ExitStack() as guards:
        guards.enter_context(patch("backend.state.engine_status", return_value=ENGINE.copy()))
        for target in ("backend.jobs.JobWorker.start", "backend.jobs.prepare_model", "backend.app.configured_store", "backend.api.create_account_backup"):
            guards.enter_context(patch(target, side_effect=AssertionError("Capacity baseline must not start a worker, use cloud defaults or create a backup")))
        app = create_app(root / "workspace", run_worker=False, auth_provider=FakeAuthFactory(),
                         object_store=storage, template_dir=template)
        store = app.state.store
        with TestClient(app, base_url=ORIGIN) as client:
            signed_in(client)
            _checked_response(client.get("/api/state"), 1)
            asset_sizes = _seed(store, manifest, count, profile)
            baseline = client.get("/api/state")
            state = _checked_response(baseline, count)
            before = _row_counts(store)
            for _ in range(warmup):
                _checked_response(client.get("/api/state"), count, baseline.content)
            timings = []
            for _ in range(samples):
                started = perf_counter()
                response = client.get("/api/state")
                timings.append((perf_counter() - started) * 1000)
                _checked_response(response, count, baseline.content)
            traces = [_trace_request(client, store, count, baseline.content) for _ in range(sql_samples)]
            if any(set(trace) - {"SELECT", "BEGIN", "COMMIT"} for trace in traces):
                raise RuntimeError("Unexpected non-read SQL in the state baseline")
            after = _row_counts(store)
            if before != after or app.state.worker.thread.is_alive():
                raise RuntimeError("State measurement changed rows or started a worker")
    ordered = sorted(timings)
    return {
        "profile": profile, "collections": count, "shares": len(state["data"]["shares"]), "community": len(state["data"]["community"]),
        "warmup_requests": warmup, "timed_requests": samples, "successful_timed_requests": samples,
        "response_body_bytes": len(baseline.content), "response_content_encoding": baseline.headers.get("content-encoding", "identity"),
        "latency_ms": {"p50": round(statistics.median(timings), 4), "p95": round(ordered[math.ceil(samples * .95) - 1], 4), "max": round(max(timings), 4)},
        "sql": {"samples": sql_samples, "select_min": min(trace.get("SELECT", 0) for trace in traces),
                "select_max": max(trace.get("SELECT", 0) for trace in traces),
                "statement_min": min(sum(trace.values()) for trace in traces), "statement_max": max(sum(trace.values()) for trace in traces),
                "statements_by_kind": traces,
                "scope": "separate untimed HTTP reads including FakeAuth account lookup; excludes connect-time PRAGMA foreign_keys and fixture setup"},
        "read_checks": {"snapshot_and_revision_unchanged": True, "row_counts_unchanged": True,
                        "traced_write_statements": 0, "worker_started": False},
        "row_counts": after,
        "lists": _list_resources(state["data"], asset_sizes),
    }


def _git_metadata() -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, timeout=5, check=True)
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=ROOT,
                                capture_output=True, text=True, timeout=5, check=True)
        return {"git_commit": commit.stdout.strip(), "git_worktree_dirty": bool(status.stdout.strip())}
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "git_worktree_dirty": None}


def run_benchmark(temp_root: Path, *, counts=(100, 1000), profiles=PROFILES, warmup=5, samples=30, sql_samples=3) -> dict:
    if not counts or len(set(counts)) != len(counts) or any(type(count) is not int or not 1 <= count <= 65536 for count in counts):
        raise ValueError("counts must be distinct integers between 1 and 65536")
    if not profiles or len(set(profiles)) != len(profiles) or any(profile not in PROFILES for profile in profiles):
        raise ValueError("profiles must be distinct supported profile names")
    if type(warmup) is not int or warmup < 0 or type(samples) is not int or samples < 1 or type(sql_samples) is not int or sql_samples < 1:
        raise ValueError("warmup must be nonnegative; samples and sql-samples must be positive integers")
    parent = _scratch_root(temp_root)
    parent.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="capacity-owned-", dir=parent) as temporary:
        owned = Path(temporary).resolve()
        if owned.parent != parent or not owned.name.startswith("capacity-owned-"):
            raise RuntimeError("Unexpected temporary directory ownership")
        for profile in profiles:
            for count in counts:
                results.append(_sample_case(owned / f"{profile}-{count}", profile, count, warmup, samples, sql_samples))
    return {
        "schema_version": 1, "measured_at_utc": datetime.now(timezone.utc).isoformat(),
        "environment": {**_git_metadata(), "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
                        "platform": platform.platform(), "logical_cpu_count": os.cpu_count(),
                        "fastapi": version("fastapi"), "httpx": version("httpx")},
        "method": {"endpoint": "GET /api/state", "transport": "in-process TestClient", "concurrency": 1,
                   "p95_method": "nearest rank", "timing": "client.get through complete response body; validation occurs after the timer",
                   "sql_in_timing": False, "synthetic_image_dimensions": [9, 7], "temporary_data_cleaned": True},
        "limitations": LIMITATIONS,
        "cases": results,
    }


def write_report(path: Path, report: dict) -> None:
    path = _absolute_path(path, "output")
    if path.suffix.lower() != ".json":
        raise ValueError("output must be a new .json file")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Never replace previous evidence or an existing file.
    with path.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--temp-root", required=True, type=Path, help="Absolute scratch parent (D: on Windows); only fresh owned children are used/removed")
    parser.add_argument("--output", required=True, type=Path, help="New absolute .json report path; existing files are never replaced")
    parser.add_argument("--counts", nargs="+", type=int, default=[100, 1000])
    parser.add_argument("--profiles", nargs="+", choices=PROFILES, default=list(PROFILES))
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--samples", type=int, default=30)
    parser.add_argument("--sql-samples", type=int, default=3)
    args = parser.parse_args(argv)
    output = _absolute_path(args.output, "output")
    if output.exists() or output.suffix.lower() != ".json":
        parser.error("output must be a new .json file")
    report = run_benchmark(args.temp_root, counts=args.counts, profiles=args.profiles,
                           warmup=args.warmup, samples=args.samples, sql_samples=args.sql_samples)
    write_report(output, report)
    print("profile                        count    body bytes   p50 ms   p95 ms   max ms   SELECTs")
    for case in report["cases"]:
        latency, sql = case["latency_ms"], case["sql"]
        print(f"{case['profile']:<30} {case['collections']:>5} {case['response_body_bytes']:>13}"
              f" {latency['p50']:>8.2f} {latency['p95']:>8.2f} {latency['max']:>8.2f} {sql['select_min']:>5}-{sql['select_max']}")
    print(f"Report: {output}")
    print("Synthetic local baseline only; not browser/network performance or an EC2 capacity guarantee.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

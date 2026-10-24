"""Compare explicit historical polling and current incremental HTTP protocols.

Each invocation uses one source tree and a new owned synthetic account. Run the
two protocols in separate processes. The workload has twelve logical polling
opportunities and three batches of remote changes; it does not sleep or pretend
to measure browser/WebSocket scheduling, network traffic, or EC2 capacity.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
from time import perf_counter
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
USER_ID = "user-alice"
PROTOCOLS = ("full-poll", "incremental")


def checked_path(value, label):
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    if os.name == "nt" and path.drive.upper() != "D:":
        raise ValueError(f"{label} must use D:")
    for parent in (path, *path.parents):
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError(f"{label} must not traverse links")
    return path.resolve()


def source_hashes(source):
    files = [*source.joinpath("backend").glob("*.py"), *source.joinpath("backend/services").glob("*.py"),
             *source.joinpath("backend").glob("requirements*.txt"), source / "backend/tests/account_fixtures.py"]
    return {path.relative_to(source).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(files)}


class Meter:
    def __init__(self, client):
        self.client = client
        self.rows = defaultdict(list)

    def request(self, category, method, path, **kwargs):
        started = perf_counter()
        response = self.client.request(method, path, **kwargs)
        elapsed = (perf_counter() - started) * 1000
        self.rows[category].append({"bytes": len(response.content), "milliseconds": elapsed,
                                    "status": response.status_code,
                                    "encoding": response.headers.get("content-encoding", "identity")})
        if response.status_code != 200:
            raise RuntimeError(f"Synthetic {category} request failed: HTTP {response.status_code}")
        return response.json()

    def report(self):
        result = {}
        for category, rows in sorted(self.rows.items()):
            ordered = sorted(row["milliseconds"] for row in rows)
            result[category] = {"requests": len(rows), "responseBodyBytes": sum(row["bytes"] for row in rows),
                                "failedRequests": sum(row["status"] != 200 for row in rows),
                                "contentEncodings": sorted({row["encoding"] for row in rows}),
                                "latencyMilliseconds": {"p50": statistics.median(ordered),
                                                        "p95": ordered[math.ceil(len(ordered) * .95) - 1]}}
        return result


def full_snapshot(meter, category):
    body = meter.request(category, "GET", "/api/state")
    if body["userId"] != USER_ID or not body["initialized"]:
        raise RuntimeError("Historical snapshot identity mismatch")
    return {row["id"]: row for row in body["data"]["tickets"]}, body["revision"], body["serverId"]


def paged_snapshot(meter, category):
    tickets, token, identity = {}, None, None
    while True:
        params = {"limit": 100}
        if token is not None:
            params["pageToken"] = token
        body = meter.request(category, "GET", "/api/sync/snapshot", params=params)
        current = (body["serverId"], body["userId"], body["snapshotId"], body["cursor"], body["total"])
        identity = identity or current
        if identity != current or body["userId"] != USER_ID or body["offset"] != len(tickets):
            raise RuntimeError("Frozen snapshot identity/offset mismatch")
        for ticket in body["tickets"]:
            if ticket["id"] in tickets:
                raise RuntimeError("Duplicate ticket across snapshot pages")
            tickets[ticket["id"]] = ticket
        next_token = body["nextPageToken"]
        if next_token is None:
            if len(tickets) != body["total"]:
                raise RuntimeError("Incomplete snapshot")
            return tickets, body["cursor"], body["serverId"]
        if not body["tickets"] or next_token == token or len(tickets) >= body["total"]:
            raise RuntimeError("Snapshot failed to advance")
        token = next_token


def catch_up(meter, tickets, cursor, server_id, barrier, *, category="steadySync"):
    while True:
        body = meter.request(category, "GET", "/api/sync/changes",
                             params={"serverId": server_id, "after": cursor, "limit": 100})
        if body["userId"] != USER_ID or body["serverId"] != server_id or body["fromCursor"] != cursor:
            raise RuntimeError("Incremental identity/cursor mismatch")
        for change in body["changes"]:
            if change["cursor"] != cursor + 1:
                raise RuntimeError("Non-contiguous incremental history")
            if change["type"] == "upsert":
                tickets[change["ticket"]["id"]] = change["ticket"]
            elif change["type"] == "delete":
                tickets.pop(change["id"], None)
            else:
                raise RuntimeError("Unknown change type")
            cursor = change["cursor"]
        if cursor != body["nextCursor"] or cursor > body["headCursor"]:
            raise RuntimeError("Incomplete incremental page")
        if not body["hasMore"]:
            if cursor < barrier:
                raise RuntimeError("Mutation barrier was not reached")
            return cursor
        if not body["changes"]:
            raise RuntimeError("Incremental page failed to advance")


def comparable(tickets):
    return [{"id": ticket["id"], "fields": ticket["fields"], "version": ticket["version"]}
            for ticket in sorted(tickets.values(), key=lambda row: row["id"])]


def run_case(source, temp_root, protocol, count):
    if protocol not in PROTOCOLS or type(count) is not int or not 3 <= count <= 10000:
        raise ValueError("Unsupported protocol or ticket count (3..10000)")
    source = checked_path(source, "source-root")
    parent = checked_path(temp_root, "temp-root")
    if any((folder / name).exists() for folder in (parent, parent / "workspace") for name in ("keepsite.sqlite3", "memoverse.sqlite3")):
        raise ValueError("temp-root must not contain an existing account workspace")
    hashes = source_hashes(source)
    # A legacy protocol is a measurement fixture, never a production fallback.
    if protocol == "full-poll" and source == ROOT:
        raise ValueError("full-poll requires an explicit isolated historical source tree")
    loaded = sys.modules.get("backend.app")
    if loaded is not None and Path(loaded.__file__).resolve().parent.parent != source:
        raise RuntimeError("Run different source trees in separate Python processes")
    sys.path.insert(0, str(source))
    from fastapi.testclient import TestClient
    from backend.app import create_app
    from backend.object_storage import LocalObjectStore
    from backend.shared_state import RedisSharedState
    from backend.store import encode
    from backend.tests.account_fixtures import FakeAuthFactory, ORIGIN, WRITE_HEADERS, image_bytes, signed_in

    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="sync-owned-", dir=parent) as temporary, ExitStack() as guards:
        owned = Path(temporary).resolve()
        if owned.parent != parent or not owned.name.startswith("sync-owned-"):
            raise RuntimeError("Unexpected scratch ownership")
        for target in ("backend.app.configured_store", "backend.api.create_account_backup"):
            guards.enter_context(patch(target, side_effect=AssertionError("No cloud defaults or backups allowed")))
        app = create_app(owned / "workspace", auth_provider=FakeAuthFactory(),
                         object_store=LocalObjectStore(owned / "objects"), shared_state=RedisSharedState(),
                         run_recovery=False, run_processing=False)
        store = app.state.store
        store.processing_enabled = False
        with TestClient(app, base_url=ORIGIN) as client:
            signed_in(client)
            # Initialize synthetic authentication outside the measured protocol.
            if client.get("/api/auth/session").status_code not in (200, 404):
                raise RuntimeError("Synthetic account initialization failed")
            raw = image_bytes("purple")
            asset_id = hashlib.sha256(raw).hexdigest()
            descriptor = {"id": asset_id, "path": f"media/{asset_id}.png", "mimeType": "image/png",
                          "size": len(raw), "width": 9, "height": 7, "sha256": asset_id}
            store.storage.put_bytes(descriptor["path"], raw, asset_id, descriptor["mimeType"])
            expected = {}
            with store.transaction(True) as db:
                store.register_asset(db, descriptor)
                db.execute("INSERT INTO asset_grants VALUES (?,?)", (USER_ID, asset_id))
                db.execute("INSERT INTO upload_receipts VALUES (?,?)", (USER_ID, asset_id))
                for index in range(count):
                    identifier = f"sync-{index:05d}"
                    fields = {"title": f"合成电影 {index:05d}", "cinema": "合成影院", "hall": "1号厅",
                              "seat": "3排5座", "showtime": "2026-10-22T19:30"}
                    stamp = 1_700_000_000_000 + index
                    db.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,1,1,?,?,NULL,?)",
                               (USER_ID, identifier, encode(fields), encode({key: "user" for key in fields}),
                                asset_id, None, stamp, stamp, identifier))
                    expected[identifier] = {"id": identifier, "fields": fields, "version": 1}
                store.bump(db, USER_ID)
                db.execute("UPDATE sync_baselines SET cursor=(SELECT revision FROM users WHERE id=?) WHERE user_id=?",
                           (USER_ID, USER_ID))
            meter = Meter(client)
            initial = full_snapshot if protocol == "full-poll" else paged_snapshot
            tickets, cursor, server_id = initial(meter, "initialSync")
            if protocol == "incremental":
                cursor = catch_up(meter, tickets, cursor, server_id, cursor, category="initialSync")
            if comparable(tickets) != comparable(expected):
                raise RuntimeError("Initial synthetic state mismatch")
            # Three remote event batches. The middle batch contains three writes
            # while the observer is disconnected, including operation replay.
            batches = {2: [("PATCH", "sync-00000", {"fields": {"seat": "8排8座"}})],
                       6: [("PATCH", "sync-00001", {"fields": {"hall": "9号厅"}}),
                           ("PATCH", "sync-00001", {"fields": {"seat": "6排6座"}}),
                           ("DELETE", "sync-00002", {})],
                       10: [("POST", "sync-added", {"id": "sync-added", "originalAssetId": asset_id,
                                                     "fields": {"title": "新增合成电影"}})]}
            barriers, checks = [], 0
            for tick in range(1, 13):
                barrier = cursor
                for number, (method, identifier, body) in enumerate(batches.get(tick, [])):
                    payload = {**body, "operationId": f"sync-op-{tick}-{number}"}
                    url = "/api/tickets" if method == "POST" else f"/api/tickets/{identifier}"
                    response = meter.request("writerReceipts", method, url, json=payload, headers=WRITE_HEADERS)
                    if protocol == "incremental":
                        if response["operationId"] != payload["operationId"] or response["serverId"] != server_id:
                            raise RuntimeError("Operation receipt identity mismatch")
                        barrier = max(barrier, response["committedCursor"])
                    if method == "DELETE":
                        del expected[identifier]
                    elif method == "POST":
                        fields = {key: "" for key in ("title", "cinema", "hall", "seat", "showtime")}
                        fields.update(body["fields"])
                        expected[identifier] = {"id": identifier, "fields": fields, "version": 1}
                    else:
                        expected[identifier]["fields"].update(body["fields"])
                        expected[identifier]["version"] += 1
                    if tick == 6 and number == 0:
                        meter.request("writerReceipts", method, url, json=payload, headers=WRITE_HEADERS)
                if protocol == "full-poll":
                    tickets, cursor, current_server = full_snapshot(meter, "steadySync")
                    if current_server != server_id:
                        raise RuntimeError("Server identity changed")
                elif tick in batches:
                    cursor = catch_up(meter, tickets, cursor, server_id, barrier)
                    barriers.append(barrier)
                if comparable(tickets) != comparable(expected):
                    raise RuntimeError(f"Observer diverged from independent expected model at tick {tick}")
                checks += 1
            final_hash = hashlib.sha256(json.dumps(comparable(tickets), ensure_ascii=False,
                                                   sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            measured = meter.report()
    if source_hashes(source) != hashes:
        raise RuntimeError("Source changed during measurement")
    return {"protocol": protocol, "tickets": count, "sourceSha256": hashes, "measurements": measured,
            "checks": {"modelComparisons": checks + 1, "finalTickets": len(tickets),
                       "finalComparableSha256": final_hash, "observedCommitBarriers": barriers,
                       "temporaryAccountRemoved": True}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-commit", required=True, help="Exact source provenance; verified separately using recorded hashes")
    parser.add_argument("--temp-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--protocol", choices=PROTOCOLS, required=True)
    parser.add_argument("--tickets", type=int, default=300)
    args = parser.parse_args(argv)
    output = checked_path(args.output, "output")
    if output.exists() or output.suffix != ".json":
        parser.error("output must be a new .json file")
    harness_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result = run_case(args.source_root, args.temp_root, args.protocol, args.tickets)
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != harness_hash:
        raise RuntimeError("Measurement harness changed during the run")
    report = {"schemaVersion": 1, "createdAt": datetime.now(timezone.utc).isoformat(),
              "environment": {"platform": platform.platform(), "python": platform.python_version(),
                              "fastapi": version("fastapi"), "httpx": version("httpx"),
                              "declaredSourceCommit": args.source_commit,
                              "harnessSha256": harness_hash},
              "method": {"concurrency": 1, "logicalPollOpportunities": 12, "batchesOfChanges": 3,
                         "distinctWrites": 5, "operationReplays": 1, "snapshotPageLimit": 100,
                         "transport": "serial in-process TestClient", "imageFixture": "synthetic 9x7 PNG"},
              "limitations": ["Event arrival and reconnect are scripted; no browser or WebSocket is used in this HTTP protocol measurement.",
                              "The twelve opportunities model a two-minute 10-second poll interval but execute without wall-clock waiting.",
                              "No socket heartbeat/upgrade/frame, request-header, TLS, HTTP compression or media-download bytes are included.",
                              "No real Cognito, S3, Redis, network, concurrent load or OCR/poster processing; no capacity guarantee.",
                              "Initial pages can require more HTTP requests than one full snapshot. Report bootstrap separately from steady-state.",
                              "Latencies describe this one serial local run; different sample counts and warm caches do not prove a speedup."],
              "result": result}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"protocol": args.protocol, "measurements": result["measurements"], "checks": result["checks"]}, indent=2))
    print(f"Report: {output}")


if __name__ == "__main__":
    main()

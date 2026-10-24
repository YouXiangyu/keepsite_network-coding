"""Shared browser fixture isolation and measurement completeness; no cloud or live ports."""
import asyncio
from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("keepsite_browser_fixture", ROOT / "demo/tests/browser_fixture.py")
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)
TOKEN = "1" * 32
HEADERS = {"X-Keepsite-Fixture": fixture.FIXTURE_HEADER, "X-Keepsite-Fixture-Run": TOKEN}


def test_shared_fixture_preserves_m5_and_m6_manifest_checks_real_account_references(tmp_path):
    for profile in (fixture.P2P_PROFILE, fixture.benchmark_profile(3)):
        app, redis = fixture.application(tmp_path / profile.name, TOKEN, profile)
        with TestClient(app, base_url=profile.origin, client=("127.0.0.1", 12345)) as client:
            assert client.get("/__test/status").status_code == 403
            if profile.name == "benchmark":
                assert client.get("/__test/manifest", headers=HEADERS).status_code == 401
            session = client.post("/__test/session", headers=HEADERS, json={"group": "benchmark", "label": "source"})
            assert session.status_code == 200
            user = session.json()["userId"]
            if profile.name == "p2p":
                assert session.json()["media"]["width"] == 512 and session.json()["media"]["height"] == 768
                assert set(client.get("/__test/status", headers=HEADERS).json()) == {"requests"}
                assert client.get("/api/sync/snapshot").json()["total"] == 1
                continue
            manifest = client.get("/__test/manifest", headers=HEADERS).json()
            assert manifest["userId"] == user
            assert manifest["dataset"] == {"seed": 20261022, "count": 3, "width": 256, "height": 384,
                                           "totalBytes": sum(item["media"]["size"] for item in manifest["items"])}
            assert len({item["media"]["sha256"] for item in manifest["items"]}) == 3
            assert manifest["dataset"]["totalBytes"] < fixture.MAX_DATASET_BYTES
            second = client.post("/__test/session", headers=HEADERS, json={"group": "benchmark", "label": "target"})
            assert second.json()["userId"] == user
            assert client.post("/__test/phase", headers=HEADERS, json={"phaseId": "formal-1", "action": "start"}).status_code == 200
            snapshot = client.get("/api/sync/snapshot").json()
            assert snapshot["total"] == 3 and snapshot["capabilities"]["cloudStorage"] is False
            media = manifest["items"][0]["media"]
            authorized = client.get(f"/api/media/{media['assetId']}/download?secret=must-not-be-recorded").json()
            assert authorized["transport"] == "server"
            response = client.get(authorized["url"])
            assert hashlib.sha256(response.content).hexdigest() == media["sha256"]
            report = client.post("/__test/phase", headers=HEADERS, json={"phaseId": "formal-1", "action": "stop"}).json()
            assert not report["overflow"] and not report["incomplete"]
            assert len(report["requests"]) == 3
            row = next(row for row in report["requests"] if row["route"] == "/api/media/:assetId")
            assert row["bodyBytes"] == len(response.content) == media["size"]
            assert row["label"] == "target" and row["complete"] and row["status"] == 200
            assert row["durationMs"] >= 0 and row["finishedAt"] >= row["startedAt"]
            assert "secret" not in json.dumps(report) and "fixture-" not in json.dumps(report)
            client.post("/__test/session", headers=HEADERS, json={"group": "benchmark", "who": "bob", "label": "other"})
            assert client.get("/__test/manifest", headers=HEADERS).status_code == 404
            assert client.get(authorized["url"]).status_code == 404
        redis.close()


def test_phase_stop_waits_for_final_stream_chunk_and_overflow_is_explicit():
    async def run():
        metrics = fixture.PhaseMetrics(maximum=1)
        metrics.start("stream")
        entered, release = asyncio.Event(), asyncio.Event()
        async def application(_scope, _receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"one", "more_body": True})
            entered.set()
            await release.wait()
            await send({"type": "http.response.body", "body": b"two", "more_body": False})
        auth = SimpleNamespace(cookie_name="session", labels={"private-cookie": "source"})
        measured = fixture.MeasureHTTP(application, metrics, auth)
        scope = {"type": "http", "method": "GET", "path": "/api/media/" + "a" * 64,
                 "headers": [(b"cookie", b"session=private-cookie")], "query_string": b"private-token=secret"}
        async def send(_message):
            pass
        task = asyncio.create_task(measured(scope, None, send))
        await entered.wait()
        stopping = asyncio.create_task(metrics.stop("stream"))
        await asyncio.sleep(0.02)
        assert not stopping.done()
        with pytest.raises(HTTPException) as error:
            metrics.start("too-early")
        assert error.value.status_code == 409
        release.set()
        await task
        report = await stopping
        assert report["requests"][0]["bodyBytes"] == 6 and not report["incomplete"]
        assert "private" not in json.dumps(report)
        metrics.start("overflow")
        sent = []
        async def collect(message):
            sent.append(message)
        await measured(scope, None, collect)
        assert sent[0]["status"] == 503
        report = await metrics.stop("overflow")
        assert report["overflow"] and report["requests"] == []
    asyncio.run(run())


def test_aborted_stream_cannot_be_reported_as_complete():
    async def run():
        metrics = fixture.PhaseMetrics()
        metrics.start("aborted")
        async def application(_scope, _receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"partial", "more_body": True})
            raise RuntimeError("stream interrupted")
        async def send(_message):
            pass
        measured = fixture.MeasureHTTP(application, metrics, SimpleNamespace(cookie_name="session", labels={}))
        with pytest.raises(RuntimeError, match="interrupted"):
            await measured({"type": "http", "method": "GET", "path": "/api/sync/snapshot", "headers": []}, None, send)
        report = await metrics.stop("aborted")
        assert report["incomplete"] and not report["requests"][0]["complete"]
        assert report["requests"][0]["bodyBytes"] == 7
    asyncio.run(run())


def test_fixture_run_owns_only_its_manifest_and_cleans_normal_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(fixture, "ROOT", tmp_path)
    monkeypatch.setenv("KEEPSITE_P2P_RUN_TOKEN", TOKEN)
    dist = tmp_path / "client"
    dist.mkdir()
    (dist / "index.html").write_text("synthetic fixture", encoding="utf-8")
    profile = replace(fixture.benchmark_profile(1), dist=dist)
    cache = tmp_path / ".cache"
    cache.mkdir()
    sentinel = cache / "user-existing.txt"
    sentinel.write_text("retained", encoding="utf-8")
    class Server:
        def __init__(self, config):
            self.app = config.app
            self.should_exit = False
        def run(self):
            assert (cache / f"m6-run-{TOKEN}.json").exists()
            with TestClient(self.app, base_url=profile.origin, client=("127.0.0.1", 12345)) as client:
                bad = {**HEADERS, "X-Keepsite-Fixture-Run": "0" * 32}
                assert client.post("/__test/shutdown", headers=bad).status_code == 403
                assert not self.should_exit
                assert client.post("/__test/shutdown", headers=HEADERS).status_code == 200
                assert self.should_exit
    monkeypatch.setattr(fixture.uvicorn, "Server", Server)
    fixture.run_fixture(profile)
    assert [path.name for path in cache.iterdir()] == [sentinel.name]
    assert sentinel.read_text(encoding="utf-8") == "retained"
    manifest = cache / f"m6-run-{TOKEN}.json"
    manifest.write_text("foreign-owner", encoding="utf-8")
    with pytest.raises(FileExistsError):
        fixture.run_fixture(profile)
    assert manifest.read_text(encoding="utf-8") == "foreign-owner"
    assert sorted(path.name for path in cache.iterdir()) == sorted([sentinel.name, manifest.name])

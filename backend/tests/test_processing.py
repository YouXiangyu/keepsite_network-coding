"""Durable pipeline, real Streams protocol, lease fencing and publication recovery."""
from contextlib import contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import time
import uuid

import fakeredis
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from backend.app import create_app
from backend.backups import create_account_backup, restore_account_backup
from backend.object_storage import S3ObjectStore, StorageError
from backend.posters import PosterCandidate
from backend.processing import ProcessingWorker, ProcessingFailed, run_isolated_ocr, TEMP_RETENTION_SECONDS
from backend import processing_repository as repository
from backend.shared_state import RedisSharedState
from account_fixtures import account_snapshot
from account_fixtures import ORIGIN, USERS, WRITE_HEADERS, FakeAuthFactory, image_bytes, signed_in
from test_api import create, upload, patch, delete, make_app, applied
from test_backup_restore import restored_app
from test_object_storage import FakeS3Client

OWNER = USERS["alice"]["id"]


def candidate():
    return PosterCandidate(content=image_bytes("yellow"), mime_type="image/png",
        source_page="https://commons.wikimedia.org/wiki/File:Example_poster.png",
        license_name="Public domain", license_url="https://commons.wikimedia.org/wiki/File:Example_poster.png#Licensing",
        author="Example creator", fetched_at=int(time.time() * 1000), width=9, height=7,
        source_file="File:Example poster.png")


class PosterFixture:
    def __init__(self, result=True):
        self.result = candidate() if result is True else result
        self.calls = []
        self.before_result = None
        self.closed = False

    def availability(self):
        return {"available": True, "reason": None}

    def find(self, title, showtime="", *, stopping=lambda: False):
        self.calls.append((title, showtime))
        if self.before_result:
            self.before_result()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        self.closed = True


@contextmanager
def pipeline(root, *, provider=None, runner=None, cache=None):
    fake = FakeS3Client()
    storage = S3ObjectStore("test-bucket", "ap-northeast-1", client=fake)
    cache = cache or RedisSharedState(client=fakeredis.FakeRedis())
    provider = provider or PosterFixture()
    calls = []
    def recognize(raw, **kwargs):
        calls.append(raw)
        return {"title": "Example", "cinema": "OCR影院", "hall": "1厅", "seat": "A1", "showtime": "2026-10-22T19:30"}
    def factory(store, shared):
        return ProcessingWorker(store, shared, provider=provider, ocr_runner=runner or recognize,
                                ocr_status={"available": True, "reason": None})
    app = create_app(root / "data", object_store=storage, auth_provider=FakeAuthFactory(), shared_state=cache,
                     run_recovery=False, run_processing=False, processing_factory=factory)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        yield client, app, app.state.processing_worker, fake, provider, calls


def current_job(store):
    with store.transaction() as db:
        return dict(db.execute("SELECT j.* FROM ticket_processing p JOIN processing_jobs j ON j.id=p.job_id WHERE p.user_id=? AND p.ticket_id='ticket-1'", (OWNER,)).fetchone())


def force_due(store, identifier):
    with store.transaction(True) as db:
        db.execute("UPDATE processing_jobs SET lease_until=0,next_attempt_at=0,last_enqueued_at=0 WHERE id=?", (identifier,))


def ticket(client):
    response = client.get("/api/sync/snapshot")
    assert response.status_code == 200, response.text
    return response.json()["tickets"][0]


def test_creation_commits_intent_without_waiting_and_stream_worker_publishes(tmp_path):
    with pipeline(tmp_path) as (client, app, worker, fake, provider, ocr_calls):
        state, _ = create(client)
        assert state["tickets"][0]["processing"] == {"status": "queued", "error": None}
        assert state["capabilities"]["processing"] is True and state["capabilities"]["processingUnavailableReason"] is None
        assert not ocr_calls and not provider.calls
        assert worker.queue.client.exists(worker.queue.stream) == 0
        assert worker.run_once()
        result = ticket(client)
        assert result["processing"] == {"status": "ready", "error": None}
        assert result["fields"]["cinema"] == "OCR影院" and result["fieldSources"]["cinema"] == "ocr"
        assert result["media"]["poster"]["cloudStatus"] == "saved"
        assert result["media"]["poster"]["source"]["provider"] == "wikimedia_commons"
        assert result["version"] == 4  # creation, OCR claim, OCR fields/poster stage, published poster
        assert len(ocr_calls) == len(provider.calls) == 1
        assert len(fake.objects) == 2
        assert worker.queue.client.xpending(worker.queue.stream, worker.queue.GROUP)["pending"] == 0
        assert list((app.state.store.root / "tmp" / "processing").glob("*.image")) == []
        with app.state.store.transaction() as db:
            states = [json.loads(row[0])["processing"]["status"] for row in db.execute("SELECT payload FROM ticket_changes ORDER BY cursor")]
            assert states == ["queued", "ocr", "poster", "ready"]
            assert db.execute("SELECT COUNT(*) FROM processing_cleanup").fetchone()[0] == 1


def test_missing_configuration_never_fakes_processing_or_registers_jobs(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        state, _ = create(client)
        assert state["tickets"][0]["processing"]["status"] == "not_started"
        response = client.post("/api/tickets/ticket-1/process", headers=WRITE_HEADERS, json={"operationId": "manual"})
        assert response.status_code == 503
        with app.state.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM processing_jobs").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM operations WHERE id='manual'").fetchone()[0] == 0


def test_processing_retry_is_idempotent_and_owner_scoped(tmp_path):
    with pipeline(tmp_path) as (client, app, worker, *_):
        create(client)
        worker.run_once()
        request = {"operationId": "process-again"}
        first = client.post("/api/tickets/ticket-1/process", headers=WRITE_HEADERS, json=request)
        assert first.status_code == 200 and applied(client, first)["tickets"][0]["processing"]["status"] == "queued"
        assert client.post("/api/tickets/ticket-1/process", headers=WRITE_HEADERS, json=request).json() == first.json()
        with app.state.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM processing_jobs").fetchone()[0] == 2
        signed_in(client, "bob")
        assert client.post("/api/tickets/ticket-1/process", headers=WRITE_HEADERS, json=request).status_code == 404
        signed_in(client)
        assert client.post("/api/tickets/ticket-1/process", json=request).status_code == 403
        assert patch(client, {"seat": "B2"}, operation="process-again").status_code == 409


def test_queue_loss_and_duplicate_delivery_rebuild_from_sql_without_duplicate_work(tmp_path):
    with pipeline(tmp_path) as (client, app, worker, fake, provider, ocr_calls):
        create(client)
        job = current_job(app.state.store)
        worker.reconcile()
        assert worker.queue.client.xlen(worker.queue.stream) == 1
        worker.queue.client.delete(worker.queue.stream)
        force_due(app.state.store, job["id"])
        worker.run_once()
        assert ticket(client)["processing"]["status"] == "ready"
        worker.queue.publish(job["id"])
        worker.queue.publish(job["id"])
        worker.run_once()
        worker.run_once()
        assert len(ocr_calls) == len(provider.calls) == 1
        assert fake.writes == 2
        assert current_job(app.state.store)["attempts"] == 1


def test_two_consumers_and_expired_leases_fence_old_results(tmp_path):
    with pipeline(tmp_path) as (client, app, *_):
        create(client)
        identifier = current_job(app.state.store)["id"]
        first = repository.claim(app.state.store, identifier, "worker-1")
        assert repository.claim(app.state.store, identifier, "worker-2") is None
        force_due(app.state.store, identifier)
        second = repository.claim(app.state.store, identifier, "worker-2")
        assert second["lease_token"] == first["lease_token"] + 1
        assert repository.save_ocr(app.state.store, first, {"title": "stale"}) is None
        assert repository.finish(app.state.store, first) is False
        assert repository.save_ocr(app.state.store, second, {"title": "current"})["title"] == "current"
        assert repository.finish(app.state.store, second)
        assert ticket(client)["fields"]["title"] == "current"


def test_prepared_poster_survives_publication_failure_without_refetch(tmp_path, monkeypatch):
    with pipeline(tmp_path) as (client, app, worker, fake, provider, ocr_calls):
        create(client)
        original_finish = repository.finish
        monkeypatch.setattr(repository, "finish", lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError("database unavailable")))
        worker.run_once()
        job = current_job(app.state.store)
        assert job["status"] == "poster" and job["artifact_id"] is not None
        assert len(fake.objects) == 2 and ticket(client)["media"]["poster"] is None
        monkeypatch.setattr(repository, "finish", original_finish)
        force_due(app.state.store, job["id"])
        worker.run_once()
        assert ticket(client)["processing"]["status"] == "ready"
        assert len(provider.calls) == len(ocr_calls) == 1
        assert fake.writes == 2


def test_user_fields_are_not_overwritten_and_no_match_is_terminal(tmp_path):
    with pipeline(tmp_path, provider=PosterFixture(None)) as (client, app, worker, fake, provider, _):
        create(client, fields={"title": "User title", "seat": "", "cinema": "User cinema"})
        worker.run_once()
        result = ticket(client)
        assert result["processing"] == {"status": "no_match", "error": None}
        assert result["fields"]["title"] == "User title" and result["fields"]["seat"] == ""
        assert result["fields"]["cinema"] == "User cinema"
        assert provider.calls[0][0] == "User title"
        assert result["media"]["poster"] is None and len(fake.objects) == 1


def test_original_change_preserves_poster_while_pending_then_no_match_uses_original(tmp_path):
    results = iter([{"title": "Old OCR", "cinema": "Old OCR cinema"}, {}])
    with pipeline(tmp_path, runner=lambda *args, **kwargs: next(results)) as (client, app, worker, fake, provider, _):
        create(client, fields={"seat": "User seat"})
        worker.run_once()
        first = ticket(client)
        replacement = upload(client, "blue")
        response = client.patch("/api/tickets/ticket-1", headers=WRITE_HEADERS,
                                json={"operationId": "new-original", "originalAssetId": replacement["assetId"]})
        changed = applied(client, response)["tickets"][0]
        assert changed["fields"]["title"] == changed["fields"]["cinema"] == ""
        assert changed["fieldSources"]["title"] == changed["fieldSources"]["cinema"] == "empty"
        assert changed["fields"]["seat"] == "User seat"
        assert changed["media"]["poster"] == first["media"]["poster"]
        worker.run_once()
        result = ticket(client)
        assert result["processing"]["status"] == "no_match"
        assert result["fields"]["title"] == result["fields"]["cinema"] == ""
        assert result["media"]["poster"] is None
        assert result["media"]["original"]["assetId"] == replacement["assetId"]
        assert client.get(first["media"]["poster"]["url"]).status_code == 404
        assert client.get(replacement["url"]).status_code == 200
        with app.state.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM poster_sources WHERE user_id=? AND ticket_id='ticket-1'", (OWNER,)).fetchone()[0] == 0


@pytest.mark.parametrize("ending", ["no_match", "fail", "claim_attempts", "claim_deadline", "expire"])
def test_terminal_reprocessing_withdraws_old_cover_atomically_and_notifies(ending, tmp_path):
    with pipeline(tmp_path) as (client, app, worker, fake, *_):
        create(client)
        worker.run_once()
        first = ticket(client)
        old_poster, original = first["media"]["poster"], first["media"]["original"]
        objects_before = dict(fake.objects)
        assert client.post("/api/tickets/ticket-1/process", headers=WRITE_HEADERS,
                           json={"operationId": "process-again"}).status_code == 200
        assert ticket(client)["media"]["poster"] == old_poster
        identifier = current_job(app.state.store)["id"]
        job = None
        if ending in ("no_match", "fail"):
            job = repository.claim(app.state.store, identifier, worker.owner)
            assert repository.save_ocr(app.state.store, job, {}) is not None
            assert ticket(client)["media"]["poster"] == old_poster
        if ending == "fail":
            assert repository.fail(app.state.store, job)
            retrying = ticket(client)
            assert retrying["processing"]["status"] == "queued" and retrying["media"]["poster"] == old_poster
            assert client.get(old_poster["url"]).status_code == 200
            force_due(app.state.store, identifier)
            job = repository.claim(app.state.store, identifier, worker.owner)
        with app.state.store.transaction(True) as db:
            if ending in ("fail", "claim_attempts"):
                db.execute("UPDATE processing_jobs SET attempts=? WHERE id=?", (repository.MAX_ATTEMPTS, identifier))
            elif ending in ("claim_deadline", "expire"):
                db.execute("UPDATE processing_jobs SET deadline_at=0 WHERE id=?", (identifier,))
            assert db.execute("SELECT COUNT(*) FROM poster_sources WHERE user_id=? AND ticket_id='ticket-1'", (OWNER,)).fetchone()[0] == 1
        before = account_snapshot(client)
        observed = []
        unsubscribe = app.state.store.subscribe_changes(lambda user, cursor: observed.append((user, cursor)))
        try:
            if ending == "no_match":
                assert repository.finish(app.state.store, job)
            elif ending == "fail":
                assert repository.fail(app.state.store, job)
            elif ending.startswith("claim_"):
                assert repository.claim(app.state.store, identifier, worker.owner) is None
            else:
                assert repository.expire_jobs(app.state.store) == 1
        finally:
            unsubscribe()
        after = account_snapshot(client)
        result = after["tickets"][0]
        assert result["processing"]["status"] == ("no_match" if ending == "no_match" else "failed")
        assert result["media"] == {"original": original, "poster": None}
        assert result["version"] == before["tickets"][0]["version"] + 1
        assert after["cursor"] == before["cursor"] + 1
        assert observed == [(OWNER, after["cursor"])]
        response = client.get("/api/sync/changes", params={"serverId": before["serverId"], "after": before["cursor"]})
        assert response.status_code == 200
        assert response.json()["changes"] == [{"type": "upsert", "cursor": after["cursor"], "ticket": result}]
        with app.state.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM poster_sources WHERE user_id=? AND ticket_id='ticket-1'", (OWNER,)).fetchone()[0] == 0
        assert client.get(old_poster["url"]).status_code == 404
        assert client.get(original["url"]).status_code == 200
        assert fake.objects == objects_before  # Reference withdrawal never deletes stored image bytes.


def test_terminal_cover_withdrawal_rolls_back_with_the_state_and_change_log(tmp_path, monkeypatch):
    with pipeline(tmp_path) as (client, app, worker, *_):
        create(client)
        worker.run_once()
        client.post("/api/tickets/ticket-1/process", headers=WRITE_HEADERS, json={"operationId": "process-again"})
        job = repository.claim(app.state.store, current_job(app.state.store)["id"], worker.owner)
        before = account_snapshot(client)
        observed = []
        unsubscribe = app.state.store.subscribe_changes(lambda user, cursor: observed.append((user, cursor)))
        original_record = app.state.store.record_ticket_change
        def broken_record(*args):
            original_record(*args)
            raise RuntimeError("injected publication rollback")
        monkeypatch.setattr(app.state.store, "record_ticket_change", broken_record)
        try:
            with pytest.raises(RuntimeError, match="publication rollback"):
                repository.finish(app.state.store, job)
        finally:
            unsubscribe()
        assert account_snapshot(client) == before and observed == []
        with app.state.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM poster_sources WHERE user_id=? AND ticket_id='ticket-1'", (OWNER,)).fetchone()[0] == 1
        assert client.get(before["tickets"][0]["media"]["poster"]["url"]).status_code == 200


@pytest.mark.parametrize("superseded", ["lease", "input", "deleted"])
def test_obsolete_processing_cannot_withdraw_a_current_cover(superseded, tmp_path):
    with pipeline(tmp_path) as (client, app, worker, *_):
        create(client)
        worker.run_once()
        client.post("/api/tickets/ticket-1/process", headers=WRITE_HEADERS, json={"operationId": "process-again"})
        old = repository.claim(app.state.store, current_job(app.state.store)["id"], "old-worker")
        if superseded == "lease":
            force_due(app.state.store, old["id"])
            newer = repository.claim(app.state.store, old["id"], "new-worker")
            assert newer["lease_token"] > old["lease_token"]
        elif superseded == "input":
            assert patch(client, {"title": "Corrected title"}, "corrected").status_code == 200
            worker.run_once()
            assert ticket(client)["processing"]["status"] == "ready"
        else:
            assert delete(client).status_code == 200
        before = account_snapshot(client)
        with app.state.store.transaction() as db:
            source = tuple(db.execute("SELECT asset_id,source FROM poster_sources WHERE user_id=? AND ticket_id='ticket-1'", (OWNER,)).fetchone())
        assert repository.finish(app.state.store, old) is False
        assert repository.fail(app.state.store, old) is False
        assert repository.claim(app.state.store, old["id"], "old-worker") is None
        assert repository.expire_jobs(app.state.store) == 0
        assert account_snapshot(client) == before
        with app.state.store.transaction() as db:
            assert tuple(db.execute("SELECT asset_id,source FROM poster_sources WHERE user_id=? AND ticket_id='ticket-1'", (OWNER,)).fetchone()) == source


def test_correction_during_fetch_discards_old_job_and_keeps_user_input(tmp_path):
    with pipeline(tmp_path) as (client, app, worker, fake, provider, _):
        create(client)
        provider.before_result = lambda: patch(client, {"title": "Corrected"}, operation="correct-during-fetch")
        worker.run_once()
        result = ticket(client)
        assert result["fields"]["title"] == "Corrected" and result["fieldSources"]["title"] == "user"
        assert result["processing"]["status"] == "queued" and result["media"]["poster"] is None
        assert len(fake.objects) == 1


def test_delete_during_s3_upload_never_publishes_or_authorizes_orphan(tmp_path, monkeypatch):
    with pipeline(tmp_path) as (client, app, worker, fake, provider, _):
        create(client)
        original_put = fake.put_object
        def raced(**kwargs):
            delete(client)
            return original_put(**kwargs)
        monkeypatch.setattr(fake, "put_object", raced)
        worker.run_once()
        assert account_snapshot(client)["tickets"] == []
        with app.state.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM processing_artifacts").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM asset_grants").fetchone()[0] == 1
            descriptor = json.loads(db.execute("SELECT descriptor FROM processing_artifacts").fetchone()[0])
        assert client.get("/api/media/" + descriptor["id"]).status_code == 404
        assert len(fake.objects) == 2


def test_finite_retries_preserve_original_and_end_in_visible_failure(tmp_path):
    with pipeline(tmp_path, provider=PosterFixture(RuntimeError("never expose provider URL"))) as (client, app, worker, fake, _, _):
        create(client)
        for _ in range(repository.MAX_ATTEMPTS):
            job = current_job(app.state.store)
            force_due(app.state.store, job["id"])
            worker.run_once()
        result = ticket(client)
        assert result["processing"]["status"] == "failed"
        assert "never expose" not in result["processing"]["error"]
        assert result["media"]["poster"] is None and client.get(result["media"]["original"]["url"]).status_code == 200
        assert current_job(app.state.store)["attempts"] == repository.MAX_ATTEMPTS


def test_expired_durable_job_converges_even_when_redis_is_down(tmp_path, monkeypatch):
    with pipeline(tmp_path) as (client, app, worker, *_):
        first, _ = create(client)
        with app.state.store.transaction(True) as db:
            db.execute("UPDATE processing_jobs SET deadline_at=0")
        def offline(*args, **kwargs):
            raise RedisConnectionError("offline")
        monkeypatch.setattr(worker.queue.client, "xgroup_create", offline)
        with pytest.raises(RedisConnectionError):
            worker.run_once()
        failed = ticket(client)
        assert failed["processing"]["status"] == "failed"
        assert failed["version"] == first["tickets"][0]["version"] + 1


def test_cleanup_is_bounded_recorded_and_preserves_active_and_recent_files(tmp_path):
    with pipeline(tmp_path) as (client, app, worker, *_):
        create(client)
        job = repository.claim(app.state.store, current_job(app.state.store)["id"], worker.owner)
        name = uuid.uuid4().hex + ".image"
        active = worker._temp_path(name, create=True)
        active.write_bytes(b"owned active")
        repository.prepare_artifact(app.state.store, job, {}, {}, name)
        orphan = worker._temp_path(uuid.uuid4().hex + ".image")
        orphan.write_bytes(b"owned orphan")
        recent = worker._temp_path(uuid.uuid4().hex + ".image")
        recent.write_bytes(b"recent")
        unrelated = active.parent / "preserve.txt"
        unrelated.write_text("not a pipeline temporary")
        old = time.time() - TEMP_RETENTION_SECONDS - 1
        os.utime(active, (old, old))
        os.utime(orphan, (old, old))
        assert worker.cleanup_temporary(maximum=200) == 1
        assert active.exists() and recent.exists() and unrelated.exists() and not orphan.exists()
        with app.state.store.transaction(True) as db:
            db.execute("UPDATE processing_jobs SET status='cancelled'")
        assert worker.cleanup_temporary(maximum=200) == 1
        assert not active.exists()
        with app.state.store.transaction() as db:
            assert {row[0] for row in db.execute("SELECT reason FROM processing_cleanup")} == {"orphan_expired", "terminal_expired"}


def _slow_child(raw, output):
    time.sleep(30)


def test_ocr_timeout_terminates_owned_process_and_closes_resources():
    before = {child.pid for child in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(ProcessingFailed):
        run_isolated_ocr(b"synthetic", timeout=0.05, target=_slow_child)
    assert time.monotonic() - started < 8
    assert {child.pid for child in multiprocessing.active_children()} <= before


def test_poster_attribution_survives_backup_but_runtime_jobs_do_not_resume_as_ready(tmp_path):
    with pipeline(tmp_path) as (client, app, worker, *_):
        create(client)
        worker.run_once()
        source = ticket(client)["media"]["poster"]["source"]
        archive = create_account_backup(app.state.store, OWNER)
        destination = tmp_path / "restored"
        restore_account_backup(archive, destination, user_id=OWNER)
        restored = restored_app(destination)
        with TestClient(restored, base_url=ORIGIN) as reader:
            signed_in(reader)
            result = ticket(reader)
            assert result["media"]["poster"]["source"] == source
            assert result["media"]["poster"]["cloudStatus"] == "not_configured"
            assert result["processing"]["status"] == "failed"
            assert reader.get(result["media"]["poster"]["url"]).status_code == 200
            with restored.state.store.transaction() as db:
                assert db.execute("SELECT COUNT(*) FROM processing_artifacts").fetchone()[0] == 0


def test_real_redis_stream_group_delivery_and_queue_loss(tmp_path):
    url = os.environ.get("MEMOVERSE_TEST_REDIS_URL")
    if not url:
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("CI must provide real Redis for Streams coverage")
        pytest.skip("No local Redis configured; Application CI must run real Streams coverage")
    cache = RedisSharedState(url=url)
    stream = None
    try:
        assert cache.client.ping()
        with pipeline(tmp_path, cache=cache) as (client, app, worker, *_):
            stream = worker.queue.stream
            create(client)
            worker.reconcile()
            assert cache.client.xlen(stream) == 1
            cache.client.delete(stream)
            force_due(app.state.store, current_job(app.state.store)["id"])
            worker.run_once()
            assert ticket(client)["processing"]["status"] == "ready"
            assert cache.client.xpending(stream, worker.queue.GROUP)["pending"] == 0
            cache.client.delete(stream)
            stream = None
    finally:
        # The context normally closes the client. Only this UUID database stream
        # can be removed here, never a global Redis database or other test keys.
        if stream and cache.client is not None:
            cache.client.delete(stream)
        cache.close()


@pytest.mark.parametrize("after_write", [False, True], ids=["put-failed", "put-response-lost"])
def test_poster_upload_recovery_reuses_durable_artifact_without_refetch(tmp_path, monkeypatch, after_write):
    from botocore.exceptions import ReadTimeoutError
    with pipeline(tmp_path) as (client, app, worker, fake, provider, ocr_calls):
        create(client)
        original_put = fake.put_object
        def fail(**kwargs):
            if after_write:
                original_put(**kwargs)
                raise ReadTimeoutError(endpoint_url="https://synthetic.invalid")
            raise FakeS3Client.error(503)
        monkeypatch.setattr(fake, "put_object", fail)
        worker.run_once()
        pending = current_job(app.state.store)
        assert pending["status"] == "queued" and pending["artifact_id"] is not None
        assert ticket(client)["media"]["poster"] is None
        monkeypatch.setattr(fake, "put_object", original_put)
        force_due(app.state.store, pending["id"])
        worker.run_once()
        assert ticket(client)["processing"]["status"] == "ready"
        assert len(provider.calls) == len(ocr_calls) == 1
        assert fake.writes == 2


def test_unsafe_tmp_parent_link_cannot_escape_processing_cleanup(tmp_path):
    with pipeline(tmp_path) as (client, app, worker, *_):
        outside = tmp_path / "outside"
        (outside / "processing").mkdir(parents=True)
        sentinel = outside / "processing" / (uuid.uuid4().hex + ".image")
        sentinel.write_bytes(b"external sentinel")
        old = time.time() - TEMP_RETENTION_SECONDS - 1
        os.utime(sentinel, (old, old))
        temporary = app.state.store.root / "tmp"
        temporary.rmdir()
        try:
            temporary.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            temporary.mkdir()
            pytest.skip(f"Platform cannot create test directory symlink: {type(error).__name__}")
        try:
            with pytest.raises(ProcessingFailed):
                worker._temp_path(uuid.uuid4().hex + ".image", create=True)
            assert worker.cleanup_temporary() == 0
            assert sentinel.read_bytes() == b"external sentinel"
        finally:
            temporary.unlink()
            temporary.mkdir()


def test_tmp_parent_junction_check_is_applied_before_scanning(tmp_path, monkeypatch):
    with pipeline(tmp_path) as (client, app, worker, *_):
        temporary = app.state.store.root / "tmp"
        original = Path.is_junction
        monkeypatch.setattr(Path, "is_junction", lambda path: True if path == temporary else original(path))
        with pytest.raises(ProcessingFailed):
            worker._temp_path(uuid.uuid4().hex + ".image", create=True)
        assert worker.cleanup_temporary() == 0
        assert not (temporary / "processing").exists()


def test_provider_is_not_closed_concurrently_with_an_unfinished_worker(tmp_path):
    from types import SimpleNamespace
    with pipeline(tmp_path) as (client, app, worker, fake, provider, _):
        worker.thread = SimpleNamespace(ident=1, join=lambda timeout: None, is_alive=lambda: True)
        worker.stop()
        assert not provider.closed
        # Model the worker's eventual finally block; no actual thread was started.
        worker._close_resources()
        assert provider.closed


def test_cache_configuration_identity_includes_unavailable_reason(tmp_path):
    cache = RedisSharedState(client=fakeredis.FakeRedis())
    from backend.object_storage import LocalObjectStore
    app = create_app(tmp_path / "data", object_store=LocalObjectStore(tmp_path / "objects"),
                     auth_provider=FakeAuthFactory(), shared_state=cache, run_recovery=False, run_processing=False)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        first = account_snapshot(client)
        app.state.store.processing_unavailable_reason = "处理配置发生变化，请重新配置。"
        second = account_snapshot(client)
        assert second["cursor"] == first["cursor"]
        assert second["capabilities"]["processingUnavailableReason"] == "处理配置发生变化，请重新配置。"
        assert cache.client.dbsize() == 2


def test_cleanup_parent_change_is_rejected_without_stopping_later_processing(tmp_path, monkeypatch):
    with pipeline(tmp_path) as (client, app, worker, *_):
        orphan = worker._temp_path(uuid.uuid4().hex + ".image", create=True)
        orphan.write_bytes(b"sentinel")
        old = time.time() - TEMP_RETENTION_SECONDS - 1
        os.utime(orphan, (old, old))
        original = worker._temp_path
        monkeypatch.setattr(worker, "_temp_path", lambda *args, **kwargs: (_ for _ in ()).throw(ProcessingFailed()))
        assert worker.cleanup_temporary() == 0
        assert orphan.read_bytes() == b"sentinel"
        monkeypatch.setattr(worker, "_temp_path", original)
        create(client)
        assert worker.run_once()
        assert ticket(client)["processing"]["status"] == "ready"

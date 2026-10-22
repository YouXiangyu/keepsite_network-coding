"""Durable per-account changes, baseline resets and disposable Redis snapshots."""
import json
import sqlite3

import fakeredis
import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from backend.app import create_app
from backend.backups import create_account_backup, restore_account_backup
from backend.object_storage import LocalObjectStore, S3ObjectStore
from backend.shared_state import RedisSharedState
from account_fixtures import ORIGIN, USERS, FakeAuthFactory, signed_in
from test_api import make_app, create, patch, delete, upload
from test_backup_restore import restored_app
from test_object_storage import FakeS3Client


def test_every_ticket_mutation_logs_the_same_committed_revision(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        first, payload = create(client)
        second = patch(client, {"seat": "A1"}).json()
        third = delete(client).json()
        from account_fixtures import WRITE_HEADERS
        assert client.post("/api/tickets", headers=WRITE_HEADERS, json=payload).json() == third
    with app.state.store.transaction() as db:
        rows = db.execute("SELECT * FROM ticket_changes ORDER BY cursor").fetchall()
        assert [row["cursor"] for row in rows] == [first["revision"], second["revision"], third["revision"]] == [1, 2, 3]
        values = [json.loads(row["payload"]) for row in rows]
        assert values[0] == first["data"]["tickets"][0]
        assert values[1] == second["data"]["tickets"][0]
        assert values[2]["deletedAt"] is not None and values[2]["version"] == 3
        assert values[2]["media"]["original"] == values[0]["media"]["original"]
        assert db.execute("SELECT cursor FROM sync_baselines WHERE user_id=?", (USERS["alice"]["id"],)).fetchone()[0] == 0


def test_change_log_failure_rolls_back_ticket_operation_and_revision(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        first, _ = create(client)
        def fail(*args):
            raise sqlite3.OperationalError("injected changelog failure")
        monkeypatch.setattr(app.state.store, "record_ticket_change", fail)
        with pytest.raises(sqlite3.OperationalError):
            patch(client, {"seat": "A1"})
        assert client.get("/api/state").json() == first
        with app.state.store.transaction() as db:
            assert db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM ticket_changes").fetchone()[0] == 1


def test_backup_restoration_starts_new_sync_baseline_without_pending_uploads(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        create(client)
        final = patch(client, {"hall": "2"}).json()
        upload(client, "blue")
    archive = create_account_backup(app.state.store, USERS["alice"]["id"])
    destination = tmp_path / "restored"
    restore_account_backup(archive, destination, user_id=USERS["alice"]["id"])
    restored = restored_app(destination)
    with TestClient(restored, base_url=ORIGIN) as client:
        signed_in(client)
        state = client.get("/api/state").json()
        assert state["serverId"] != final["serverId"]
        assert state["revision"] == final["revision"]
        with restored.state.store.transaction() as db:
            assert db.execute("SELECT cursor FROM sync_baselines").fetchone()[0] == state["revision"]
            assert db.execute("SELECT COUNT(*) FROM ticket_changes").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM upload_intents").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM upload_receipts").fetchone()[0] == 0
        new_state = patch(client, {"seat": "B1"}, operation="restored-change").json()
        with restored.state.store.transaction() as db:
            assert db.execute("SELECT cursor FROM ticket_changes").fetchone()[0] == new_state["revision"] == state["revision"] + 1


def test_m1_metadata_without_change_tables_receives_explicit_baseline(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        state, _ = create(client)
    with app.state.store.transaction(True) as db:
        db.execute("DROP TABLE ticket_changes")
        db.execute("DROP TABLE sync_baselines")
    reopened = make_app(tmp_path)
    with reopened.state.store.transaction() as db:
        assert db.execute("SELECT cursor FROM sync_baselines").fetchone()[0] == state["revision"]
        assert db.execute("SELECT COUNT(*) FROM ticket_changes").fetchone()[0] == 0


def test_unknown_or_changed_storage_binding_is_rejected(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        create(client)
    other = S3ObjectStore("test-bucket", "ap-northeast-1", client=FakeS3Client())
    with pytest.raises(RuntimeError, match="binding changed"):
        make_app(tmp_path, other)
    with app.state.store.transaction(True) as db:
        db.execute("DELETE FROM meta WHERE key='storage_binding'")
    with pytest.raises(RuntimeError, match="binding is unknown"):
        make_app(tmp_path)


def test_redis_is_outside_sql_transactions_and_lost_cache_never_loses_records(tmp_path, monkeypatch):
    fake = fakeredis.FakeRedis()
    cache = RedisSharedState(client=fake)
    app = create_app(tmp_path / "data", object_store=LocalObjectStore(tmp_path / "objects"),
                     auth_provider=FakeAuthFactory(), shared_state=cache, run_recovery=False)
    calls = []
    for name in ("get_snapshot", "put_snapshot"):
        original = getattr(cache, name)
        def checked(*args, _name=name, _original=original):
            with app.state.store.transaction(True, timeout=0.01):
                pass
            calls.append(_name)
            return _original(*args)
        monkeypatch.setattr(cache, name, checked)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        state, _ = create(client)
        assert calls == [], "Write handlers do not call Redis inside their mutation transaction"
        assert client.get("/api/state").json() == state
        assert fake.dbsize() == 1
        assert client.get("/api/state").json() == state
        assert calls.count("put_snapshot") == 1
        fake.flushall()
        assert client.get("/api/state").json() == state
        edited = patch(client, {"title": "changed"}).json()
        assert client.get("/api/state").json() == edited
        assert fake.dbsize() == 2
        signed_in(client, "bob")
        assert client.get("/api/state").json()["data"] == {"tickets": []}
        signed_in(client)
        def offline(*args, **kwargs):
            raise RedisConnectionError("offline")
        monkeypatch.setattr(fake, "get", offline)
        monkeypatch.setattr(fake, "set", offline)
        assert client.get("/api/state").json() == edited
        deleted = delete(client).json()
        assert client.get("/api/state").json() == deleted


def test_account_snapshot_batches_media_queries(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        asset = upload(client)
        for index in range(10):
            create(client, str(index), asset)
        connect = app.state.store.connect
        selects = []
        def traced(**kwargs):
            db = connect(**kwargs)
            db.set_trace_callback(lambda sql: selects.append(sql) if sql.lstrip().startswith("SELECT") else None)
            return db
        monkeypatch.setattr(app.state.store, "connect", traced)
        assert len(client.get("/api/state").json()["data"]["tickets"]) == 10
        assert len(selects) <= 10, "Query count should not grow per ticket"


def test_storage_binding_is_rechecked_inside_initialization_transaction(tmp_path, monkeypatch):
    from backend.store import Store
    app = make_app(tmp_path)
    original_connect = Store.connect
    changed = False
    def switch_after_preflight(self, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            with sqlite3.connect(app.state.store.database) as db:
                db.execute("UPDATE meta SET value='different-storage' WHERE key='storage_binding'")
        return original_connect(self, **kwargs)
    monkeypatch.setattr(Store, "connect", switch_after_preflight)
    with pytest.raises(RuntimeError, match="during initialization"):
        make_app(tmp_path)

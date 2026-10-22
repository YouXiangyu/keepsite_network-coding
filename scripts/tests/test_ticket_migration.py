"""Isolated schema-2 migration must preserve originals and account ownership."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3

from PIL import Image
import pytest

SPEC = importlib.util.spec_from_file_location("ticket_migration", Path(__file__).parents[1] / "migrate-movie-tickets.py")
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


@pytest.fixture
def legacy(tmp_path):
    source = tmp_path / "snapshot.sqlite3"
    media = tmp_path / "originals"
    (media / "media").mkdir(parents=True)
    image = io.BytesIO()
    Image.new("RGB", (12, 8), "blue").save(image, format="PNG")
    raw = image.getvalue()
    asset_id = hashlib.sha256(raw).hexdigest()
    key = f"media/{asset_id}.png"
    (media / key).write_bytes(raw)
    with sqlite3.connect(source) as db:
        db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO meta VALUES('schema_version','2');
            CREATE TABLE users(id TEXT PRIMARY KEY,email TEXT,name TEXT,revision INTEGER,template_version TEXT,created_at INTEGER);
            CREATE TABLE assets(id TEXT PRIMARY KEY,object_key TEXT,mime TEXT,size INTEGER,width INTEGER,height INTEGER,created_at INTEGER);
            CREATE TABLE asset_grants(user_id TEXT,asset_id TEXT,PRIMARY KEY(user_id,asset_id));
            CREATE TABLE collections(user_id TEXT,id TEXT,payload TEXT,asset_id TEXT,model_id TEXT,version INTEGER,PRIMARY KEY(user_id,id));
        """)
        db.executemany("INSERT INTO users VALUES (?,?,?,7,'old-template',100)", [("alice", "a@example.test", "Alice"), ("bob", "b@example.test", "Bob")])
        db.execute("INSERT INTO assets VALUES (?,?,?, ?,12,8,100)", (asset_id, key, "image/png", len(raw)))
        db.execute("INSERT INTO asset_grants VALUES ('alice',?)", (asset_id,))
        for name, category in [("film", "movie"), ("trip", "travel")]:
            db.execute("INSERT INTO collections VALUES ('alice',?,?,?,'old-model',5)", (
                name, json.dumps({"title": "保留片名", "category": category, "date": "2026-10-18", "addedAt": 123, "note": "preserved only in archive"}), asset_id,
            ))
    return source, media, asset_id, raw


def test_inventory_is_read_only_and_does_not_guess_screening_time(legacy):
    source, media, _asset, _raw = legacy
    before = migration.digest(source)
    report = migration.inventory(migration.read_snapshot(source))
    assert report["accounts"] == 2
    assert report["movieTickets"] == 1
    assert report["excludedCollections"] == 1
    assert migration.digest(source) == before
    assert not Path(str(source) + "-wal").exists()
    assert not Path(str(source) + "-shm").exists()


def test_migration_preserves_complete_snapshot_originals_and_ownership(legacy, tmp_path):
    from backend.object_storage import LocalObjectStore
    from backend.state import account_state
    from backend.store import Store

    source, media, asset_id, raw = legacy
    before = migration.digest(source)
    destination = tmp_path / "isolated-v3"
    report = migration.migrate(source, media, destination)
    assert report["status"] == "isolated-copy-verified"
    assert report["mediaStorage"] == "local-only"
    assert migration.digest(source) == before
    assert migration.digest(destination / "migration/source-schema2.sqlite3") == before
    assert (destination / "objects/media" / f"{asset_id}.png").read_bytes() == raw
    assert (media / "media" / f"{asset_id}.png").read_bytes() == raw
    assert not (destination / "RESTORE_INCOMPLETE").exists()
    store = Store(destination, storage=LocalObjectStore(destination / "objects"))
    with store.transaction() as db:
        alice = account_state(store, db, "alice")
        bob = account_state(store, db, "bob")
        assert bob["data"]["tickets"] == []
        assert len(alice["data"]["tickets"]) == 1
        ticket = alice["data"]["tickets"][0]
        assert ticket["fields"] == {"title": "保留片名", "cinema": "", "hall": "", "seat": "", "showtime": ""}
        assert ticket["fieldSources"]["title"] == "migration"
        assert ticket["fieldSources"]["showtime"] == "empty"
        assert ticket["media"]["original"]["cloudStatus"] == "not_configured"
        assert ticket["media"]["poster"] is None
        assert ticket["processing"]["status"] == "not_started"
        assert db.execute("SELECT cursor FROM sync_baselines WHERE user_id='alice'").fetchone()[0] == alice["revision"]
        assert db.execute("SELECT count(*) FROM ticket_changes").fetchone()[0] == 0
    with pytest.raises(migration.MigrationError, match="new directory"):
        migration.migrate(source, media, destination)


@pytest.mark.parametrize("damage", ["missing", "corrupt", "metadata", "owner"])
def test_invalid_original_or_grant_stops_before_creating_target(legacy, tmp_path, damage):
    source, media, asset_id, _raw = legacy
    original = media / "media" / f"{asset_id}.png"
    if damage == "missing":
        original.unlink()
    elif damage == "corrupt":
        image = io.BytesIO()
        Image.new("RGB", (12, 8), "red").save(image, format="PNG")
        original.write_bytes(image.getvalue())
    else:
        with sqlite3.connect(source) as db:
            db.execute("UPDATE assets SET width=13" if damage == "metadata" else "DELETE FROM asset_grants")
    destination = tmp_path / "blocked-v3"
    with pytest.raises(migration.MigrationError):
        migration.migrate(source, media, destination)
    assert not destination.exists()


def test_refuses_journal_and_wrong_schema_without_source_writes(legacy):
    source, _media, _asset, _raw = legacy
    journal = Path(str(source) + "-wal")
    journal.write_bytes(b"uncheckpointed data")
    before = migration.digest(source)
    with pytest.raises(migration.MigrationError, match="journal/WAL"):
        migration.read_snapshot(source)
    assert migration.digest(source) == before
    journal.unlink()
    with sqlite3.connect(source) as db:
        db.execute("UPDATE meta SET value='3'")
    with pytest.raises(migration.MigrationError, match="schema-2"):
        migration.read_snapshot(source)


def test_failure_keeps_recoverable_archive_and_blocks_startup(legacy, tmp_path, monkeypatch):
    from backend.object_storage import LocalObjectStore, StorageError
    from backend.store import Store

    source, media, _asset, _raw = legacy
    destination = tmp_path / "incomplete-v3"

    def fail(*_args, **_kwargs):
        raise StorageError("simulated disk full")

    monkeypatch.setattr(LocalObjectStore, "put_file", fail)
    with pytest.raises(StorageError):
        migration.migrate(source, media, destination)
    assert not destination.exists()
    staging = next(tmp_path.glob(".incomplete-v3.migration-*"))
    assert (staging / "RESTORE_INCOMPLETE").exists()
    assert (staging / "migration/source-schema2.sqlite3").is_file()
    with pytest.raises(RuntimeError, match="[Ii]ncomplete"):
        Store(staging, storage=LocalObjectStore(staging / "objects"))


def test_initialization_interruption_never_publishes_startable_destination(legacy, tmp_path, monkeypatch):
    import backend.store

    source, media, _asset, _raw = legacy
    destination = tmp_path / "never-published"
    original_store = backend.store.Store

    def interrupted(*args, **kwargs):
        original_store(*args, **kwargs)
        assert not destination.exists()
        raise KeyboardInterrupt("simulated interruption immediately after database initialization")

    monkeypatch.setattr(backend.store, "Store", interrupted)
    with pytest.raises(KeyboardInterrupt):
        migration.migrate(source, media, destination)
    assert not destination.exists()
    staging = next(tmp_path.glob(".never-published.migration-*"))
    assert (staging / "RESTORE_INCOMPLETE").is_file()

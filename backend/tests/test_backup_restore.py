"""Exercise account export, isolated restore, integrity and ownership together."""
import json
import stat
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.backups import BackupError, create_account_backup, restore_account_backup
from backend.object_storage import LocalObjectStore, StorageError
from backend import jobs, relief_engine
from backend import backups
from account_fixtures import FakeAuthFactory, ORIGIN, WRITE_HEADERS, signed_in
from test_api import build_app, create_collection, edit_payload, get_item, state
from test_jobs import fixture_generate


@pytest.fixture
def exported(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "engine_status", lambda: {"available": True, "reason": "fixture"})
    monkeypatch.setattr(relief_engine, "generate_relief", fixture_generate)
    app, _ = build_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        item = create_collection(client)
        assert app.state.worker.run_one()
        item = get_item(state(client.get("/api/state")), item["id"])
        state(client.patch(f"/api/collections/{item['id']}", headers=WRITE_HEADERS,
                           json=edit_payload(item, title="恢复我的收藏", note="原图、模型、笔记都要保留")))
        state(client.put("/api/community/community-1/like", headers=WRITE_HEADERS, json={"liked": True}))
        state(client.post("/api/shares/share-1/read", headers=WRITE_HEADERS))
        before = state(client.get("/api/state"))
        expected_objects = {}
        for item in before["data"]["collections"]:
            for key in ("customImage", "modelUrl"):
                expected_objects[item[key]] = client.get(item[key]).content
        archive = create_account_backup(app.state.store, "user-alice")
        signed_in(client, "bob")
        bob = create_collection(client)
        assert app.state.worker.run_one()
        yield app, archive, before, expected_objects, bob


def test_export_restore_refresh_preserves_records_models_and_account_isolation(exported, tmp_path):
    original, archive, before, expected_objects, bob = exported
    destination = tmp_path / "restored"
    summary = restore_account_backup(archive, destination, user_id="user-alice")
    assert summary["verified"] and summary["requeuedJobs"] == 0
    restored = create_app(destination, run_worker=False, auth_provider=FakeAuthFactory(),
                          object_store=LocalObjectStore(destination / "objects"), template_dir=tmp_path / "template")
    for _ in range(2):
        with TestClient(restored, base_url=ORIGIN) as client:
            signed_in(client)
            after = state(client.get("/api/state"))
            assert after["data"] == before["data"]
            assert after["revision"] == before["revision"]
            assert after["serverId"] != before["serverId"]
            for url, expected in expected_objects.items():
                assert client.get(url).content == expected
            assert client.get(f"/api/jobs/{bob['generationJobId']}").status_code == 404
            with restored.state.store.transaction() as db:
                assert db.execute("SELECT id FROM users").fetchall()[0][0] == "user-alice"
                assert not db.execute("PRAGMA foreign_key_check").fetchall()
            signed_in(client, "bob")
            state(client.get("/api/state"))
            private = next(item for item in before["data"]["collections"] if item.get("generationJobId"))
            for url in (private["customImage"], private["modelUrl"], f"/api/jobs/{private['generationJobId']}"):
                assert client.get(url).status_code == 404
    assert original.state.store.database != restored.state.store.database


def rewrite_archive(source, target, mutate):
    with zipfile.ZipFile(source) as incoming:
        entries = {name: incoming.read(name) for name in incoming.namelist()}
    manifest = json.loads(entries["manifest.json"])
    mutate(manifest, entries)
    entries["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(target, "w") as outgoing:
        for name, raw in entries.items():
            outgoing.writestr(name, raw)
    return target


@pytest.mark.parametrize("damage", ["bytes", "missing", "extra", "owner", "job_owner", "job_revision", "model_original", "path", "size",
                                    "missing_title", "bad_tags", "snapshot_title", "share_unread", "community_count", "profile",
                                    "community_missing_model", "share_missing_model", "wrong_camera", "snapshot_generation"])
def test_invalid_archives_do_not_publish_a_destination(exported, tmp_path, damage):
    _, archive, _, _, _ = exported
    def mutate(manifest, entries):
        key = manifest["files"][0]["path"]
        if damage == "bytes": entries[key] = b"corrupted"
        elif damage == "missing": entries.pop(key)
        elif damage == "extra": entries["other-account.txt"] = b"unexpected"
        elif damage == "owner": manifest["userId"] = "user-bob"
        elif damage == "job_owner": manifest["jobs"][0]["user_id"] = "user-bob"
        elif damage == "job_revision": manifest["jobs"][0]["input_revision"] += 1
        elif damage == "model_original": manifest["models"][0]["asset_id"] = "missing"
        elif damage == "path":
            manifest["files"][0]["path"] = "../escape"
            entries["../escape"] = entries.pop(key)
        elif damage == "size": manifest["files"][0]["size"] = -1
        elif damage in {"missing_title", "bad_tags"}:
            payload = json.loads(manifest["collections"][0]["payload"])
            if damage == "missing_title": payload.pop("title")
            else: payload["tags"] = [123]
            manifest["collections"][0]["payload"] = json.dumps(payload)
        elif damage == "snapshot_title": manifest["data"]["collections"][0]["title"] = "Different from stored row"
        elif damage == "share_unread": manifest["data"]["shares"][0]["unread"] = "false"
        elif damage == "community_count": manifest["data"]["community"][0]["likes"] = -1
        elif damage == "profile": manifest["data"]["profile"]["name"] = 123
        elif damage == "community_missing_model": manifest["data"]["community"][0].pop("modelId")
        elif damage == "share_missing_model": manifest["data"]["shares"][0]["item"].pop("modelId")
        elif damage == "wrong_camera": manifest["data"]["community"][0]["modelCamera"] = {}
        elif damage == "snapshot_generation": manifest["data"]["collections"][0]["generation"] = "failed"
    broken = rewrite_archive(archive, tmp_path / "broken.zip", mutate)
    with pytest.raises((BackupError, ValueError)):
        restore_account_backup(broken, tmp_path / "not-published", user_id="user-alice")
    assert not (tmp_path / "not-published").exists()
    assert not (tmp_path / "escape").exists()


def test_existing_destination_and_owner_reassignment_are_rejected(exported, tmp_path):
    _, archive, _, _, _ = exported
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("do not replace")
    with pytest.raises(BackupError):
        restore_account_backup(archive, destination, user_id="user-alice")
    assert sentinel.read_text() == "do not replace"
    with pytest.raises(BackupError):
        restore_account_backup(archive, tmp_path / "wrong-owner", user_id="user-bob")


def test_duplicate_zip_names_are_rejected(exported, tmp_path):
    _, archive, _, _, _ = exported
    duplicate = tmp_path / "duplicate.zip"
    duplicate.write_bytes(archive.read_bytes())
    with zipfile.ZipFile(duplicate, "a") as bundle, pytest.warns(UserWarning):
        bundle.writestr("manifest.json", "{}")
    with pytest.raises(BackupError):
        restore_account_backup(duplicate, tmp_path / "duplicate", user_id="user-alice")


def test_interrupted_publication_is_marked_and_cannot_start_as_an_app(exported, tmp_path, monkeypatch):
    _, archive, _, _, _ = exported
    rename = backups.os.rename
    def interrupted(source, target):
        if source.name == "memoverse.sqlite3":
            raise OSError("fixture publication interrupted")
        return rename(source, target)
    monkeypatch.setattr(backups.os, "rename", interrupted)
    destination = tmp_path / "incomplete-restore"
    with pytest.raises(OSError, match="interrupted"):
        restore_account_backup(archive, destination, user_id="user-alice")
    assert (destination / "RESTORE_INCOMPLETE").is_file()
    with pytest.raises(RuntimeError, match="[Ii]ncomplete"):
        create_app(destination, run_worker=False, auth_provider=FakeAuthFactory(),
                   object_store=LocalObjectStore(destination / "objects"), template_dir=tmp_path / "template")


def test_export_never_emits_an_archive_exceeding_its_restore_limits(exported, monkeypatch):
    app, _, _, _, _ = exported
    before = set((app.state.store.root / "exports").iterdir())
    monkeypatch.setattr(backups, "MAX_MANIFEST_BYTES", 16)
    with pytest.raises(BackupError, match="restore limits"):
        create_account_backup(app.state.store, "user-alice")
    assert set((app.state.store.root / "exports").iterdir()) == before


def test_export_stops_an_oversized_stream_before_writing_or_consuming_more(exported, monkeypatch):
    app, _, _, _, _ = exported
    storage = app.state.store.storage
    original_open = storage.open
    events = []
    class Overflow:
        def __init__(self, key):
            reader = original_open(key)
            self.size = reader.size
            reader.close()
        def chunks(self):
            events.append("first")
            yield b"x" * (self.size + 1)
            pytest.fail("Oversized source must stop before reading another chunk")
        def close(self):
            events.append("closed")
    before = set((app.state.store.root / "exports").iterdir())
    monkeypatch.setattr(storage, "open", Overflow)
    with pytest.raises(StorageError, match="exceeds"):
        create_account_backup(app.state.store, "user-alice")
    assert events == ["first", "closed"]
    assert set((app.state.store.root / "exports").iterdir()) == before


def test_symlink_archive_member_is_rejected(exported, tmp_path):
    _, archive, _, _, _ = exported
    linked = tmp_path / "linked.zip"
    with zipfile.ZipFile(archive) as incoming, zipfile.ZipFile(linked, "w") as outgoing:
        for info in incoming.infolist():
            if info.filename.startswith("media/"):
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            outgoing.writestr(info, incoming.read(info))
    with pytest.raises(BackupError, match="ordinary"):
        restore_account_backup(linked, tmp_path / "linked", user_id="user-alice")


@pytest.mark.parametrize("limit", ["MAX_FILES", "MAX_EXPANDED_BYTES", "MAX_MANIFEST_BYTES"])
def test_restore_resource_limits_reject_before_publication(exported, tmp_path, monkeypatch, limit):
    _, archive, _, _, _ = exported
    monkeypatch.setattr(backups, limit, 1)
    with pytest.raises(BackupError):
        restore_account_backup(archive, tmp_path / "limited", user_id="user-alice")
    assert not (tmp_path / "limited").exists()


def test_backup_during_processing_requeues_only_restored_current_task(tmp_path, monkeypatch):
    app, _ = build_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        item = create_collection(client)
        with app.state.store.transaction(True) as db:
            db.execute("UPDATE jobs SET status='processing' WHERE id=?", (item["generationJobId"],))
        archive = create_account_backup(app.state.store, "user-alice")
    destination = tmp_path / "restored-processing"
    assert restore_account_backup(archive, destination, user_id="user-alice")["requeuedJobs"] == 1
    restored = create_app(destination, run_worker=False, auth_provider=FakeAuthFactory(),
                          object_store=LocalObjectStore(destination / "objects"), template_dir=tmp_path / "template")
    monkeypatch.setattr(jobs, "engine_status", lambda: {"available": True, "reason": "fixture"})
    monkeypatch.setattr(relief_engine, "generate_relief", fixture_generate)
    with TestClient(restored, base_url=ORIGIN) as client:
        signed_in(client)
        assert get_item(state(client.get("/api/state")), item["id"])["generation"] == "queued"
        assert restored.state.worker.run_one()
        ready = get_item(state(client.get("/api/state")), item["id"])
        assert ready["generation"] == "ready"
        assert client.get(ready["modelUrl"]).status_code == 200
    with app.state.store.transaction() as db:
        assert db.execute("SELECT status FROM jobs WHERE id=?", (item["generationJobId"],)).fetchone()[0] == "processing"

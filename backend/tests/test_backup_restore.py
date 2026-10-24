"""Account backup preserves schema-3 semantics without reviving deleted media."""
import io
import json
import os
from pathlib import Path
import stat
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend import backups, filesystem
from backend.backups import BackupError, create_account_backup, restore_account_backup
from backend.app import create_app
from backend.object_storage import LocalObjectStore, ObjectReader
from backend.shared_state import RedisSharedState
from account_fixtures import account_snapshot
from account_fixtures import FakeAuthFactory, ORIGIN, USERS, WRITE_HEADERS, signed_in
from test_api import make_app, create, delete, patch, upload, applied


@pytest.fixture
def exported(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        state, payload = create(client, fields={"title": "Private movie"})
        deleted, deleted_payload = create(client, "deleted", asset=upload(client, "blue"))
        assert delete(client, "deleted").status_code == 200
        signed_in(client, "bob")
        create(client, "bob-only", asset=upload(client, "green"))
    archive = create_account_backup(app.state.store, USERS["alice"]["id"])
    return app, archive, payload, deleted_payload


def restored_app(destination):
    return create_app(destination, object_store=LocalObjectStore(destination / "objects"), auth_provider=FakeAuthFactory(), shared_state=RedisSharedState(), run_recovery=False)


def test_backup_is_scoped_and_deleted_only_media_bytes_are_not_exported(exported, tmp_path):
    app, archive, payload, deleted_payload = exported
    with zipfile.ZipFile(archive) as source:
        manifest = json.loads(source.read("manifest.json"))
        assert manifest["user"]["id"] == USERS["alice"]["id"]
        assert {row["id"] for row in manifest["tickets"]} == {"ticket-1", "deleted"}
        assert len(source.namelist()) == 2
        assert not any(deleted_payload["originalAssetId"] in name for name in source.namelist())
        assert not any("auth" in name or "session" in name for name in source.namelist())
    destination = tmp_path / "restored"
    result = restore_account_backup(archive, destination, user_id=USERS["alice"]["id"])
    assert result["verified"] and result["tickets"] == 2 and result["files"] == 1
    with TestClient(restored_app(destination), base_url=ORIGIN) as client:
        signed_in(client)
        state = account_snapshot(client)
        assert [row["id"] for row in state["tickets"]] == ["ticket-1"]
        assert state["tickets"][0]["fields"]["title"] == "Private movie"
        assert applied(client, client.post("/api/tickets", headers=WRITE_HEADERS, json=payload)) == state
        assert applied(client, client.post("/api/tickets", headers=WRITE_HEADERS, json=deleted_payload)) == state
        assert client.post("/api/tickets", headers=WRITE_HEADERS, json={**deleted_payload, "operationId": "revive"}).status_code == 409
        assert client.get("/api/media/" + deleted_payload["originalAssetId"]).status_code == 404
        assert client.get("/api/media/" + payload["originalAssetId"]).status_code == 200
        assert state["capabilities"]["cloudStorage"] is False
        signed_in(client, "bob")
        assert account_snapshot(client)["tickets"] == []
        assert client.get("/api/media/" + payload["originalAssetId"]).status_code == 404
    second_archive = create_account_backup(restored_app(destination).state.store, USERS["alice"]["id"])
    assert second_archive.exists(), "Restored tombstone references need no missing media bytes"


def test_shared_media_is_exported_when_any_reference_remains_live(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        signed_in(client)
        asset = upload(client)
        create(client, "one", asset)
        create(client, "two", asset)
        delete(client, "one")
    archive = create_account_backup(app.state.store, USERS["alice"]["id"])
    with zipfile.ZipFile(archive) as source:
        assert len(source.namelist()) == 2
    assert restore_account_backup(archive, tmp_path / "restored", user_id=USERS["alice"]["id"])["files"] == 1


def rewrite(archive, destination, change):
    with zipfile.ZipFile(archive) as source:
        files = {name: source.read(name) for name in source.namelist()}
    manifest = json.loads(files["manifest.json"])
    change(files, manifest)
    files["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(destination, "w") as target:
        for name, raw in files.items():
            target.writestr(name, raw)


@pytest.mark.parametrize("damage", ["hash", "missing", "path", "owner", "schema", "field", "source", "negative", "duplicate-record", "deleted-bytes", "unknown-member"])
def test_corrupt_backups_never_publish_destination(exported, tmp_path, damage):
    _, archive, _, deleted_payload = exported
    bad = tmp_path / (damage + ".zip")
    def change(files, manifest):
        key = next(name for name in files if name != "manifest.json")
        if damage == "hash":
            files[key] = b"x" * len(files[key])
        elif damage == "missing":
            del files[key]
        elif damage == "path":
            files["../outside"] = files.pop(key)
        elif damage == "owner":
            manifest["tickets"][0]["user_id"] = "other"
        elif damage == "schema":
            manifest["schemaVersion"] = 2
        elif damage == "field":
            manifest["tickets"][0]["fields"] = '{"category":"movie"}'
        elif damage == "source":
            sources = json.loads(manifest["tickets"][0]["field_sources"])
            sources["title"] = "invented"
            manifest["tickets"][0]["field_sources"] = json.dumps(sources)
        elif damage == "negative":
            manifest["tickets"][0]["version"] = -1
        elif damage == "duplicate-record":
            manifest["tickets"].append(manifest["tickets"][0])
        elif damage == "deleted-bytes":
            files["media/" + deleted_payload["originalAssetId"] + ".png"] = b"deleted"
        else:
            files["auth.sqlite3"] = b"secret"
    rewrite(archive, bad, change)
    destination = tmp_path / (damage + "-output")
    with pytest.raises(BackupError):
        restore_account_backup(bad, destination, user_id=USERS["alice"]["id"])
    assert not destination.exists()


def test_destination_and_owner_reassignment_are_rejected(exported, tmp_path):
    _, archive, _, _ = exported
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("keep")
    with pytest.raises(BackupError):
        restore_account_backup(archive, existing, user_id=USERS["alice"]["id"])
    assert (existing / "keep").read_text() == "keep"
    with pytest.raises(BackupError):
        restore_account_backup(archive, tmp_path / "wrong-owner", user_id=USERS["bob"]["id"])


def test_duplicate_and_symlink_members_are_rejected(exported, tmp_path):
    _, archive, _, _ = exported
    with zipfile.ZipFile(archive) as source:
        raw = source.read("manifest.json")
    for kind in ("duplicate", "symlink"):
        bad = tmp_path / (kind + ".zip")
        with zipfile.ZipFile(bad, "w") as target:
            target.writestr("manifest.json", raw)
            if kind == "duplicate":
                with pytest.warns(UserWarning):
                    target.writestr("manifest.json", raw)
            else:
                info = zipfile.ZipInfo("link")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                target.writestr(info, "outside")
        with pytest.raises(BackupError):
            restore_account_backup(bad, tmp_path / (kind + "-out"), user_id=USERS["alice"]["id"])


@pytest.mark.parametrize("limit", ["MAX_MEMBERS", "MAX_TOTAL_BYTES", "MAX_MANIFEST_BYTES"])
def test_archive_limits_apply_to_export_and_restore(exported, tmp_path, monkeypatch, limit):
    app, archive, _, _ = exported
    monkeypatch.setattr(backups, limit, 1)
    with pytest.raises(BackupError):
        restore_account_backup(archive, tmp_path / "restored", user_id=USERS["alice"]["id"])
    with pytest.raises(BackupError):
        create_account_backup(app.state.store, USERS["alice"]["id"])


def test_export_stops_oversized_stream_and_closes_reader(exported, monkeypatch):
    app, _, _, _ = exported
    body = io.BytesIO(b"x" * 2000)
    monkeypatch.setattr(app.state.store.storage, "open", lambda key: ObjectReader(body, 10))
    with pytest.raises(BackupError, match="length"):
        create_account_backup(app.state.store, USERS["alice"]["id"])
    assert body.closed


def test_failed_marker_creation_never_exposes_an_empty_destination(exported, tmp_path, monkeypatch):
    _, archive, _, _ = exported
    destination = tmp_path / "marker-failed"
    original = Path.open
    def fail(path, *args, **kwargs):
        if path.name == "RESTORE_INCOMPLETE":
            assert not destination.exists()
            raise PermissionError("injected marker write denied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(PermissionError, match="injected"):
        restore_account_backup(archive, destination, user_id=USERS["alice"]["id"])
    assert not os.path.lexists(destination)
    assert not list(tmp_path.glob(".account-restore-*"))
    assert archive.is_file()


def test_interrupted_publication_never_exposes_a_partial_destination(exported, tmp_path, monkeypatch):
    _, archive, _, _ = exported
    destination = tmp_path / "not-published"
    def fail(source, target):
        assert target == destination and not target.exists()
        assert source.parent.parent == destination.parent
        assert (source / "RESTORE_INCOMPLETE").is_file()
        assert (source / "keepsite.sqlite3").is_file()
        assert (source / "objects/media").is_dir()
        raise OSError("injected publication failure")
    monkeypatch.setattr(filesystem, "publish_directory_noreplace", fail)
    with pytest.raises(OSError, match="injected"):
        restore_account_backup(archive, destination, user_id=USERS["alice"]["id"])
    assert not os.path.lexists(destination)
    assert not list(tmp_path.glob(".account-restore-*"))


def test_interruption_after_atomic_publication_keeps_guarded_destination(exported, tmp_path, monkeypatch):
    _, archive, _, _ = exported
    destination = tmp_path / "interrupted"
    publish = filesystem.publish_directory_noreplace
    def interrupt(source, target):
        assert not target.exists()
        publish(source, target)
        assert not source.exists()
        assert (target / "RESTORE_INCOMPLETE").is_file()
        assert (target / "keepsite.sqlite3").is_file()
        raise KeyboardInterrupt("injected post-publication interruption")
    monkeypatch.setattr(filesystem, "publish_directory_noreplace", interrupt)
    with pytest.raises(KeyboardInterrupt, match="injected"):
        restore_account_backup(archive, destination, user_id=USERS["alice"]["id"])
    assert (destination / "RESTORE_INCOMPLETE").exists()
    assert not list(tmp_path.glob(".account-restore-*"))
    with pytest.raises(RuntimeError, match="Incomplete"):
        create_app(destination, object_store=object())


def test_marker_removal_failure_keeps_guard_and_all_restored_files(exported, tmp_path, monkeypatch):
    _, archive, payload, _ = exported
    destination = tmp_path / "guard-retained"
    original = Path.unlink
    def fail(path, *args, **kwargs):
        if path == destination / "RESTORE_INCOMPLETE":
            raise PermissionError("injected guard removal denied")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(PermissionError, match="injected"):
        restore_account_backup(archive, destination, user_id=USERS["alice"]["id"])
    assert (destination / "RESTORE_INCOMPLETE").is_file()
    assert (destination / "keepsite.sqlite3").is_file()
    assert next((destination / "objects/media").glob(payload["originalAssetId"] + ".*")).is_file()
    with pytest.raises(RuntimeError, match="Incomplete"):
        create_app(destination, object_store=object())


@pytest.mark.parametrize("kind", ["empty-directory", "directory", "file", "dangling-link"])
def test_destination_appearing_at_publication_is_never_replaced(exported, tmp_path, monkeypatch, kind):
    _, archive, _, _ = exported
    destination = tmp_path / "competing-owner"
    sentinel = b"another owner's data"
    publish = filesystem.publish_directory_noreplace
    identity = []
    def race(source, target):
        if kind in {"empty-directory", "directory"}:
            target.mkdir()
            if kind == "directory": (target / "keep").write_bytes(sentinel)
        elif kind == "file":
            target.write_bytes(sentinel)
        else:
            try: target.symlink_to(tmp_path / "missing-target", target_is_directory=True)
            except OSError as error: pytest.skip(f"Creating a test symlink is not permitted: {error}")
        identity.append(target.lstat().st_ino)
        publish(source, target)
    monkeypatch.setattr(filesystem, "publish_directory_noreplace", race)
    with pytest.raises(OSError):
        restore_account_backup(archive, destination, user_id=USERS["alice"]["id"])
    assert os.path.lexists(destination) and destination.lstat().st_ino == identity[0]
    assert not list(tmp_path.glob(".account-restore-*"))
    if kind == "directory": assert (destination / "keep").read_bytes() == sentinel
    elif kind == "file": assert destination.read_bytes() == sentinel
    elif kind == "empty-directory": assert list(destination.iterdir()) == []
    else: assert destination.is_symlink() and not (tmp_path / "missing-target").exists()


def test_linked_destination_parent_is_rejected_before_staging(exported, tmp_path):
    _, archive, _, _ = exported
    actual = tmp_path / "actual-parent"
    actual.mkdir()
    linked = tmp_path / "linked-parent"
    try: linked.symlink_to(actual, target_is_directory=True)
    except OSError as error: pytest.skip(f"Creating a test symlink is not permitted: {error}")
    with pytest.raises(BackupError, match="links"):
        restore_account_backup(archive, linked / "restored", user_id=USERS["alice"]["id"])
    assert list(actual.iterdir()) == []


def test_restored_tombstone_grants_do_not_authorize_attaching_missing_media(exported, tmp_path):
    _, archive, _, deleted_payload = exported
    destination = tmp_path / "restored-no-revival"
    restore_account_backup(archive, destination, user_id=USERS["alice"]["id"])
    with TestClient(restored_app(destination), base_url=ORIGIN) as client:
        signed_in(client)
        response = client.post("/api/tickets", headers=WRITE_HEADERS,
                               json={**deleted_payload, "id": "new-id", "operationId": "new-op"})
        assert response.status_code == 404
        response = client.patch("/api/tickets/ticket-1", headers=WRITE_HEADERS,
                                json={"originalAssetId": deleted_payload["originalAssetId"], "operationId": "replace-missing"})
        assert response.status_code == 404

"""Offline name migration preserves identities, all files and original state."""
from contextlib import contextmanager, closing
import importlib.util
from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location("keepsite_migration", Path(__file__).parents[1] / "migrate-keepsite.py")
migration = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migration)


@pytest.fixture
def original(tmp_path):
    data = tmp_path / "memoverse-v3"
    data.mkdir()
    with sqlite3.connect(data / "memoverse.sqlite3") as db:
        db.executescript("""
            CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
            INSERT INTO meta VALUES('schema_version','3');
            INSERT INTO meta VALUES('storage_binding','s3:memoverse-real-private-bucket');
            CREATE TABLE users(id TEXT PRIMARY KEY,email TEXT);
            INSERT INTO users VALUES('canonical-issuer-sub-id','alice@example.test');
            CREATE TABLE tickets(user_id TEXT,id TEXT,fields TEXT);
            INSERT INTO tickets VALUES('canonical-issuer-sub-id','ticket-1','保留电影票');
        """)
    with sqlite3.connect(data / "auth.sqlite3") as db:
        db.execute("CREATE TABLE sessions(id TEXT,user_id TEXT)")
        db.execute("INSERT INTO sessions VALUES('session-unchanged','canonical-issuer-sub-id')")
    for name, raw in {"objects/media/original.png": b"original image", "objects/media/poster.jpg": b"poster", "exports/account.zip": b"backup zip", "migration/source-schema2.sqlite3": None}.items():
        path = data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if raw is None:
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE retained(value TEXT)")
                db.execute("INSERT INTO retained VALUES('original schema2 backup')")
        else:
            path.write_bytes(raw)
    env = tmp_path / "memoverse.env"
    env.write_text(f"MEMOVERSE_DATA_DIR={data.as_posix()}\nMEMOVERSE_STORAGE=s3\nMEMOVERSE_S3_BUCKET=memoverse-real-private-bucket\nMEMOVERSE_COGNITO_USER_POOL_ID=memoverse-users\nMEMOVERSE_SESSION_KEY=unchanged-session-key\nAWS_REGION=ap-northeast-1\n", encoding="utf-8")
    return data, env, tmp_path / "keepsite-v3"


@contextmanager
def database_locks(databases):
    # Exercise actual SQLite locks on Windows too, replacing only Linux/systemd.
    connections = []
    try:
        for path in databases:
            connection = migration.sqlite_connection(path, writable=True)
            connections.append(connection)
            connection.execute("BEGIN IMMEDIATE")
        yield
    finally:
        for connection in connections:
            connection.close()


def execute(original):
    with patch.object(migration, "offline_locks", database_locks), patch.object(migration, "require_stopped_services"), patch.object(migration, "service_identity", return_value=(123, 123)), patch.object(migration, "apply_service_ownership"):
        return migration.migrate(*original, execute=True)


def test_plan_does_not_write_or_print_secrets(original):
    source, env, destination = original
    before = migration.source_files(source), env.read_bytes()
    result = migration.migrate(*original)
    assert result["mode"] == "plan"
    assert not destination.exists()
    assert before == (migration.source_files(source), env.read_bytes())
    assert "unchanged-session-key" not in str(result)


def test_execute_preserves_accounts_auth_media_backups_and_source(original):
    source, env, destination = original
    before = migration.source_files(source), env.read_bytes()
    result = execute(original)
    assert before == (migration.source_files(source), env.read_bytes())
    assert not (destination / "memoverse.sqlite3").exists()
    assert not (destination / "RESTORE_INCOMPLETE").exists()
    for database, query, expected in [
        ("keepsite.sqlite3", "SELECT * FROM users", [("canonical-issuer-sub-id", "alice@example.test")]),
        ("keepsite.sqlite3", "SELECT value FROM meta WHERE key='storage_binding'", [("s3:memoverse-real-private-bucket",)]),
        ("auth.sqlite3", "SELECT * FROM sessions", [("session-unchanged", "canonical-issuer-sub-id")]),
        ("migration/source-schema2.sqlite3", "SELECT * FROM retained", [("original schema2 backup",)]),
    ]:
        with closing(sqlite3.connect(destination / database)) as db:
            assert db.execute(query).fetchall() == expected
    for name in ("objects/media/original.png", "objects/media/poster.jpg", "exports/account.zip", "migration/source-schema2.sqlite3"):
        assert (destination / name).read_bytes() == (source / name).read_bytes()
    converted = (destination / "keepsite-migration/keepsite.env").read_text()
    assert f"KEEPSITE_DATA_DIR={destination.as_posix()}" in converted
    assert "MEMOVERSE_" not in converted
    assert "KEEPSITE_S3_BUCKET=memoverse-real-private-bucket" in converted
    assert "KEEPSITE_COGNITO_USER_POOL_ID=memoverse-users" in converted
    assert "KEEPSITE_SESSION_KEY=unchanged-session-key" in converted
    assert result["authDatabase"] == "auth.sqlite3"


def test_existing_destination_never_changed(original):
    destination = original[2]
    destination.mkdir()
    (destination / "keep").write_text("preserve")
    with pytest.raises(migration.MigrationError, match="already exists"):
        execute(original)
    assert (destination / "keep").read_text() == "preserve"


def test_local_pending_upload_identity_moves_without_changing_account(original):
    source, _env, destination = original
    with sqlite3.connect(source / "memoverse.sqlite3") as db:
        db.execute("UPDATE meta SET value='local' WHERE key='storage_binding'")
        db.execute("CREATE TABLE upload_intents(user_id TEXT,storage_identity TEXT)")
        db.execute("INSERT INTO upload_intents VALUES(?,?)", ("canonical-issuer-sub-id", "local:" + str((source / "objects").resolve())))
    execute(original)
    with closing(sqlite3.connect(destination / "keepsite.sqlite3")) as db:
        assert db.execute("SELECT * FROM upload_intents").fetchall() == [("canonical-issuer-sub-id", "local:" + str((destination / "objects").resolve()))]
    with closing(sqlite3.connect(source / "memoverse.sqlite3")) as db:
        assert db.execute("SELECT storage_identity FROM upload_intents").fetchone()[0] == "local:" + str((source / "objects").resolve())


def test_publication_race_never_clobbers_empty_destination(original):
    from backend import filesystem
    publish = filesystem.publish_directory_noreplace

    def competing(staging, destination):
        destination.mkdir()
        publish(staging, destination)

    with patch.object(filesystem, "publish_directory_noreplace", competing), pytest.raises(OSError):
        execute(original)
    assert list(original[2].iterdir()) == []
    failed = list(original[2].parent.glob(".keepsite-v3.migration-*"))
    assert len(failed) == 1 and (failed[0] / "RESTORE_INCOMPLETE").exists()


def test_existing_writer_stops_migration_without_publication(original):
    with closing(migration.sqlite_connection(original[0] / "auth.sqlite3", writable=True)) as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            execute(original)
    assert not original[2].exists()


def test_writer_cannot_enter_during_file_copy(original):
    copy = migration.shutil.copyfile
    attempted = []

    def check_locked(source, destination):
        with closing(migration.sqlite_connection(original[0] / "auth.sqlite3", writable=True)) as db:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                db.execute("BEGIN IMMEDIATE")
        attempted.append(True)
        return copy(source, destination)

    with patch.object(migration.shutil, "copyfile", check_locked):
        execute(original)
    assert attempted


def test_file_mutation_stops_publication(original):
    copy = migration.shutil.copyfile

    def mutate(source, destination):
        result = copy(source, destination)
        if source.name == "original.png":
            source.write_bytes(b"concurrent write")
        return result

    with patch.object(migration.shutil, "copyfile", mutate), pytest.raises(migration.MigrationError, match="changed"):
        execute(original)
    assert not original[2].exists()


@pytest.mark.parametrize("extra", ["KEEPSITE_S3_BUCKET=other\n", "MEMOVERSE_S3_BUCKET=duplicate\n", "export BAD=value\n"])
def test_environment_collision_and_shell_input_rejected_before_copy(original, extra):
    with original[1].open("a") as stream:
        stream.write(extra)
    with pytest.raises(migration.MigrationError):
        execute(original)
    assert not original[2].exists()


def test_path_rewrites_are_explicit_and_do_not_rename_resource_values():
    env = "MEMOVERSE_DATA_DIR=/var/lib/memoverse-v3\nMEMOVERSE_OCR_MODEL_DIR=/opt/memoverse/shared/ocr\nMEMOVERSE_S3_BUCKET=memoverse-real\n"
    with pytest.raises(migration.MigrationError, match="path-map"):
        migration.convert_environment(env, Path("/var/lib/memoverse-v3"), Path("/var/lib/keepsite-v3"))
    converted = migration.convert_environment(env, Path("/var/lib/memoverse-v3"), Path("/var/lib/keepsite-v3"), [("/opt/memoverse/shared", "/opt/keepsite/shared")])
    assert "KEEPSITE_OCR_MODEL_DIR=/opt/keepsite/shared/ocr" in converted
    assert "KEEPSITE_S3_BUCKET=memoverse-real" in converted


def test_live_service_blocks_execution(original):
    with patch.object(migration, "offline_locks") as locks, patch.object(migration, "service_identity", return_value=(123, 123)):
        locks.side_effect = migration.MigrationError("Stop memoverse.service")
        with pytest.raises(migration.MigrationError, match="Stop"):
            migration.migrate(*original, execute=True)
    assert not original[2].exists()


def test_schema2_rejected_without_implicit_upgrade(original):
    with sqlite3.connect(original[0] / "memoverse.sqlite3") as db:
        db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    with pytest.raises(migration.MigrationError, match="Only schema 3"):
        execute(original)
    assert not original[2].exists()


def test_ordinary_files_with_journal_suffix_are_preserved(original):
    source = original[0]
    (source / "exports/photo").write_bytes(b"original")
    (source / "exports/photo-wal").write_bytes(b"ordinary backup")
    execute(original)
    assert (original[2] / "exports/photo-wal").read_bytes() == b"ordinary backup"


def test_inventory_change_before_locking_is_rejected(original):
    @contextmanager
    def changed(_databases):
        (original[0] / "added-file").write_text("unexpected addition")
        yield

    with patch.object(migration, "offline_locks", changed), patch.object(migration, "service_identity", return_value=(123, 123)):
        with pytest.raises(migration.MigrationError, match="inventory changed"):
            migration.migrate(*original, execute=True)
    assert not original[2].exists()


def test_runtime_ownership_excludes_restricted_migration_metadata(tmp_path):
    runtime = tmp_path / "data"
    (runtime / "objects").mkdir(parents=True)
    (runtime / "objects/image.png").write_bytes(b"image")
    (runtime / "keepsite-migration").mkdir()
    secret = runtime / "keepsite-migration/keepsite.env"
    secret.write_text("SECRET=preserved")
    with patch.object(migration.os, "chown", create=True) as chown:
        migration.apply_service_ownership(runtime, (123, 456))
    changed = {call.args[0] for call in chown.call_args_list}
    assert changed == {runtime, runtime / "objects", runtime / "objects/image.png"}
    assert secret not in changed


def test_service_check_accepts_only_stopped_or_missing_new_unit():
    import subprocess

    stopped = subprocess.CompletedProcess([], 0, "LoadState=loaded\nActiveState=inactive\nMainPID=0\n", "")
    missing = subprocess.CompletedProcess([], 1, "LoadState=not-found\nActiveState=inactive\nMainPID=0\n", "")
    running = subprocess.CompletedProcess([], 0, "LoadState=loaded\nActiveState=active\nMainPID=123\n", "")
    with patch.object(migration.sys, "platform", "linux"), patch.object(migration.subprocess, "run", side_effect=[stopped, missing]):
        migration.require_stopped_services()
    with patch.object(migration.sys, "platform", "linux"), patch.object(migration.subprocess, "run", return_value=running):
        with pytest.raises(migration.MigrationError, match="Stop"):
            migration.require_stopped_services()

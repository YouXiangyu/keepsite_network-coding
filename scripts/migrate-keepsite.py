#!/usr/bin/env python3
"""Offline, non-destructive schema-3 name migration; default is read-only inventory.

Execution requires both server application units stopped. It holds both deployment
locks and SQLite writer reservations throughout copying, validation and publication.
No service, timer, DNS or cloud configuration is changed by this tool.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OLD_DATABASE = "memoverse.sqlite3"
NEW_DATABASE = "keepsite.sqlite3"
PATH_KEYS = {"KEEPSITE_DATA_DIR", "KEEPSITE_OCR_MODEL_DIR", "TMPDIR", "PIP_CACHE_DIR"}
DEPLOYMENT_LOCKS = (Path("/opt/memoverse/activation-backups/.lock"), Path("/opt/keepsite/activation-backups/.lock"))


class MigrationError(Exception):
    pass


def no_links(path: Path):
    for part in (path, *path.parents):
        if part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction()):
            raise MigrationError("Source/destination paths must not contain links.")


def digest(path: Path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def source_files(source: Path):
    no_links(source)
    if not source.is_dir():
        raise MigrationError("Source data directory must exist.")
    result = {}
    for path in source.rglob("*"):
        no_links(path)
        if path.is_dir():
            continue
        if not stat.S_ISREG(path.stat().st_mode):
            raise MigrationError("Only regular source files/directories are accepted.")
        name = path.relative_to(source).as_posix()
        # SQLite backup consumes committed WAL; never copy live journal files.
        if name in {database + suffix for database in (OLD_DATABASE, "auth.sqlite3") for suffix in ("-wal", "-shm", "-journal")}:
            if not (source / name.rsplit("-", 1)[0]).is_file():
                raise MigrationError("Unrecognized operational SQLite journal file.")
            continue
        result[name] = digest(path)
    if OLD_DATABASE not in result or NEW_DATABASE in result:
        raise MigrationError("Require only the original memoverse.sqlite3; target database must not already exist.")
    if "RESTORE_INCOMPLETE" in result or any(name.startswith("keepsite-migration/") for name in result):
        raise MigrationError("Source is incomplete or already migrated.")
    return result


def sqlite_connection(path: Path, *, writable=False):
    return sqlite3.connect(path.resolve().as_uri() + ("?mode=rw" if writable else "?mode=ro"), uri=True, timeout=0)


def inspect_database(path: Path, *, schema3=False):
    with closing(sqlite_connection(path)) as db:
        if [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"] or db.execute("PRAGMA foreign_key_check").fetchall():
            raise MigrationError("Database integrity or foreign-key check failed.")
        if schema3 and db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() != ("3",):
            raise MigrationError("Only schema 3 is renamed. Convert schema 1/2 with the separate reviewed migration first.")
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: db.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in tables}


def convert_environment(text: str, source: Path, destination: Path, path_maps=()):
    """Rename variable keys, and explicitly approved path values only; no shell eval."""
    mapped = [(source.as_posix(), destination.as_posix()), *path_maps]
    keys, lines = set(), []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            lines.append(line)
            continue
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
        if not match:
            raise MigrationError("Environment requires one literal KEY=value per line; shell statements are not accepted.")
        old_key, raw = match.groups()
        key = "KEEPSITE_" + old_key[len("MEMOVERSE_"):] if old_key.startswith("MEMOVERSE_") else old_key
        if key in keys:
            raise MigrationError("Environment keys collide after migration; resolve duplicate variables explicitly.")
        keys.add(key)
        quote = raw[:1] if raw[:1] in {"'", '"'} else ""
        if quote and (len(raw) < 2 or raw[-1] != quote):
            raise MigrationError("Unterminated environment value.")
        value = raw[1:-1] if quote else raw
        if any(char in value for char in ("\\", "\x00", "\r")) or "${" in value or "$(" in value:
            raise MigrationError("Escaped/expanded environment values require manual review.")
        if key in PATH_KEYS:
            for old, new in sorted(mapped, key=lambda pair: len(pair[0]), reverse=True):
                if value == old or value.startswith(old.rstrip("/") + "/"):
                    value = new.rstrip("/") + value[len(old.rstrip("/")):]
                    break
            if key == "KEEPSITE_DATA_DIR" and value != destination.as_posix():
                raise MigrationError("Configured data directory does not match the explicit source/destination pair.")
            if "/memoverse" in value:
                raise MigrationError("A path still refers to the old installation; provide an explicit --path-map.")
        lines.append(f"{key}={quote}{value}{quote}")
    if "KEEPSITE_DATA_DIR" not in keys:
        raise MigrationError("Environment must explicitly specify the source data directory.")
    return "\n".join(lines) + "\n"


def require_stopped_services():
    if sys.platform != "linux":
        raise MigrationError("Execute on the offline Linux server; inventory is available on any platform.")
    for unit in ("memoverse.service", "keepsite.service"):
        result = subprocess.run(["systemctl", "show", unit, "--property=LoadState,ActiveState,MainPID", "--no-pager"],
                                check=False, capture_output=True, text=True, timeout=10)
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        missing_new = unit == "keepsite.service" and values.get("LoadState") == "not-found" and result.returncode in (0, 1)
        if (result.returncode and not missing_new) or values.get("ActiveState") not in {"inactive", "failed"} or values.get("MainPID") != "0":
            raise MigrationError(f"Stop {unit} and all manual application/worker processes before migration.")


@contextmanager
def offline_locks(databases):
    import fcntl

    with ExitStack() as stack:
        for path in DEPLOYMENT_LOCKS:
            no_links(path)
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            stream = stack.enter_context(path.open("a"))
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require_stopped_services()
        for path in databases:
            db = stack.enter_context(closing(sqlite_connection(path, writable=True)))
            # This reserves the writer slot in WAL and rollback-journal modes.
            # Backup uses a separate read-only connection to avoid backup hanging
            # on its own pending transaction. Reservations release on close.
            db.execute("BEGIN IMMEDIATE")
        yield


def service_identity(user: str):
    if sys.platform != "linux":
        raise MigrationError("Execute on the offline Linux server; inventory is available on any platform.")
    import pwd

    try:
        identity = pwd.getpwnam(user)
    except KeyError as error:
        raise MigrationError("Provision the keepsite system user/group before migration; do not initialize the data directory.") from error
    if identity.pw_uid == 0:
        raise MigrationError("The application service user must not be root.")
    return identity.pw_uid, identity.pw_gid


def apply_service_ownership(staging: Path, identity):
    uid, gid = identity
    # Secret-bearing migration metadata stays root-owned and inaccessible to the
    # application. Runtime files are readable/writable by the new service user.
    for path in (staging, *staging.rglob("*")):
        if path == staging / "keepsite-migration" or (staging / "keepsite-migration") in path.parents:
            continue
        os.chown(path, uid, gid, follow_symlinks=False)


def validate_paths(source: Path, environment: Path, destination: Path):
    if not all(path.is_absolute() for path in (source, environment, destination)):
        raise MigrationError("All paths must be explicit absolute paths.")
    for path in (source, environment, destination):
        no_links(path)
    if os.path.lexists(destination):
        raise MigrationError("Destination already exists; it will never be overwritten.")
    if not destination.parent.is_dir() or destination.resolve().is_relative_to(source.resolve()):
        raise MigrationError("Destination parent must exist outside the source tree.")
    if not environment.is_file():
        raise MigrationError("Source environment file must exist.")


def migrate(source: Path, environment: Path, destination: Path, *, path_maps=(), execute=False, service_user="keepsite"):
    validate_paths(source, environment, destination)
    original_env = environment.read_bytes()
    converted_env = convert_environment(original_env.decode("utf-8"), source, destination, path_maps)
    files = source_files(source)
    counts = inspect_database(source / OLD_DATABASE, schema3=True)
    report = {"mode": "execute" if execute else "plan", "source": str(source), "destination": str(destination),
              "files": len(files), "tables": counts, "authDatabase": "auth.sqlite3" if "auth.sqlite3" in files else "absent",
              "environment": str(destination / "keepsite-migration/keepsite.env"),
              "cloudResources": "Unchanged; bucket, object keys, region, IAM and Cognito values are preserved."}
    if not execute:
        return report
    identity = service_identity(service_user)
    databases = [source / name for name in (OLD_DATABASE, "auth.sqlite3") if name in files]
    with offline_locks(databases):
        # Repeat inventory only after all writer reservations have been acquired.
        if source_files(source) != files:
            raise MigrationError("Source file inventory changed before the offline locks were acquired.")
        staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.migration-", dir=destination.parent))
        marker = staging / "RESTORE_INCOMPLETE"
        marker.write_text("Offline migration incomplete; do not start.\n", encoding="utf-8")
        try:
            for folder in source.rglob("*"):
                if folder.is_dir():
                    (staging / folder.relative_to(source)).mkdir(parents=True, exist_ok=True)
                    (staging / folder.relative_to(source)).chmod(0o700)
            for name, checksum in files.items():
                incoming = source / name
                outgoing = staging / (NEW_DATABASE if name == OLD_DATABASE else name)
                outgoing.parent.mkdir(parents=True, exist_ok=True)
                if incoming in databases:
                    with closing(sqlite_connection(incoming)) as source_db, closing(sqlite3.connect(outgoing)) as target_db:
                        source_db.backup(target_db)
                        target_db.execute("PRAGMA journal_mode=DELETE")
                    if inspect_database(incoming, schema3=name == OLD_DATABASE) != inspect_database(outgoing, schema3=name == OLD_DATABASE):
                        raise MigrationError("Snapshot table counts changed.")
                    if name == OLD_DATABASE:
                        with closing(sqlite3.connect(outgoing)) as target_db:
                            binding = target_db.execute("SELECT value FROM meta WHERE key='storage_binding'").fetchone()
                            if binding == ("local",):
                                tables = {row[0] for row in target_db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                                if "upload_intents" in tables:
                                    # This is a durable absolute-path identity, not
                                    # an account ID or an object key. S3 is untouched.
                                    target_db.execute("UPDATE upload_intents SET storage_identity=? WHERE storage_identity=?", (
                                        "local:" + str((destination / "objects").resolve()),
                                        "local:" + str((source / "objects").resolve()),
                                    ))
                                    target_db.commit()
                else:
                    shutil.copyfile(incoming, outgoing)
                    if digest(outgoing) != checksum:
                        raise MigrationError("File changed while copying; destination was not published.")
                outgoing.chmod(0o600)
            if source_files(source) != files or environment.read_bytes() != original_env:
                raise MigrationError("Source changed during migration; destination was not published.")
            metadata = staging / "keepsite-migration"
            metadata.mkdir(mode=0o700)
            (metadata / "keepsite.env").write_text(converted_env, encoding="utf-8")
            (metadata / "keepsite.env").chmod(0o600)
            # Store the original restricted config for recovery, never print it.
            (metadata / "source.env").write_bytes(original_env)
            (metadata / "source.env").chmod(0o600)
            report["snapshotSha256"] = {path.relative_to(staging).as_posix(): digest(path) for path in staging.rglob("*") if path.is_file() and path != marker and path.parent != metadata}
            (metadata / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            (metadata / "report.json").chmod(0o600)
            apply_service_ownership(staging, identity)
            require_stopped_services()
            from backend.filesystem import publish_directory_noreplace
            # Staging is private. Remove marker only once fully validated, before
            # atomic publication; a crash cannot expose an incomplete final tree.
            marker.unlink()
            publish_directory_noreplace(staging, destination)
        except BaseException:
            if staging.is_dir():
                marker.write_text("Migration failed; preserve for inspection. Do not start.\n", encoding="utf-8")
            raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data", type=Path, required=True)
    parser.add_argument("--source-env", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--path-map", action="append", default=[], metavar="OLD=NEW", help="Explicit path-prefix conversion, restricted to filesystem environment keys")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--service-user", default="keepsite", help="Existing system user owning the new runtime data; default keepsite")
    args = parser.parse_args()
    try:
        maps = []
        for mapping in args.path_map:
            if "=" not in mapping:
                raise MigrationError("--path-map requires OLD=NEW.")
            old, new = mapping.split("=", 1)
            if not old.startswith("/") or not new.startswith("/") or old == "/":
                raise MigrationError("Path maps require explicit non-root absolute path prefixes.")
            maps.append((old.rstrip("/"), new.rstrip("/")))
        print(json.dumps(migrate(args.source_data, args.source_env, args.destination, path_maps=maps, execute=args.execute, service_user=args.service_user), indent=2))
    except (MigrationError, OSError, sqlite3.Error, UnicodeError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Migration stopped: {error}\n")


if __name__ == "__main__":
    main()

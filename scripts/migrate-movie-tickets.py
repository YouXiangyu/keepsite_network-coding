#!/usr/bin/env python3
"""Inventory a closed schema-2 snapshot; explicitly migrate a verified local copy.

This tool never connects to S3 and never changes the source database or files.
The complete old snapshot is retained alongside the new, isolated schema-3 data.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class MigrationError(ValueError):
    pass


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read_snapshot(source: Path):
    source = source.resolve(strict=True)
    if not source.is_file():
        raise MigrationError("Source must be an offline SQLite snapshot file.")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(source) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise MigrationError("Source has a journal/WAL. Export a consistent offline snapshot first.")
    checksum = digest(source)
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        try:
            schema = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if not schema or schema[0] != "2":
                raise MigrationError("Only an explicit schema-2 snapshot is accepted.")
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or db.execute("PRAGMA foreign_key_check").fetchall():
                raise MigrationError("Source integrity or foreign-key validation failed.")
            users = [dict(row) for row in db.execute("SELECT * FROM users ORDER BY id")]
            assets = {row["id"]: dict(row) for row in db.execute("SELECT * FROM assets ORDER BY id")}
            grants = {(row[0], row[1]) for row in db.execute("SELECT user_id,asset_id FROM asset_grants")}
            records = [dict(row) for row in db.execute("SELECT * FROM collections ORDER BY user_id,id")]
        except sqlite3.DatabaseError as error:
            raise MigrationError("Source does not have a complete schema-2 database.") from error
    if checksum != digest(source):
        raise MigrationError("Source changed while reading. Use a closed, immutable snapshot.")
    user_ids = {user["id"] for user in users}
    movies, excluded = [], 0
    for row in records:
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError) as error:
            raise MigrationError("Invalid source collection payload; source has not been changed.") from error
        if not isinstance(payload, dict):
            raise MigrationError("Source collection payload must be an object.")
        if payload.get("category") != "movie":
            excluded += 1
            continue
        if row["user_id"] not in user_ids or (row["user_id"], row["asset_id"]) not in grants or row["asset_id"] not in assets:
            raise MigrationError("A movie has no verified owner or original-photo grant.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", row["id"]):
            raise MigrationError("A movie identifier is invalid; resolve it in an isolated copy first.")
        title = payload.get("title", "")
        if not isinstance(title, str) or len(title.strip()) > 120:
            raise MigrationError("A movie title needs manual review in an isolated copy.")
        movies.append({**row, "legacy": payload, "title": title.strip()})
    return {"source": source, "sha256": checksum, "users": users, "assets": assets, "movies": movies, "excluded": excluded}


def inventory(snapshot):
    referenced = sorted({row["asset_id"] for row in snapshot["movies"]})
    return {
        "sourceSchema": 2,
        "targetSchema": 3,
        "sourceSha256": snapshot["sha256"],
        "accounts": len(snapshot["users"]),
        "movieTickets": len(snapshot["movies"]),
        "originalPhotos": [{"assetId": key, "objectKey": snapshot["assets"][key]["object_key"]} for key in referenced],
        "excludedCollections": snapshot["excluded"],
        "preservation": "Complete schema-2 database retained; no source files or cloud objects deleted.",
        "reviewRequired": [
            "Only category=movie collections are selected; verify they are actual movie tickets.",
            "Only the legacy title is imported. A collection date is not evidence of a screening time.",
            "Other fields, social data and model references remain in the full source snapshot.",
            "Acquire and verify original-photo copies separately before migration; this tool has no cloud credentials.",
        ],
    }


def verified_originals(snapshot, media_root: Path):
    from backend.media import image_info
    from backend.object_storage import LocalObjectStore

    if not media_root.is_dir():
        raise MigrationError("Source media directory does not exist.")
    # Constructing LocalObjectStore on this existing directory does not write files.
    storage = LocalObjectStore(media_root)
    originals = []
    for asset_id in sorted({row["asset_id"] for row in snapshot["movies"]}):
        asset = snapshot["assets"][asset_id]
        if not re.fullmatch(r"[a-f0-9]{64}", asset_id) or not asset["object_key"].startswith("media/"):
            raise MigrationError("An original-photo identity or object prefix is invalid.")
        path = storage._path(asset["object_key"])
        if not path.is_file() or path.stat().st_size > 12 * 1024 * 1024:
            raise MigrationError("An original is missing or exceeds the supported image size.")
        info = image_info(path.read_bytes())
        if (info["assetId"], info["mimeType"], info["size"], info["width"], info["height"]) != (
            asset_id, asset["mime"], asset["size"], asset["width"], asset["height"]
        ):
            raise MigrationError("An original photo does not match its recorded hash and image metadata.")
        originals.append((asset, path))
    return originals


def migrate(source: Path, media_root: Path, destination: Path):
    from backend.object_storage import LocalObjectStore
    from backend.store import Store, encode, now_ms

    snapshot = read_snapshot(source)
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise MigrationError("Destination must be a new directory; no existing data will be replaced.")
    if not destination.parent.is_dir():
        raise MigrationError("Destination parent must exist.")
    if any(parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()) for parent in destination.parents):
        raise MigrationError("Destination ancestors must not contain links.")
    if destination.resolve().is_relative_to(media_root.resolve()):
        raise MigrationError("Destination must be outside the source media directory.")
    originals = verified_originals(snapshot, media_root)
    # A crash during Store initialization must never publish a startable empty
    # destination. Build a distinct sibling and publish only after validation.
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}.migration-", dir=destination.parent))
    marker = staging / "RESTORE_INCOMPLETE"
    published = False
    try:
        store = Store(staging, storage=LocalObjectStore(staging / "objects"))
        marker.write_text("Isolated migration incomplete; do not start this directory.\n", encoding="utf-8")
        archive = staging / "migration"
        archive.mkdir()
        shutil.copyfile(snapshot["source"], archive / "source-schema2.sqlite3")
        if digest(archive / "source-schema2.sqlite3") != snapshot["sha256"]:
            raise MigrationError("Source snapshot changed before it could be archived.")
        for asset, path in originals:
            store.storage.put_file(asset["object_key"], path, asset["id"], asset["mime"])
        timestamp = now_ms()
        with store.transaction(True) as db:
            for user in snapshot["users"]:
                db.execute("INSERT INTO users(id,email,name,revision,created_at) VALUES (?,?,?,?,?)", (
                    user["id"], user["email"], user["name"], 1, user["created_at"],
                ))
            for asset, _path in originals:
                store.register_asset(db, {"id": asset["id"], "path": asset["object_key"], "mimeType": asset["mime"],
                                          "size": asset["size"], "width": asset["width"], "height": asset["height"]})
            for movie in snapshot["movies"]:
                fields = {"title": movie["title"], "cinema": "", "hall": "", "seat": "", "showtime": ""}
                sources = {key: "migration" if value else "empty" for key, value in fields.items()}
                created = movie["legacy"].get("addedAt", timestamp)
                if type(created) is not int or created < 0:
                    created = timestamp
                operation = "migration-" + hashlib.sha256((snapshot["sha256"] + "\0" + movie["user_id"] + "\0" + movie["id"]).encode()).hexdigest()
                db.execute("INSERT OR IGNORE INTO asset_grants VALUES (?,?)", (movie["user_id"], movie["asset_id"]))
                db.execute("""INSERT INTO tickets(user_id,id,fields,field_sources,original_asset_id,poster_asset_id,
                           version,input_version,created_at,updated_at,deleted_at,operation_id)
                           VALUES (?,?,?,?,?,NULL,1,1,?,?,NULL,?)""", (
                    movie["user_id"], movie["id"], encode(fields), encode(sources), movie["asset_id"], created, max(created, timestamp), operation,
                ))
            for user in snapshot["users"]:
                store.ensure_sync_baseline(db, user["id"])
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise MigrationError("Migrated references failed validation.")
        with closing(store.connect()) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise MigrationError("Migrated database failed integrity validation.")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        report = inventory(snapshot)
        report.update(status="isolated-copy-verified", mediaStorage="local-only", originalBytes=sum(a["size"] for a, _p in originals))
        (archive / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if os.path.lexists(destination):
            raise MigrationError("Destination appeared during migration; it was not changed.")
        if staging.resolve().parent != destination.parent.resolve():
            raise MigrationError("Staging directory escaped the intended destination parent.")
        os.rename(staging, destination)
        published = True
        (destination / "RESTORE_INCOMPLETE").unlink()
        return report
    except BaseException:
        marker = (destination if published else staging) / "RESTORE_INCOMPLETE"
        marker.write_text("Migration failed. Keep this isolated directory for inspection; do not start it.\n", encoding="utf-8")
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-snapshot", type=Path, required=True)
    parser.add_argument("--source-media", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--execute", action="store_true", help="Create a verified isolated schema-3 copy; default is inventory only.")
    args = parser.parse_args()
    try:
        if args.execute:
            if not args.source_media or not args.destination:
                parser.error("--execute requires --source-media and --destination")
            result = migrate(args.source_snapshot, args.source_media, args.destination)
        else:
            result = inventory(read_snapshot(args.source_snapshot))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (MigrationError, OSError, ValueError) as error:
        parser.exit(1, f"Migration stopped: {error}\n")


if __name__ == "__main__":
    main()

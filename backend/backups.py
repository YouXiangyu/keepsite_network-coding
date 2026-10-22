"""Validated schema-3 account backup and isolated local restoration."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
import zipfile

from fastapi import HTTPException
from pydantic import TypeAdapter, ValidationError

from .contracts import FIELD_NAMES, Identifier, TicketFields
from .media import image_info, MAX_IMAGE_BYTES
from .object_storage import LocalObjectStore, StorageError
from .state import account_state
from .store import Store, encode

MAX_MEMBERS = 50_000
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
HASH = re.compile(r"^[a-f0-9]{64}$")
COLUMNS = {
    "users": "id,email,name,revision,created_at",
    "assets": "id,object_key,mime,size,width,height,created_at",
    "tickets": "user_id,id,fields,field_sources,original_asset_id,poster_asset_id,version,input_version,created_at,updated_at,deleted_at,operation_id",
    "operations": "user_id,id,fingerprint,created_at",
}


class BackupError(ValueError):
    pass


def _integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise BackupError("Invalid integer")


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise BackupError("Duplicate JSON key")
            result[key] = value
        return result
    def constant(value):
        raise BackupError("Invalid JSON constant")
    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


def snapshot_account(store, user_id):
    with store.transaction() as db:
        user = dict(store.require_user(db, user_id))
        tickets = [dict(row) for row in db.execute("SELECT * FROM tickets WHERE user_id=? ORDER BY id", (user_id,))]
        operations = [dict(row) for row in db.execute("SELECT * FROM operations WHERE user_id=? ORDER BY id", (user_id,))]
        identifiers = {row[key] for row in tickets for key in ("original_asset_id", "poster_asset_id") if row[key]}
        assets = [dict(store.asset(db, user_id, identifier)) for identifier in sorted(identifiers)]
    return {"schemaVersion": 3, "user": user, "tickets": tickets, "operations": operations, "assets": assets}


def _validate(manifest, user_id):
    if not isinstance(manifest, dict) or set(manifest) != {"schemaVersion", "user", "tickets", "operations", "assets"} or manifest["schemaVersion"] != 3:
        raise BackupError("Unsupported backup format")
    user = manifest["user"]
    if set(user) != set(COLUMNS["users"].split(",")) or user["id"] != user_id or not isinstance(user_id, str) or not user_id or len(user_id) > 512:
        raise BackupError("Account ownership mismatch")
    if not isinstance(user["name"], str) or not isinstance(user["email"], str):
        raise BackupError("Invalid account metadata")
    _integer(user["revision"])
    _integer(user["created_at"])
    indexed = {}
    for name in ("assets", "tickets", "operations"):
        rows = manifest[name]
        if not isinstance(rows, list) or len(rows) > MAX_MEMBERS:
            raise BackupError("Invalid record count")
        index = {}
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(COLUMNS[name].split(",")) or row["id"] in index:
                raise BackupError("Invalid or duplicate record")
            index[row["id"]] = row
        indexed[name] = index
    assets = indexed["assets"]
    for asset in assets.values():
        if not isinstance(asset["id"], str) or not HASH.fullmatch(asset["id"]):
            raise BackupError("Invalid media hash")
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(asset["mime"])
        if not suffix or asset["object_key"] != "media/" + asset["id"] + suffix:
            raise BackupError("Invalid media path")
        for key in ("size", "width", "height"):
            _integer(asset[key], 1)
        _integer(asset["created_at"])
        if asset["size"] > MAX_IMAGE_BYTES or asset["width"] * asset["height"] > 40_000_000:
            raise BackupError("Media limit exceeded")
    referenced = set()
    for row in indexed["tickets"].values():
        if row["user_id"] != user_id:
            raise BackupError("Ticket owner mismatch")
        TypeAdapter(Identifier).validate_python(row["id"])
        TypeAdapter(Identifier).validate_python(row["operation_id"])
        fields, sources = _json(row["fields"]), _json(row["field_sources"])
        if set(fields) != set(FIELD_NAMES) or TicketFields.model_validate(fields).model_dump() != fields:
            raise BackupError("Invalid movie fields")
        if set(sources) != set(FIELD_NAMES) or any(value not in {"user", "ocr", "migration", "empty"} for value in sources.values()):
            raise BackupError("Invalid field provenance")
        for key in ("version", "input_version"):
            _integer(row[key], 1)
        for key in ("created_at", "updated_at"):
            _integer(row[key])
        if row["updated_at"] < row["created_at"]:
            raise BackupError("Invalid record time")
        if row["deleted_at"] is not None:
            _integer(row["deleted_at"], row["created_at"])
        for key in ("original_asset_id", "poster_asset_id"):
            identifier = row[key]
            if key == "poster_asset_id" and identifier is None:
                continue
            if identifier not in assets:
                raise BackupError("Missing media reference")
            referenced.add(identifier)
    if referenced != set(assets):
        raise BackupError("Unexpected unreferenced media")
    for row in indexed["operations"].values():
        if row["user_id"] != user_id or not isinstance(row["fingerprint"], str) or not HASH.fullmatch(row["fingerprint"]):
            raise BackupError("Invalid operation")
        TypeAdapter(Identifier).validate_python(row["id"])
        _integer(row["created_at"])
    return indexed


def _live_assets(manifest):
    live = {row[key] for row in manifest["tickets"] if row["deleted_at"] is None
            for key in ("original_asset_id", "poster_asset_id") if row[key]}
    return [asset for asset in manifest["assets"] if asset["id"] in live]


def create_account_backup(store, user_id):
    manifest = snapshot_account(store, user_id)
    try:
        _validate(manifest, user_id)
    except (TypeError, KeyError, ValueError) as error:
        raise BackupError("Invalid account data") from error
    payload = encode(manifest).encode()
    live_assets = _live_assets(manifest)
    if len(payload) > MAX_MANIFEST_BYTES or len(live_assets) + 1 > MAX_MEMBERS or len(payload) + sum(a["size"] for a in live_assets) > MAX_TOTAL_BYTES:
        raise BackupError("Backup resource limit exceeded")
    archive = store.root / "exports" / (str(uuid.uuid4()) + ".zip")
    try:
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as target:
            target.writestr("manifest.json", payload)
            for asset in live_assets:
                digest, total = hashlib.sha256(), 0
                with closing(store.storage.open(asset["object_key"])) as source, target.open(asset["object_key"], "w") as output:
                    while chunk := source.read():
                        total += len(chunk)
                        if total > asset["size"]:
                            raise BackupError("Media length mismatch")
                        digest.update(chunk)
                        output.write(chunk)
                if total != asset["size"] or digest.hexdigest() != asset["id"]:
                    raise BackupError("Media checksum mismatch")
        return archive
    except BaseException:
        archive.unlink(missing_ok=True)
        raise


def _insert(db, name, records):
    columns = COLUMNS[name].split(",")
    for row in records:
        db.execute(f"INSERT INTO {name} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", tuple(row[key] for key in columns))


def restore_account_backup(archive: Path, destination: Path, *, user_id: str):
    if not destination.is_absolute() or os.path.lexists(destination) or not destination.parent.is_dir():
        raise BackupError("Destination must be a new absolute directory with an existing parent")
    if any(parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()) for parent in destination.parents):
        raise BackupError("Destination ancestors must not contain links")
    try:
        with tempfile.TemporaryDirectory(prefix=".account-restore-", dir=destination.parent) as temporary:
            root = Path(temporary) / "workspace"
            objects = root / "objects"
            with zipfile.ZipFile(archive) as source:
                entries = source.infolist()
                names = [entry.filename for entry in entries]
                if len(entries) > MAX_MEMBERS or len(set(names)) != len(names) or "manifest.json" not in names or sum(entry.file_size for entry in entries) > MAX_TOTAL_BYTES:
                    raise BackupError("Invalid archive members or resource limits")
                if any(stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1 for entry in entries):
                    raise BackupError("Linked or encrypted archive member")
                if source.getinfo("manifest.json").file_size > MAX_MANIFEST_BYTES:
                    raise BackupError("Manifest too large")
                manifest = _json(source.read("manifest.json"))
                indexed = _validate(manifest, user_id)
                live_assets = _live_assets(manifest)
                expected = {asset["object_key"] for asset in live_assets} | {"manifest.json"}
                if set(names) != expected:
                    raise BackupError("Unexpected or missing archive members")
                storage = LocalObjectStore(objects)
                for asset in live_assets:
                    if source.getinfo(asset["object_key"]).file_size != asset["size"]:
                        raise BackupError("Invalid media length")
                    raw = source.read(asset["object_key"])
                    info = image_info(raw)
                    if any(info[key] != asset[target] for key, target in (("assetId", "id"), ("size", "size"), ("mimeType", "mime"), ("width", "width"), ("height", "height"))):
                        raise BackupError("Media identity mismatch")
                    storage.put_bytes(asset["object_key"], raw, asset["id"], asset["mime"])
            store = Store(root, storage=storage)
            with store.transaction(True) as db:
                _insert(db, "users", [manifest["user"]])
                _insert(db, "assets", manifest["assets"])
                db.executemany("INSERT INTO asset_grants VALUES (?,?)", ((user_id, identifier) for identifier in indexed["assets"]))
                _insert(db, "tickets", manifest["tickets"])
                _insert(db, "operations", manifest["operations"])
                if db.execute("PRAGMA foreign_key_check").fetchone() or db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError("Restored database integrity failure")
                account_state(store, db, user_id)
            with closing(store.connect()) as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("PRAGMA journal_mode=DELETE")
            destination.mkdir()
            marker = destination / "RESTORE_INCOMPLETE"
            marker.write_text("Restoration did not finish. Do not run this directory.\n", encoding="utf-8")
            for child in root.iterdir():
                os.rename(child, destination / child.name)
            marker.unlink()
        return {"verified": True, "userId": user_id, "destination": str(destination),
                "tickets": len(manifest["tickets"]), "files": len(live_assets)}
    except (KeyError, TypeError, UnicodeError, ValueError, zipfile.BadZipFile, StorageError, HTTPException, ValidationError) as error:
        raise BackupError("Malformed account backup") from error

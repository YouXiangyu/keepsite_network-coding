"""Account-only backup export and verified restoration into a new directory."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import zipfile
from contextlib import closing
from datetime import date as Date

from fastapi import HTTPException
from pydantic import ValidationError

from .contracts import CollectionFields, Profile
from .media import MAX_IMAGE_BYTES, image_info
from .object_storage import LocalObjectStore, StorageError, object_key
from .store import Store, encode, now_ms
from .state import account_state
from .template import safe_file, validate_model_outputs

MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_FILES = 50000
HASH = re.compile(r"[a-f0-9]{64}\Z")


class BackupError(ValueError):
    pass


def snapshot_account(store: Store, user_id: str):
    """Capture one consistent set of references before reading object bytes."""
    with store.transaction() as db:
        state = account_state(store, db, user_id)
        data = state["data"]
        items = [*data["collections"], *[share["item"] for share in data["shares"]], *data["community"]]
        assets = {item["assetId"]: dict(store.asset(db, user_id, item["assetId"])) for item in items if item.get("assetId")}
        models = {item["modelId"]: dict(store.model(db, user_id, item["modelId"])) for item in items if item.get("modelId") and item.get("generation") == "ready"}
        collections = [dict(row) for row in db.execute("SELECT * FROM collections WHERE user_id=? ORDER BY rowid", (user_id,))]
        jobs = [dict(store.job(db, user_id, row["job_id"])) for row in collections if row["job_id"]]
    return state, assets, models, collections, jobs


def create_account_backup(store: Store, user_id: str) -> Path:
    snapshot, assets, models, collection_rows, job_rows = snapshot_account(store, user_id)
    descriptor, name = tempfile.mkstemp(prefix="account-backup-", suffix=".zip", dir=store.root / "exports")
    os.close(descriptor)
    archive = Path(name)
    try:
        files = {}
        for asset in assets.values():
            key = object_key(asset["object_key"])
            files[key] = {"path": key, "sha256": asset["id"], "size": asset["size"]}
        for model in models.values():
            for file in json.loads(model["files"]):
                key = object_key(file["path"])
                record = {"path": key, "size": file["size"], "sha256": file["sha256"]}
                if key in files and files[key] != record:
                    raise BackupError("Conflicting object metadata in account snapshot")
                files[key] = record
        if len(files) + 1 > MAX_FILES or sum(record["size"] for record in files.values()) > MAX_EXPANDED_BYTES:
            raise BackupError("Account backup exceeds the supported restore limits")
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            for key, record in sorted(files.items()):
                digest, size = hashlib.sha256(), 0
                with closing(store.storage.open(key)) as incoming, bundle.open(key, "w") as outgoing:
                    if incoming.size != record["size"]:
                        raise StorageError("Backup object length disagrees with its metadata")
                    for chunk in incoming.chunks():
                        if size + len(chunk) > record["size"]:
                            raise StorageError("Backup object exceeds its declared length")
                        digest.update(chunk)
                        size += len(chunk)
                        outgoing.write(chunk)
                if size != record["size"] or digest.hexdigest() != record["sha256"]:
                    raise StorageError("Backup object integrity check failed")
            data = snapshot["data"]
            for item in [*data["collections"], *[share["item"] for share in data["shares"]], *data["community"]]:
                if item.get("assetId"):
                    item["customImage"] = assets[item["assetId"]]["object_key"]
                if item.get("modelId") and item.get("generation") == "ready":
                    item["modelUrl"] = models[item["modelId"]]["model_key"]
            manifest = encode({
                "format": "memoverse-account-backup", "version": 2, "createdAt": now_ms(), **snapshot,
                "files": list(files.values()), "collections": collection_rows, "jobs": job_rows,
                "assets": list(assets.values()), "models": list(models.values()),
            }).encode("utf-8")
            if len(manifest) > MAX_MANIFEST_BYTES or len(manifest) + sum(record["size"] for record in files.values()) > MAX_EXPANDED_BYTES:
                raise BackupError("Account backup manifest exceeds the supported restore limits")
            bundle.writestr("manifest.json", manifest)
    except BaseException:
        archive.unlink(missing_ok=True)
        raise
    return archive


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BackupError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise BackupError("Non-finite JSON number")


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def _index(rows, key="id"):
    if not isinstance(rows, list):
        raise BackupError("Expected a list of records")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(key), str) or not row[key] or row[key] in result:
            raise BackupError("Invalid or duplicate record identity")
        result[row[key]] = row
    return result


def _positive(value, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        raise BackupError("Invalid integer metadata")
    return value


def _content_identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value):
        raise BackupError("Invalid content identifier")


def _collection_payload(item):
    if not isinstance(item, dict):
        raise BackupError("Invalid collection payload")
    _content_identifier(item.get("id"))
    fields = ("title", "category", "tags", "note", "date", "art")
    if not all(key in item for key in fields) or not isinstance(item["tags"], list):
        raise BackupError("Incomplete collection fields")
    CollectionFields.model_validate({key: item[key] for key in fields}, strict=True)
    _positive(item.get("addedAt"), zero=True)
    if item.get("source") not in {"mine", "friend"} or ("sender" in item and not isinstance(item["sender"], str)):
        raise BackupError("Invalid collection source")


def _social_payloads(data):
    for share in data["shares"]:
        _content_identifier(share["id"])
        _collection_payload(share["item"])
        _positive(share["item"].get("version"))
        if not isinstance(share.get("sender"), str) or type(share.get("unread")) is not bool:
            raise BackupError("Invalid share fields")
        if not isinstance(share.get("receivedAt"), str) or Date.fromisoformat(share["receivedAt"]).isoformat() != share["receivedAt"]:
            raise BackupError("Invalid share date")
    for item in data["community"]:
        _content_identifier(item["id"])
        if not all(isinstance(item.get(key), str) for key in ("author", "description")):
            raise BackupError("Invalid community fields")
        CollectionFields.model_validate({"title": item.get("title"), "category": item.get("category"),
                                         "tags": [], "note": item["description"][:500], "date": "1970-01-01", "art": item.get("art")}, strict=True)
        for key in ("exposures", "saves", "likes"):
            _positive(item.get(key), zero=True)


def _extract_verified(archive: Path, folder: Path):
    with zipfile.ZipFile(archive) as bundle:
        members = bundle.infolist()
        names = [member.filename for member in members]
        if len(names) != len(set(names)) or len(names) > MAX_FILES or "manifest.json" not in names:
            raise BackupError("Duplicate, excessive or missing archive members")
        if sum(member.file_size for member in members) > MAX_EXPANDED_BYTES:
            raise BackupError("Archive exceeds the expanded size limit")
        for member in members:
            if member.is_dir() or stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1:
                raise BackupError("Archive must contain ordinary unencrypted files")
        info = bundle.getinfo("manifest.json")
        if info.file_size > MAX_MANIFEST_BYTES:
            raise BackupError("Manifest is too large")
        manifest = _json(bundle.read(info))
        if not isinstance(manifest, dict) or manifest.get("format") != "memoverse-account-backup" or manifest.get("version") != 2:
            raise BackupError("Unsupported account backup format")
        files = _index(manifest["files"], "path")
        if set(names) != {"manifest.json", *files}:
            raise BackupError("Archive contains missing or undeclared files")
        for key, record in files.items():
            if object_key(key) != key or not key.startswith(("media/", "models/")):
                raise BackupError("Invalid object path")
            if not HASH.fullmatch(str(record.get("sha256", ""))) or bundle.getinfo(key).file_size != _positive(record["size"]):
                raise BackupError("Invalid file hash or size")
            target = folder / key
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            with bundle.open(key) as incoming, target.open("xb") as outgoing:
                while chunk := incoming.read(1024 * 1024):
                    size += len(chunk)
                    if size > record["size"]:
                        raise BackupError("Expanded file exceeds its declared size")
                    digest.update(chunk)
                    outgoing.write(chunk)
            if size != record["size"] or digest.hexdigest() != record["sha256"]:
                raise BackupError("Archive file integrity check failed")
    return manifest, files


def _validate_manifest(manifest, files, objects: Path, user_id: str):
    if not isinstance(user_id, str) or not user_id or len(user_id) > 512 or manifest.get("userId") != user_id:
        raise BackupError("Backup owner must match the explicitly selected account")
    _positive(manifest["revision"], zero=True)
    data = manifest["data"]
    if not isinstance(data, dict) or set(data) != {"collections", "shares", "community", "profile", "likedCommunityIds"}:
        raise BackupError("Incomplete account data")
    profile = data["profile"]
    if not isinstance(profile, dict) or not all(key in profile for key in ("name", "bio")):
        raise BackupError("Invalid profile")
    Profile.model_validate(profile, strict=True)
    assets, models = _index(manifest["assets"]), _index(manifest["models"])
    collections, jobs = _index(manifest["collections"]), _index(manifest["jobs"])
    rendered, shares, community = _index(data["collections"]), _index(data["shares"]), _index(data["community"])
    if set(rendered) != set(collections):
        raise BackupError("Collection rows disagree with the account snapshot")
    _social_payloads(data)
    required_files = {}
    for asset in assets.values():
        identifier = asset["id"]
        if not HASH.fullmatch(identifier) or asset["object_key"] not in {f"media/{identifier}.{extension}" for extension in ("png", "jpg", "webp")}:
            raise BackupError("Invalid content-addressed image")
        record = {"path": asset["object_key"], "sha256": identifier, "size": _positive(asset["size"])}
        if files.get(record["path"]) != record:
            raise BackupError("Image metadata disagrees with its file")
        if record["size"] > MAX_IMAGE_BYTES:
            raise BackupError("Image exceeds the upload size limit")
        info = image_info(safe_file(objects, record["path"]).read_bytes())
        if info["mimeType"] != asset["mime"] or (info["width"], info["height"]) != (_positive(asset["width"]), _positive(asset["height"])):
            raise BackupError("Image dimensions/type mismatch")
        required_files[record["path"]] = record
    for model in models.values():
        identifier = model["id"]
        if not HASH.fullmatch(identifier) or model["asset_id"] not in assets:
            raise BackupError("Invalid model identity or original")
        expected = {f"models/{identifier}/{name}" for name in ("model.glb", "depth.npy", "geometry.json")}
        records = _index(_json(model["files"]), "path")
        if set(records) != expected or model["model_key"] != f"models/{identifier}/model.glb":
            raise BackupError("Model must include exactly GLB, depth and geometry")
        for key, record in records.items():
            if files.get(key) != record:
                raise BackupError("Model metadata disagrees with its files")
            required_files[key] = record
        geometry = validate_model_outputs(*(safe_file(objects, f"models/{identifier}/{name}") for name in ("model.glb", "depth.npy", "geometry.json")))
        if _json(model["camera"]) != geometry["camera"] or not isinstance(_json(model["metadata"]), dict):
            raise BackupError("Model camera/metadata mismatch")
    if files != required_files:
        raise BackupError("Archive has unreferenced files")
    for row in [*collections.values(), *jobs.values()]:
        if row["user_id"] != user_id or row["asset_id"] not in assets:
            raise BackupError("Record owner/original mismatch")
        _positive(row["input_revision"])
        if row["model_id"] and (row["model_id"] not in models or models[row["model_id"]]["asset_id"] != row["asset_id"]):
            raise BackupError("Record model belongs to another image")
    linked_jobs = set()
    for identifier, row in collections.items():
        payload = _json(row["payload"])
        if not isinstance(payload, dict) or payload.get("id") != identifier:
            raise BackupError("Invalid collection payload")
        _collection_payload(payload)
        _collection_payload(rendered[identifier])
        if any(rendered[identifier].get(key) != value for key, value in payload.items()):
            raise BackupError("Collection payload disagrees with its rendered snapshot")
        _positive(row["version"])
        if row["job_id"]:
            linked_jobs.add(row["job_id"])
            job = jobs.get(row["job_id"])
            if not job or (job["collection_id"], job["asset_id"], job["input_revision"]) != (identifier, row["asset_id"], row["input_revision"]):
                raise BackupError("Current job does not match collection input")
            if job["status"] == "ready" and (not job["model_id"] or job["model_id"] != row["model_id"]):
                raise BackupError("Ready job is missing its published model")
        elif not row["model_id"]:
            raise BackupError("Collection has neither a model nor a task")
        expected_status = jobs[row["job_id"]]["status"] if row["job_id"] else "ready"
        if (rendered[identifier].get("assetId") != row["asset_id"] or rendered[identifier].get("version") != row["version"]
                or rendered[identifier].get("modelId") != row["model_id"]
                or rendered[identifier].get("generation") != expected_status):
            raise BackupError("Rendered collection disagrees with its row")
    if linked_jobs != set(jobs):
        raise BackupError("Archive contains unreferenced jobs")
    for job in jobs.values():
        _content_identifier(job["id"])
        if job["status"] not in {"queued", "processing", "blocked", "failed", "ready"} or not isinstance(job.get("error"), str):
            raise BackupError("Unknown task status")
        _positive(job["created_at"], zero=True)
        _positive(job["updated_at"], zero=True)
    social_items = [*[share["item"] for share in shares.values()], *community.values()]
    if any(item.get("generation") != "ready" for item in social_items):
        raise BackupError("Shared/template content must retain its published model")
    items = [*rendered.values(), *social_items]
    for item in items:
        if item.get("assetId") not in assets:
            raise BackupError("Content references a missing original")
        if item.get("modelId") and (item["modelId"] not in models or models[item["modelId"]]["asset_id"] != item["assetId"]):
            raise BackupError("Content references an inconsistent model")
        if item.get("customImage") != assets[item["assetId"]]["object_key"]:
            raise BackupError("Content image path disagrees with its original")
        if item.get("generation") == "ready":
            model = models.get(item.get("modelId"))
            if not model or item.get("modelUrl") != model["model_key"] or item.get("modelCamera") != _json(model["camera"]):
                raise BackupError("Ready content is missing its model or camera")
        elif item.get("modelUrl"):
            raise BackupError("Unfinished content must not publish a model URL")
    liked = data["likedCommunityIds"]
    if not isinstance(liked, list) or len(liked) != len(set(liked)) or not all(identifier in community for identifier in liked):
        raise BackupError("Invalid community likes")
    return assets, models, collections, jobs


def _insert_records(db, table, columns, records):
    # Tables/columns are internal constants, never archive-supplied SQL.
    names = columns.split(",")
    query = f"INSERT INTO {table} ({columns}) VALUES ({','.join('?' for _ in names)})"
    for record in records:
        db.execute(query, tuple(record[name] for name in names))


def restore_account_backup(archive: Path, destination: Path, *, user_id: str) -> dict:
    """Restore an explicit owner into a new local store; never merge or overwrite."""
    if not destination.is_absolute() or os.path.lexists(destination) or not destination.parent.is_dir():
        raise BackupError("Destination must be a new absolute directory with an existing parent")
    if any(parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()) for parent in destination.parents):
        raise BackupError("Destination ancestors must not contain links")
    try:
        with tempfile.TemporaryDirectory(prefix=".account-restore-", dir=destination.parent) as temporary:
            stage = Path(temporary)
            root = stage / "workspace"
            objects = root / "objects"
            manifest, files = _extract_verified(archive, objects)
            assets, models, collections, jobs = _validate_manifest(manifest, files, objects, user_id)
            store = Store(root, storage=LocalObjectStore(objects), template=None)
            recovered = 0
            with store.transaction(True) as db:
                db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)", (user_id, "", manifest["data"]["profile"]["name"], manifest["revision"], "account-backup-v2", now_ms()))
                _insert_records(db, "assets", "id,object_key,mime,size,width,height,created_at", assets.values())
                _insert_records(db, "models", "id,asset_id,model_key,files,camera,metadata,created_at", models.values())
                db.executemany("INSERT INTO asset_grants VALUES (?,?)", ((user_id, key) for key in assets))
                db.executemany("INSERT INTO model_grants VALUES (?,?)", ((user_id, key) for key in models))
                _insert_records(db, "collections", "user_id,id,payload,asset_id,model_id,version,input_revision,job_id,create_fingerprint", collections.values())
                for original in jobs.values():
                    job = copy.deepcopy(original)
                    if job["status"] in {"processing", "blocked"}:
                        job.update(status="queued", error="", result=None, model_id=None, updated_at=now_ms())
                        recovered += 1
                    _insert_records(db, "jobs", "id,user_id,collection_id,asset_id,input_revision,status,error,model_id,created_at,updated_at,result", [job])
                for table in ("shares", "community"):
                    db.executemany(f"INSERT INTO {table} VALUES (?,?,?)", ((user_id, row["id"], encode(row)) for row in manifest["data"][table]))
                db.executemany("INSERT INTO likes VALUES (?,?)", ((user_id, key) for key in manifest["data"]["likedCommunityIds"]))
                db.execute("INSERT INTO profiles VALUES (?,?)", (user_id, encode(manifest["data"]["profile"])))
                if recovered:
                    store.bump(db, user_id)
                if db.execute("PRAGMA foreign_key_check").fetchone():
                    raise BackupError("Restored references violate account ownership")
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError("Restored database failed integrity check")
                account_state(store, db, user_id)
            with closing(store.connect()) as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                db.execute("PRAGMA journal_mode=DELETE")
            # Reserve the destination exclusively, including against another
            # restore process racing after the initial existence check. A crash
            # during this short publication leaves an explicit incomplete marker.
            destination.mkdir()
            marker = destination / "RESTORE_INCOMPLETE"
            marker.write_text("Restoration did not finish. Do not run this directory as an application.\n", encoding="utf-8")
            for child in root.iterdir():
                os.rename(child, destination / child.name)
            marker.unlink()
        return {"verified": True, "userId": user_id, "destination": str(destination),
                "collections": len(collections), "jobs": len(jobs), "files": len(files), "requeuedJobs": recovered}
    except (KeyError, TypeError, UnicodeError, json.JSONDecodeError, zipfile.BadZipFile, StorageError, HTTPException, ValidationError) as error:
        raise BackupError("Malformed account backup") from error

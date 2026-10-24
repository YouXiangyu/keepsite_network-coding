#!/usr/bin/env python3
"""Build/verify a one-time SQLite + media migration archive; no network access.

The SQLite business rows are preserved exactly via sqlite3.backup. Source WAL
is read by SQLite, never copied as a file. Only database-referenced originals
and the three outputs of every ready job are included. geometry.json copies
have recursive *_path fields removed; their manifest hashes describe the
transformed bytes. Stop writes before the final cutover to avoid later edits
being left behind. Verification is intended before starting the destination API.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DATABASE = "keepsite.sqlite3"
MANIFEST = "migration-manifest.json"
FORMAT = "keepsite-server-data"
JOB_FILES = ("model.glb", "depth.npy", "geometry.json")
TABLES = {"meta", "assets", "collections", "shares", "community", "likes", "migrations", "jobs"}


class PackageError(Exception):
    pass


def relative_name(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or any(ord(c) < 32 for c in value):
        raise PackageError("Unsafe package path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise PackageError(f"Unsafe package path: {value!r}")
    return path.as_posix()


def no_link(path: Path):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
        raise PackageError(f"Links/reparse points are not accepted: {path}")
    return info


def safe_file(root: Path, name: str) -> Path:
    name = relative_name(name)
    candidate = root
    for part in PurePosixPath(name).parts:
        candidate = candidate / part
        try:
            no_link(candidate)
        except FileNotFoundError as error:
            raise PackageError(f"Referenced file is missing: {name}") from error
    if not candidate.resolve().is_relative_to(root.resolve()) or not stat.S_ISREG(candidate.stat().st_mode):
        raise PackageError(f"Not a regular file within data directory: {name}")
    return candidate


def read_only_database(path: Path, *, immutable=False):
    uri = path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def inspect_database(path: Path) -> tuple[dict, dict]:
    """Inspect the standalone copy; never update its business rows."""
    with closing(read_only_database(path, immutable=True)) as db:
        integrity = [row[0] for row in db.execute("PRAGMA integrity_check")]
        if integrity != ["ok"]:
            raise PackageError(f"SQLite integrity_check failed: {integrity[:3]}")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise PackageError("SQLite foreign_key_check failed")
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        if not TABLES.issubset(tables):
            raise PackageError("Unsupported/incomplete SQLite schema")
        counts = {name: db.execute('SELECT COUNT(*) FROM "' + name.replace('"', '""') + '"').fetchone()[0] for name in sorted(tables)}
        assets = {row["id"]: dict(row) for row in db.execute("SELECT * FROM assets")}
        jobs = {row["id"]: dict(row) for row in db.execute("SELECT * FROM jobs")}
        files = {DATABASE: {"kind": "database"}}
        for asset in assets.values():
            filename = asset["filename"]
            if relative_name(filename) != filename or "/" in filename or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", filename):
                raise PackageError("Invalid database asset filename")
            if not re.fullmatch(r"[a-f0-9]{64}", asset["id"]):
                raise PackageError("Asset ID must be a SHA-256 content hash")
            entry = {"kind": "media", "expectedSize": asset["size"], "expectedSha256": asset["id"]}
            name = f"media/{filename}"
            if name in files and files[name] != entry:
                raise PackageError("Conflicting asset records reference the same file")
            files[name] = entry
        for job in jobs.values():
            if job["asset_id"] not in assets:
                raise PackageError(f"Job {job['id']} references a missing asset")
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,120}", job["id"]):
                raise PackageError("Invalid job ID")
            if job["status"] == "ready":
                for filename in JOB_FILES:
                    files[f"models/{job['id']}/{filename}"] = {"kind": "geometry" if filename == "geometry.json" else "model"}
        for item in db.execute("SELECT * FROM collections"):
            if item["asset_id"] and item["asset_id"] not in assets:
                raise PackageError(f"Collection {item['id']} references a missing asset")
            if item["job_id"]:
                job = jobs.get(item["job_id"])
                if not job or job["collection_id"] != item["id"] or job["asset_id"] != item["asset_id"] or job["input_revision"] != item["input_revision"]:
                    raise PackageError(f"Collection {item['id']} has an inconsistent current job")
        for share in db.execute("SELECT id,payload FROM shares"):
            try:
                item = json.loads(share["payload"])["item"]
            except (ValueError, KeyError, TypeError) as error:
                raise PackageError(f"Share {share['id']} has invalid JSON") from error
            if item.get("assetId") and item["assetId"] not in assets:
                raise PackageError(f"Share {share['id']} references a missing asset")
            if item.get("generationJobId") and item["generationJobId"] not in jobs:
                raise PackageError(f"Share {share['id']} references a missing job")
        counts["readyJobs"] = sum(job["status"] == "ready" for job in jobs.values())
    return counts, files


def hash_file(path: Path) -> tuple[int, str]:
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(source.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
        raise PackageError(f"File changed during reading: {path.name}")
    return size, digest.hexdigest()


def portable(value):
    if isinstance(value, dict):
        return {key: portable(item) for key, item in value.items() if not key.lower().endswith("_path")}
    if isinstance(value, list):
        return [portable(item) for item in value]
    return value


def copy_referenced(source: Path, target: Path, *, geometry=False) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True)
    digest, size = hashlib.sha256(), 0
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        before = os.fstat(incoming.fileno())
        while chunk := incoming.read(1024 * 1024):
            outgoing.write(chunk)
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(incoming.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or size != before.st_size:
        raise PackageError(f"Source file changed while copying: {source.name}")
    result = {"size": size, "sha256": digest.hexdigest()}
    if geometry:
        try:
            original = json.loads(target.read_text(encoding="utf-8"))
            if not isinstance(original, dict):
                raise ValueError("geometry must be an object")
            converted = portable(original)
            if converted != original:
                target.write_text(json.dumps(converted, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
                converted_size, converted_hash = hash_file(target)
                result = {"size": converted_size, "sha256": converted_hash, "sourceSha256": digest.hexdigest(), "transform": "remove-*_path-fields"}
        except (UnicodeError, ValueError) as error:
            raise PackageError("Invalid geometry.json") from error
    return result


def verify_extracted(directory: Path) -> dict:
    directory = directory.absolute()
    no_link(directory)
    if not directory.is_dir():
        raise PackageError("Extraction destination must be a directory")
    manifest_path = safe_file(directory, MANIFEST)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as error:
        raise PackageError("Invalid migration manifest") from error
    if manifest.get("format") != FORMAT or manifest.get("version") != 1 or manifest.get("database") != DATABASE:
        raise PackageError("Unsupported migration manifest")
    records = manifest.get("files")
    if not isinstance(records, list):
        raise PackageError("Manifest files must be a list")
    declared = {}
    for record in records:
        if not isinstance(record, dict):
            raise PackageError("Invalid manifest file record")
        name = relative_name(record.get("path"))
        if name == MANIFEST or name in declared:
            raise PackageError("Duplicate/recursive manifest entry")
        if not isinstance(record.get("size"), int) or isinstance(record["size"], bool) or record["size"] < 0 or not re.fullmatch(r"[a-f0-9]{64}", str(record.get("sha256", ""))):
            raise PackageError("Invalid file size/hash in manifest")
        path = safe_file(directory, name)
        actual_size, actual_hash = hash_file(path)
        if (actual_size, actual_hash) != (record["size"], record["sha256"]):
            raise PackageError(f"File hash/size mismatch: {name}")
        declared[name] = record
    actual_names = set()
    for folder, directories, filenames in os.walk(directory, followlinks=False):
        for name in directories:
            no_link(Path(folder) / name)
        for name in filenames:
            path = Path(folder) / name
            no_link(path)
            actual_names.add(path.relative_to(directory).as_posix())
    if actual_names != set(declared) | {MANIFEST}:
        raise PackageError("Extracted directory contains missing or undeclared files (including WAL/SHM)")
    counts, required = inspect_database(safe_file(directory, DATABASE))
    if set(required) != set(declared):
        raise PackageError("Manifest file set does not match SQLite references")
    for name, expected in required.items():
        record = declared[name]
        if record.get("kind") != expected["kind"]:
            raise PackageError(f"Manifest file kind mismatch: {name}")
        if expected["kind"] == "media" and (record["size"] != expected["expectedSize"] or record["sha256"] != expected["expectedSha256"]):
            raise PackageError(f"Asset hash/size does not match SQLite: {name}")
        if expected["kind"] == "geometry":
            try:
                geometry = json.loads(safe_file(directory, name).read_text(encoding="utf-8"))
            except (UnicodeError, ValueError) as error:
                raise PackageError(f"Invalid geometry: {name}") from error
            if not isinstance(geometry, dict) or portable(geometry) != geometry:
                raise PackageError(f"Geometry contains non-portable *_path fields: {name}")
    counts.update(files=len(declared), totalBytes=sum(record["size"] for record in records))
    if counts != manifest.get("counts"):
        raise PackageError("Manifest counts do not match the SQLite/file snapshot")
    return {"verified": True, "counts": counts}


def package_data(data_dir: Path, output: Path) -> dict:
    if not output.is_absolute():
        raise PackageError("--output must be an explicit absolute file path")
    if not output.name.endswith(".tar.gz"):
        raise PackageError("--output must end in .tar.gz")
    if os.path.lexists(output):
        raise PackageError("Output already exists; refusing to overwrite")
    if not output.parent.is_dir():
        raise PackageError("Output parent directory must already exist")
    data_dir = data_dir.absolute()
    no_link(data_dir)
    if output.resolve().is_relative_to(data_dir.resolve()):
        raise PackageError("Output cannot be inside the original data directory")
    source_database = safe_file(data_dir, DATABASE)
    with tempfile.TemporaryDirectory(prefix=".keepsite-package-", dir=output.parent) as temporary:
        staging = Path(temporary)
        snapshot = staging / DATABASE
        with closing(read_only_database(source_database)) as source, closing(sqlite3.connect(snapshot)) as destination:
            source.backup(destination, pages=256, sleep=0.05)
            # Convert only the copy's journal mode, never edit any business rows.
            destination.execute("PRAGMA journal_mode=DELETE")
        counts, required = inspect_database(snapshot)
        files = []
        for name, expected in sorted(required.items()):
            if name == DATABASE:
                size, sha = hash_file(snapshot)
                details = {"size": size, "sha256": sha}
            else:
                original = safe_file(data_dir, name)
                details = copy_referenced(original, staging / name, geometry=expected["kind"] == "geometry")
            if expected["kind"] == "media" and (details["size"] != expected["expectedSize"] or details["sha256"] != expected["expectedSha256"]):
                raise PackageError(f"Original asset differs from its database record: {name}")
            files.append({"path": name, "kind": expected["kind"], **details})
        counts.update(files=len(files), totalBytes=sum(record["size"] for record in files))
        manifest = {"format": FORMAT, "version": 1, "database": DATABASE,
                    "createdAt": datetime.now(timezone.utc).isoformat(), "counts": counts, "files": files}
        (staging / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        verify_extracted(staging)
        created = False
        try:
            descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(descriptor, "wb") as stream:
                with tarfile.open(fileobj=stream, mode="w:gz", format=tarfile.PAX_FORMAT) as archive:
                    for name in [MANIFEST, *[record["path"] for record in files]]:
                        source = safe_file(staging, name)
                        info = archive.gettarinfo(str(source), arcname=name)
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        info.mode = 0o600
                        with source.open("rb") as payload:
                            archive.addfile(info, payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            if created:
                output.unlink(missing_ok=True)
            raise
    size, digest = hash_file(output)
    return {"output": str(output), "sha256": digest, "size": size, "counts": counts}


def self_test() -> bool:
    """Small synthetic tests; never open backend/data or contact a service."""
    class PackageTests(unittest.TestCase):
        def setUp(self):
            cache = ROOT / ".cache"
            cache.mkdir(exist_ok=True)
            self.temporary = tempfile.TemporaryDirectory(prefix="migration-script-test-", dir=cache)
            self.root = Path(self.temporary.name)
            self.data = self.root / "data"
            (self.data / "media").mkdir(parents=True)
            (self.data / "models" / "job-1").mkdir(parents=True)
            self.db = sqlite3.connect(self.data / DATABASE)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript('''
                CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE assets(id TEXT PRIMARY KEY,filename TEXT,size INTEGER);
                CREATE TABLE collections(id TEXT PRIMARY KEY,payload TEXT,asset_id TEXT,input_revision INTEGER,job_id TEXT);
                CREATE TABLE jobs(id TEXT PRIMARY KEY,collection_id TEXT,asset_id TEXT,input_revision INTEGER,status TEXT,result TEXT);
                CREATE TABLE shares(id TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE community(id TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE likes(id TEXT PRIMARY KEY);
                CREATE TABLE migrations(id TEXT PRIMARY KEY);
            ''')
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            raw = b"synthetic-original-image"
            asset_id = hashlib.sha256(raw).hexdigest()
            (self.data / "media" / f"{asset_id}.png").write_bytes(raw)
            (self.data / "media" / "unreferenced.png").write_bytes(b"do not package")
            self.db.execute("INSERT INTO assets VALUES (?,?,?)", (asset_id, f"{asset_id}.png", len(raw)))
            self.db.execute("INSERT INTO collections VALUES (?,?,?,?,?)", ("c1", '{}', asset_id, 3, "job-1"))
            self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?)", ("job-1", "c1", asset_id, 3, "ready", '{"camera":{"fov":30}}'))
            self.db.execute("INSERT INTO meta VALUES ('revision','7')")
            self.db.commit()  # Leave committed rows in the live source WAL.
            folder = self.data / "models" / "job-1"
            (folder / "model.glb").write_bytes(b"synthetic-model")
            (folder / "depth.npy").write_bytes(b"synthetic-depth")
            (folder / "geometry.json").write_text('{"camera":{"fov":30},"local_path":"C:/old/model","nested":{"depth_path":"D:/old/depth"}}', encoding="utf-8")
            self.geometry_before = (folder / "geometry.json").read_bytes()
            self.output = self.root / "migration.tar.gz"

        def tearDown(self):
            self.db.close()
            self.temporary.cleanup()

        def unpack(self):
            package_data(self.data, self.output)
            destination = self.root / "extracted"
            destination.mkdir()
            with tarfile.open(self.output, "r:gz") as archive:
                archive.extractall(destination, filter="data")
            return destination

        def test_consistent_sqlite_references_and_portable_geometry(self):
            extracted = self.unpack()
            summary = verify_extracted(extracted)
            self.assertEqual(summary["counts"]["collections"], 1)
            self.assertEqual(summary["counts"]["assets"], 1)
            self.assertEqual(summary["counts"]["readyJobs"], 1)
            self.assertEqual(summary["counts"]["files"], 5)
            self.assertFalse((extracted / "media" / "unreferenced.png").exists())
            self.assertFalse((extracted / (DATABASE + "-wal")).exists())
            self.assertEqual((self.data / "models/job-1/geometry.json").read_bytes(), self.geometry_before)
            with closing(read_only_database(extracted / DATABASE, immutable=True)) as copied:
                self.assertEqual(copied.execute("SELECT input_revision FROM collections").fetchone()[0], 3)
                self.assertEqual(copied.execute("SELECT value FROM meta WHERE key='revision'").fetchone()[0], "7")

        def test_tampering_and_overwrite_are_rejected(self):
            extracted = self.unpack()
            with self.assertRaises(PackageError):
                package_data(self.data, self.output)
            (extracted / "models/job-1/depth.npy").write_bytes(b"changed")
            with self.assertRaises(PackageError):
                verify_extracted(extracted)

        def test_path_traversal_and_missing_outputs_are_rejected(self):
            for name in ("../outside", "/absolute", "C:/outside", "models/../outside", "models\\outside"):
                with self.assertRaises(PackageError):
                    relative_name(name)
            (self.data / "models/job-1/model.glb").unlink()
            with self.assertRaises(PackageError):
                package_data(self.data, self.output)
            self.assertFalse(self.output.exists())

    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PackageTests))
    return result.wasSuccessful()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "backend/data")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path, help="Explicit absolute .tar.gz path; existing output is never overwritten")
    mode.add_argument("--verify-extracted", type=Path, metavar="DIRECTORY", help="Verify an extracted migration directory before running the server")
    mode.add_argument("--self-test", action="store_true", help="Run small synthetic tests in a temporary D-drive project cache directory")
    args = parser.parse_args()
    try:
        if args.self_test:
            return 0 if self_test() else 1
        result = verify_extracted(args.verify_extracted) if args.verify_extracted else package_data(args.data_dir, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (PackageError, OSError, sqlite3.Error, tarfile.TarError) as error:
        parser.exit(1, f"Migration package error: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())

"""Private immutable object storage; ownership is enforced by the SQL caller."""
from __future__ import annotations

from contextlib import closing
import base64
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import BinaryIO


CHUNK_SIZE = 512 * 1024


class StorageError(Exception):
    pass


class ObjectMissing(StorageError):
    pass


def object_key(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise StorageError("Invalid object key")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        raise StorageError("Invalid object key")
    if path.parts[0] not in ("media", "models"):
        raise StorageError("Unapproved object prefix")
    return path.as_posix()


def sha_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class ObjectReader:
    def __init__(self, body: BinaryIO, size: int):
        self.body = body
        self.size = size

    def read(self, size=CHUNK_SIZE):
        return self.body.read(size)

    def close(self):
        self.body.close()

    def chunks(self):
        try:
            while chunk := self.body.read(CHUNK_SIZE):
                yield chunk
        finally:
            self.close()


class ObjectStore:
    def put_file(self, key: str, path: Path, sha256: str, content_type: str):
        with path.open("rb") as stream:
            self._put(key, stream, path.stat().st_size, sha256, content_type)

    def put_bytes(self, key: str, value: bytes, sha256: str, content_type: str):
        self._put(key, io.BytesIO(value), len(value), sha256, content_type)

    def download(self, key: str, destination: Path, sha256: str):
        digest = hashlib.sha256()
        total = 0
        with closing(self.open(key)) as incoming, destination.open("xb") as outgoing:
            while chunk := incoming.read():
                outgoing.write(chunk)
                digest.update(chunk)
                total += len(chunk)
            if total != incoming.size or digest.hexdigest() != sha256:
                raise StorageError("Object integrity check failed")

    def open(self, key: str) -> ObjectReader:
        raise NotImplementedError

    def _put(self, key: str, stream: BinaryIO, size: int, sha256: str, content_type: str):
        raise NotImplementedError


class LocalObjectStore(ObjectStore):
    """Explicit development/test storage, never an automatic S3 fallback."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        path = self.root.joinpath(*PurePosixPath(object_key(key)).parts)
        current = path
        while current != self.root:
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise StorageError("Object path contains a link")
            current = current.parent
        if not path.resolve().is_relative_to(self.root):
            raise StorageError("Object path escapes storage root")
        return path

    def _put(self, key, stream, size, sha256, content_type):
        path = self._path(key)
        if path.exists():
            if not path.is_file() or path.stat().st_size != size or sha_file(path) != sha256:
                raise StorageError("Immutable object identity conflict")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".upload-", dir=path.parent)
        try:
            digest, copied = hashlib.sha256(), 0
            with os.fdopen(descriptor, "wb") as outgoing:
                while chunk := stream.read(CHUNK_SIZE):
                    outgoing.write(chunk)
                    copied += len(chunk)
                    digest.update(chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if copied != size or digest.hexdigest() != sha256:
                raise StorageError("Object upload checksum mismatch")
            # Keys are content/recipe-addressed; competing writers must have the
            # same validated bytes. Never replace a conflicting existing object.
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.stat().st_size != size or sha_file(path) != sha256:
                    raise StorageError("Immutable object identity conflict")
        finally:
            Path(temporary).unlink(missing_ok=True)

    def open(self, key):
        path = self._path(key)
        try:
            stream = path.open("rb")
        except FileNotFoundError as error:
            raise ObjectMissing("Object is missing") from error
        return ObjectReader(stream, os.fstat(stream.fileno()).st_size)


class S3ObjectStore(ObjectStore):
    def __init__(self, bucket: str, region: str, *, client=None):
        if not bucket or not region:
            raise StorageError("S3 bucket and AWS region are required")
        if client is None:
            import boto3
            from botocore.config import Config
            # Credentials come from the normal role/provider chain. No static
            # keys are accepted by this API or written into configuration.
            client = boto3.client("s3", region_name=region, config=Config(
                connect_timeout=5, read_timeout=30,
                retries={"mode": "standard", "total_max_attempts": 3},
            ))
        self.client, self.bucket = client, bucket

    def _matches(self, key, size, digest):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            metadata = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            raise StorageError("Cannot inspect private object") from error
        except BotoCoreError as error:
            raise StorageError("Cannot inspect private object") from error
        if metadata["ContentLength"] != size or metadata.get("Metadata", {}).get("sha256") != digest:
            raise StorageError("Immutable object identity conflict")
        return True

    def _put(self, key, stream, size, sha256, content_type):
        from botocore.exceptions import BotoCoreError, ClientError
        key = object_key(key)
        if self._matches(key, size, sha256):
            return
        try:
            self.client.put_object(
                Bucket=self.bucket, Key=key, Body=stream, ContentLength=size,
                ContentType=content_type, Metadata={"sha256": sha256},
                ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode("ascii"),
                IfNoneMatch="*", ServerSideEncryption="AES256",
            )
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (409, 412):
                if self._matches(key, size, sha256):
                    return
            raise StorageError("Private object upload failed") from error
        except (BotoCoreError, OSError) as error:
            raise StorageError("Private object upload failed") from error

    def open(self, key):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=object_key(key))
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                raise ObjectMissing("Object is missing") from error
            raise StorageError("Cannot read private object") from error
        except BotoCoreError as error:
            raise StorageError("Cannot read private object") from error
        return ObjectReader(response["Body"], response["ContentLength"])


def configured_store(data_root: Path) -> ObjectStore:
    mode = os.environ.get("MEMOVERSE_STORAGE")
    if mode == "local":
        return LocalObjectStore(data_root / "objects")
    if mode == "s3":
        return S3ObjectStore(os.environ.get("MEMOVERSE_S3_BUCKET", ""), os.environ.get("AWS_REGION", ""))
    raise StorageError("Set MEMOVERSE_STORAGE explicitly to local or s3")

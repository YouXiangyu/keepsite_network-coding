"""Private immutable object storage; ownership is enforced by the SQL caller."""
from __future__ import annotations

from contextlib import closing
import base64
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import tempfile
import time
from typing import BinaryIO
from urllib.parse import parse_qs, urlsplit


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
    def close(self):
        pass

    @property
    def storage_binding(self):
        return self.cache_identity

    @property
    def cache_identity(self):
        raise NotImplementedError

    def verify(self, key, size, sha256, content_type, *, stopping=lambda: False):
        """Read bounded bytes and verify identity; never trust a client receipt."""
        digest, total = hashlib.sha256(), 0
        try:
            if stopping():
                return False
            with closing(self.open(key)) as incoming:
                if incoming.size != size:
                    raise StorageError("Object length mismatch")
                while chunk := incoming.read(min(CHUNK_SIZE, size - total + 1)):
                    if stopping():
                        return False
                    total += len(chunk)
                    if total > size:
                        raise StorageError("Object length mismatch")
                    digest.update(chunk)
        except ObjectMissing:
            return False
        except OSError as error:
            raise StorageError("Object verification failed") from error
        if total != size or digest.hexdigest() != sha256:
            raise StorageError("Object integrity check failed")
        return True

    def put_file(self, key: str, path: Path, sha256: str, content_type: str):
        with path.open("rb") as stream:
            self._put(key, stream, path.stat().st_size, sha256, content_type)

    def put_bytes(self, key: str, value: bytes, sha256: str, content_type: str):
        self._put(key, io.BytesIO(value), len(value), sha256, content_type)

    def open(self, key: str) -> ObjectReader:
        raise NotImplementedError

    def _put(self, key: str, stream: BinaryIO, size: int, sha256: str, content_type: str):
        raise NotImplementedError


class LocalObjectStore(ObjectStore):
    """Explicit development/test storage, never an automatic S3 fallback."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def cache_identity(self):
        return "local:" + str(self.root)

    @property
    def storage_binding(self):
        # A verified local backup is restored in a new directory as one unit.
        return "local"

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
        self._owns_client = client is None
        if client is None:
            import boto3
            from botocore.config import Config
            # Credentials come from the normal role/provider chain. No static
            # keys are accepted by this API or written into configuration.
            client = boto3.client("s3", region_name=region, endpoint_url=f"https://s3.{region}.amazonaws.com", config=Config(
                signature_version="s3v4",
                connect_timeout=3, read_timeout=5,
                retries={"mode": "standard", "total_max_attempts": 2},
            ))
        self.client, self.bucket, self.region = client, bucket, region

    def close(self):
        if self._owns_client:
            self.client.close()

    @property
    def cache_identity(self):
        endpoint = getattr(getattr(self.client, "meta", None), "endpoint_url", "aws-default")
        return f"s3:{self.region}:{self.bucket}:{endpoint}"

    def verify(self, key, size, sha256, content_type, *, stopping=lambda: False):
        key = object_key(key)
        if stopping() or not self._matches(key, size, sha256, content_type):
            return False
        return not stopping()

    def download_url(self, key, *, expires_seconds=60):
        """Short HTTPS GET authorization; credentials never enter business state."""
        from botocore.exceptions import BotoCoreError, ClientError
        if expires_seconds != 60:
            raise StorageError("Unsupported download authorization lifetime")
        expected_hosts = {f"{self.bucket}.s3.{self.region}.amazonaws.com", f"s3.{self.region}.amazonaws.com"}
        endpoint = getattr(getattr(self.client, "meta", None), "endpoint_url", None)
        if endpoint is not None:
            parsed = urlsplit(endpoint)
            if parsed.scheme != "https" or parsed.hostname not in expected_hosts or parsed.username or parsed.password or parsed.port not in (None, 443):
                raise StorageError("Unapproved S3 endpoint")
        expires_at = int(time.time() * 1000) + expires_seconds * 1000
        try:
            url = self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": object_key(key)},
                                                     ExpiresIn=expires_seconds, HttpMethod="GET")
        except (BotoCoreError, ClientError) as error:
            raise StorageError("Cannot authorize private download") from error
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in expected_hosts or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise StorageError("Unapproved S3 download URL")
        query = parse_qs(parsed.query)
        if query.get("X-Amz-Algorithm") != ["AWS4-HMAC-SHA256"] or query.get("X-Amz-Expires") != ["60"]:
            raise StorageError("Download URL must use bounded SigV4 authorization")
        return url, expires_at

    def _matches(self, key, size, digest, content_type=None):
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            metadata = self.client.head_object(Bucket=self.bucket, Key=key, ChecksumMode="ENABLED")
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404:
                return False
            raise StorageError("Cannot inspect private object") from error
        except BotoCoreError as error:
            raise StorageError("Cannot inspect private object") from error
        if metadata.get("ContentLength") != size or metadata.get("Metadata", {}).get("sha256") != digest:
            raise StorageError("Immutable object identity conflict")
        expected_checksum = base64.b64encode(bytes.fromhex(digest)).decode("ascii")
        if metadata.get("ChecksumSHA256") != expected_checksum or metadata.get("ChecksumType") not in (None, "FULL_OBJECT"):
            raise StorageError("Verified full-object SHA-256 checksum unavailable or mismatched")
        if content_type is not None and metadata.get("ContentType") != content_type:
            raise StorageError("Object media type mismatch")
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
    mode = os.environ.get("KEEPSITE_STORAGE")
    if mode == "local":
        return LocalObjectStore(data_root / "objects")
    if mode == "s3":
        return S3ObjectStore(os.environ.get("KEEPSITE_S3_BUCKET", ""), os.environ.get("AWS_REGION", ""))
    raise StorageError("Set KEEPSITE_STORAGE explicitly to local or s3")

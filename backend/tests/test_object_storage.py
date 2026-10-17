"""Storage contract tests with an in-memory S3 client; no AWS requests."""
from __future__ import annotations

import base64
import hashlib
import io

from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
import pytest

from backend.object_storage import LocalObjectStore, ObjectMissing, S3ObjectStore, StorageError, object_key


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.readers = []
        self.writes = 0

    @staticmethod
    def error(status):
        return ClientError({"Error": {"Code": str(status), "Message": "test failure"}, "ResponseMetadata": {"HTTPStatusCode": status}}, "fixture")

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise self.error(404)
        raw, metadata = self.objects[Key]
        return {"ContentLength": len(raw), "Metadata": metadata}

    def put_object(self, **request):
        assert request["IfNoneMatch"] == "*"
        assert request["ServerSideEncryption"] == "AES256"
        assert "ACL" not in request
        if request["Key"] in self.objects:
            raise self.error(412)
        raw = request["Body"].read()
        assert len(raw) == request["ContentLength"]
        if base64.b64encode(hashlib.sha256(raw).digest()).decode() != request["ChecksumSHA256"]:
            raise self.error(400)
        self.objects[request["Key"]] = (raw, request["Metadata"])
        self.writes += 1

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise self.error(404)
        raw, _ = self.objects[Key]
        reader = io.BytesIO(raw)
        self.readers.append(reader)
        return {"Body": reader, "ContentLength": len(raw)}


def test_s3_objects_are_conditional_immutable_and_reused_without_new_put():
    client = FakeS3Client()
    store = S3ObjectStore("test-bucket", "ap-northeast-1", client=client)
    raw = b"original"
    digest = hashlib.sha256(raw).hexdigest()
    key = f"media/{digest}.png"
    store.put_bytes(key, raw, digest, "image/png")
    store.put_bytes(key, raw, digest, "image/png")
    assert client.writes == 1
    with pytest.raises(StorageError, match="identity"):
        store.put_bytes(key, b"different", hashlib.sha256(b"different").hexdigest(), "image/png")
    assert client.objects[key][0] == raw


def test_s3_stream_is_closed_when_response_iteration_ends_early():
    client = FakeS3Client()
    store = S3ObjectStore("test-bucket", "ap-northeast-1", client=client)
    raw = b"original"
    digest = hashlib.sha256(raw).hexdigest()
    store.put_bytes(f"media/{digest}.png", raw, digest, "image/png")
    reader = store.open(f"media/{digest}.png")
    chunks = reader.chunks()
    assert next(chunks) == raw
    chunks.close()
    assert client.readers[0].closed


def test_download_checks_hash_and_closes_s3_body_on_integrity_failure(tmp_path):
    client = FakeS3Client()
    store = S3ObjectStore("test-bucket", "ap-northeast-1", client=client)
    client.objects["media/test.png"] = (b"bytes", {"sha256": "wrong"})
    with pytest.raises(StorageError, match="integrity"):
        store.download("media/test.png", tmp_path / "download", "0" * 64)
    assert client.readers[0].closed


def test_s3_missing_object_is_not_silently_read_from_local_disk():
    store = S3ObjectStore("test-bucket", "ap-northeast-1", client=FakeS3Client())
    with pytest.raises(ObjectMissing):
        store.open("media/missing.png")


def test_local_objects_reject_conflicting_bytes_and_unapproved_paths(tmp_path):
    store = LocalObjectStore(tmp_path / "objects")
    raw = b"original"
    digest = hashlib.sha256(raw).hexdigest()
    store.put_bytes(f"media/{digest}.png", raw, digest, "image/png")
    with pytest.raises(StorageError):
        store.put_bytes(f"media/{digest}.png", b"changed", hashlib.sha256(b"changed").hexdigest(), "image/png")
    for key in ("../secret", "/absolute", "media/../secret", "media\\secret", "source.sqlite3"):
        with pytest.raises(StorageError):
            object_key(key)


@pytest.mark.parametrize("method,transport_error", [("head_object", EndpointConnectionError(endpoint_url="https://fixture.invalid")),
                                                   ("put_object", ReadTimeoutError(endpoint_url="https://fixture.invalid")),
                                                   ("get_object", EndpointConnectionError(endpoint_url="https://fixture.invalid"))], ids=["head-offline", "put-timeout", "get-offline"])
def test_s3_transport_failures_are_actionable_storage_errors(monkeypatch, method, transport_error):
    client = FakeS3Client()
    def fail(**kwargs):
        raise transport_error
    monkeypatch.setattr(client, method, fail)
    store = S3ObjectStore("test-bucket", "ap-northeast-1", client=client)
    with pytest.raises(StorageError):
        if method == "get_object":
            store.open("media/test.png")
        else:
            store.put_bytes("media/test.png", b"x", hashlib.sha256(b"x").hexdigest(), "image/png")

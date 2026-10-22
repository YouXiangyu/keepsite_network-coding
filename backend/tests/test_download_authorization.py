"""Short S3 download authorization without network calls or cloud credentials."""
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import boto3
from botocore.config import Config
from fastapi.testclient import TestClient
import pytest

from backend.object_storage import S3ObjectStore, StorageError
from account_fixtures import ORIGIN, signed_in
from test_api import make_app, create, delete, upload
from test_object_storage import FakeS3Client


def signer(region="ap-northeast-1", **kwargs):
    return boto3.client("s3", region_name=region, aws_access_key_id="synthetic-access",
                        aws_secret_access_key="synthetic-secret", config=Config(signature_version="s3v4"), **kwargs)


def test_real_sdk_presign_is_regional_sigv4_and_limited_to_sixty_seconds(monkeypatch):
    factory = boto3.client
    def synthetic_credentials(*args, **kwargs):
        return factory(*args, **kwargs, aws_access_key_id="synthetic-access", aws_secret_access_key="synthetic-secret")
    monkeypatch.setattr(boto3, "client", synthetic_credentials)
    store = S3ObjectStore("test-bucket", "ap-northeast-1")
    client = store.client
    try:
        assert client.meta.config.signature_version == "s3v4"
        before = int(time.time() * 1000)
        url, expires_at = store.download_url("media/" + "a" * 64 + ".png")
        parsed = urlsplit(url)
        assert parsed.hostname in {"test-bucket.s3.ap-northeast-1.amazonaws.com", "s3.ap-northeast-1.amazonaws.com"}
        query = parse_qs(parsed.query)
        assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
        assert query["X-Amz-Expires"] == ["60"]
        assert before < expires_at <= int(time.time() * 1000) + 60_000
    finally:
        client.close()


def test_s3_direct_download_is_opt_in_and_never_authorizes_unreferenced_or_foreign_media(tmp_path, monkeypatch):
    fake = FakeS3Client()
    signed = signer(endpoint_url="https://s3.ap-northeast-1.amazonaws.com")
    calls = []
    def presign(*args, **kwargs):
        calls.append(kwargs)
        return signed.generate_presigned_url(*args, **kwargs)
    fake.generate_presigned_url = presign
    app = make_app(tmp_path, S3ObjectStore("test-bucket", "ap-northeast-1", client=fake))
    try:
        with TestClient(app, base_url=ORIGIN) as client:
            signed_in(client)
            asset = upload(client)
            endpoint = asset["url"] + "/download"
            assert client.get(endpoint).status_code == 404
            create(client, asset=asset)
            monkeypatch.delenv("MEMOVERSE_DIRECT_DOWNLOAD", raising=False)
            fallback = client.get(endpoint).json()
            assert fallback["transport"] == "server" and fallback["url"] == asset["url"]
            assert fallback["expiresAt"] > int(time.time() * 1000)
            assert not calls
            monkeypatch.setenv("MEMOVERSE_DIRECT_DOWNLOAD", "1")
            authorized = client.get(endpoint).json()
            assert authorized["transport"] == "s3" and authorized["sha256"] == asset["assetId"]
            assert calls[0]["ExpiresIn"] == 60 and calls[0]["HttpMethod"] == "GET"
            signed_in(client, "bob")
            assert client.get(endpoint).status_code == 404
            assert len(calls) == 1
            signed_in(client)
            delete(client)
            assert client.get(endpoint).status_code == 404
            assert len(calls) == 1
            with app.state.store.transaction() as db:
                for table, column in (("tickets", "fields"), ("ticket_changes", "payload")):
                    assert all("X-Amz-" not in row[0] for row in db.execute(f"SELECT {column} FROM {table}"))
    finally:
        signed.close()


@pytest.mark.parametrize("endpoint", ["https://attacker.test", "http://s3.ap-northeast-1.amazonaws.com", "https://s3.ap-northeast-1.amazonaws.com:444"])
def test_custom_or_insecure_endpoint_cannot_receive_signed_authorization(endpoint):
    fake = FakeS3Client()
    fake.meta = SimpleNamespace(endpoint_url=endpoint)
    fake.generate_presigned_url = lambda *args, **kwargs: pytest.fail("Unapproved endpoint must be rejected before signing")
    with pytest.raises(StorageError, match="endpoint"):
        S3ObjectStore("test-bucket", "ap-northeast-1", client=fake).download_url("media/test.png")


def test_expired_download_authorization_is_reissued_with_new_deadline(monkeypatch):
    import backend.object_storage as module
    client = signer(endpoint_url="https://s3.ap-northeast-1.amazonaws.com")
    store = S3ObjectStore("test-bucket", "ap-northeast-1", client=client)
    try:
        now = [time.time()]
        monkeypatch.setattr(module.time, "time", lambda: now[0])
        _, first_expiry = store.download_url("media/" + "a" * 64 + ".png")
        now[0] += 61
        assert int(now[0] * 1000) > first_expiry
        _, new_expiry = store.download_url("media/" + "a" * 64 + ".png")
        assert new_expiry == first_expiry + 61_000
    finally:
        client.close()

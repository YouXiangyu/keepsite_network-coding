"""Synthetic cache failures must be misses, never replacement durable state."""
from copy import deepcopy
import hashlib
import json
import os
import uuid

import fakeredis
import pytest
from redis.exceptions import ConnectionError, TimeoutError

from backend.shared_state import RedisSharedState
from backend import shared_state


USER = "user-alice:with/special 字符"
SERVER = "database-instance-1"
STORAGE = "local:D:/owned-test/objects"


def snapshot(user=USER, server=SERVER, revision=7):
    asset = hashlib.sha256(b"synthetic-image-identity").hexdigest()
    return {"initialized": True, "serverId": server, "userId": user, "revision": revision,
            "data": {"tickets": [{
                "id": "ticket-1", "fields": {"title": "电影", "cinema": "", "hall": "", "seat": "", "showtime": ""},
                "fieldSources": {"title": "user", "cinema": "empty", "hall": "empty", "seat": "empty", "showtime": "empty"},
                "createdAt": 1, "updatedAt": 2, "version": 1, "inputVersion": 1, "deletedAt": None, "operationId": "create-1",
                "media": {"original": {"assetId": asset, "sha256": asset, "url": "/api/media/" + asset,
                    "mimeType": "image/png", "size": 10, "width": 2, "height": 2, "cloudStatus": "not_configured"}, "poster": None},
                "processing": {"status": "not_started", "error": None},
            }]}, "capabilities": {"processing": False, "processingUnavailableReason": None, "cloudStorage": False, "deviceSync": False}}


@pytest.fixture
def cache():
    client = fakeredis.FakeRedis()
    wrapper = RedisSharedState(client=client)
    yield wrapper
    wrapper.close()


def get(cache, revision=7, user=USER, server=SERVER, storage=STORAGE):
    return cache.get_snapshot(user, server, revision, storage)


def put(cache, value=None, revision=7, user=USER, server=SERVER, storage=STORAGE):
    return cache.put_snapshot(user, server, revision, storage, value if value is not None else snapshot(user, server, revision))


def raw_write(cache, value):
    cache.client.set(cache.snapshot_key(USER, SERVER, 7, STORAGE), json.dumps({
        "format": 1, "storageIdentity": STORAGE, "snapshot": value,
    }))


def test_cold_hot_cache_and_ttl_do_not_modify_the_snapshot(cache):
    source = snapshot()
    assert get(cache) is None
    assert put(cache, source)
    assert get(cache) == source
    key = cache.snapshot_key(USER, SERVER, 7, STORAGE)
    assert 0 < cache.client.ttl(key) <= 60
    assert USER not in key and STORAGE not in key and SERVER not in key
    returned = get(cache)
    returned["data"]["tickets"][0]["fields"]["title"] = "client-local mutation"
    assert get(cache) == source
    assert source == snapshot()
    cache.client.expire(key, 0)
    assert get(cache) is None


def test_account_database_revision_and_storage_are_independent_namespaces(cache):
    assert put(cache)
    for args in ({"user": "bob"}, {"server": "restored-server"}, {"revision": 8}, {"storage": "s3:another-bucket"}):
        assert get(cache, **args) is None
    assert put(cache, revision=8)
    assert get(cache, revision=8)["revision"] == 8
    assert get(cache)["revision"] == 7


@pytest.mark.parametrize("damage", ["owner", "server", "revision", "missing-data", "duplicate-ticket", "missing-field", "field-type",
                                    "field-source", "media-hash", "media-url", "media-dimensions", "missing-poster", "processing",
                                    "capability", "deleted", "missing-operation", "time", "bool-revision"])
def test_corrupt_or_cross_account_cache_contents_are_never_returned(cache, damage):
    payload = snapshot()
    ticket = payload["data"]["tickets"][0]
    if damage == "owner": payload["userId"] = "bob"
    elif damage == "server": payload["serverId"] = "another-database"
    elif damage == "revision": payload["revision"] = 8
    elif damage == "missing-data": payload["data"] = None
    elif damage == "duplicate-ticket": payload["data"]["tickets"].append(deepcopy(ticket))
    elif damage == "missing-field": del ticket["fields"]["title"]
    elif damage == "field-type": ticket["fields"]["seat"] = []
    elif damage == "field-source": ticket["fieldSources"]["seat"] = "unverified"
    elif damage == "media-hash": ticket["media"]["original"]["sha256"] = "b" * 64
    elif damage == "media-url": ticket["media"]["original"]["url"] = "https://other.example/private"
    elif damage == "media-dimensions": ticket["media"]["original"]["width"] = 0
    elif damage == "missing-poster": del ticket["media"]["poster"]
    elif damage == "processing": ticket["processing"]["status"] = "invented"
    elif damage == "capability": payload["capabilities"]["cloudStorage"] = "true"
    elif damage == "deleted": ticket["deletedAt"] = 3
    elif damage == "missing-operation": del ticket["operationId"]
    elif damage == "time": ticket["updatedAt"] = 0
    elif damage == "bool-revision": payload["revision"] = True
    raw_write(cache, payload)
    assert get(cache) is None
    assert not put(cache, payload)


@pytest.mark.parametrize("raw", [b"not-json", b"\xff", b"null", b"[]", b'{"format":1,"format":1}',
                                  b'{"snapshot":NaN}', b'{"format":2}', b'{"format":1,"storageIdentity":"other"}'])
def test_invalid_envelopes_are_cache_misses(cache, raw):
    cache.client.set(cache.snapshot_key(USER, SERVER, 7, STORAGE), raw)
    assert get(cache) is None


def test_oversized_snapshots_are_not_cached_or_returned(cache, monkeypatch):
    assert put(cache)
    monkeypatch.setattr(shared_state, "MAX_SNAPSHOT_BYTES", 20)
    assert get(cache) is None
    assert not put(cache)


@pytest.mark.parametrize("status", ["not_started", "queued", "ocr", "poster", "ready", "no_match", "failed"])
def test_m3_processing_states_and_safe_attribution_survive_cache(cache, status):
    payload = snapshot()
    payload["capabilities"].update(processing=True, processingUnavailableReason=None)
    ticket = payload["data"]["tickets"][0]
    ticket["processing"] = {"status": status, "error": "处理失败，请重试。" if status == "failed" else None}
    source = {"provider": "wikimedia_commons", "sourcePage": "https://commons.wikimedia.org/wiki/File:Poster.jpg",
              "sourceFile": "File:Poster.jpg", "licenseName": "Public domain",
              "licenseUrl": "https://commons.wikimedia.org/wiki/File:Poster.jpg#Licensing", "author": "Studio", "fetchedAt": 1}
    ticket["media"]["poster"] = {**ticket["media"]["original"], "cloudStatus": "saved", "source": source}
    assert put(cache, payload) and get(cache) == payload
    source["licenseUrl"] = "https://evil.test"
    raw_write(cache, payload)
    assert get(cache) is None and not put(cache, payload)


def test_missing_capability_reason_and_nontext_processing_error_are_not_cached(cache):
    payload = snapshot()
    del payload["capabilities"]["processingUnavailableReason"]
    assert not put(cache, payload)
    payload = snapshot()
    payload["data"]["tickets"][0]["processing"]["error"] = {"private": "data"}
    assert not put(cache, payload)
    payload = snapshot()
    payload["data"]["tickets"][0]["processing"]["status"] = "ready"
    assert not put(cache, payload)


@pytest.mark.parametrize("method,error", [("get", ConnectionError("offline")), ("get", TimeoutError("timeout")),
                                         ("set", ConnectionError("offline")), ("set", TimeoutError("timeout"))])
def test_read_write_failures_leave_the_caller_snapshot_intact(cache, monkeypatch, method, error):
    source = snapshot()
    assert put(cache, source)
    def unavailable(*args, **kwargs):
        raise error
    monkeypatch.setattr(cache.client, method, unavailable)
    if method == "get":
        assert get(cache) is None
    else:
        assert not put(cache, source)
    assert source == snapshot()


def test_cache_loss_and_recreated_clients_can_rebuild_from_source_snapshot():
    server = fakeredis.FakeServer()
    source = snapshot()
    first = RedisSharedState(client=fakeredis.FakeRedis(server=server))
    second = RedisSharedState(client=fakeredis.FakeRedis(server=server))
    try:
        assert put(first, source)
        assert get(second) == source
        first.client.flushdb()  # Only this test's isolated in-memory FakeServer.
        assert get(second) is None
        assert put(second, source)
        assert get(first) == source
        assert source == snapshot()
    finally:
        first.close()
        second.close()


def test_disabled_or_invalid_environment_does_not_connect(monkeypatch, caplog):
    monkeypatch.delenv("MEMOVERSE_REDIS_URL", raising=False)
    cache = RedisSharedState.from_env()
    assert cache.client is None and get(cache) is None and not put(cache)
    cache.close()
    monkeypatch.setenv("MEMOVERSE_REDIS_URL", "invalid://secret-password@host")
    disabled = RedisSharedState.from_env()
    assert disabled.client is None
    assert "secret-password" not in caplog.text


def test_connection_budget_overrides_url_options_and_close_releases_owned_pool(monkeypatch):
    cache = RedisSharedState(url="redis://localhost:6379/15?socket_timeout=90&socket_connect_timeout=90&decode_responses=True")
    pool = cache.client.connection_pool
    options = pool.connection_kwargs
    assert options["socket_timeout"] == options["socket_connect_timeout"] == 0.5
    assert options["decode_responses"] is False
    assert options["retry"].get_retries() == 0
    assert options["retry_on_error"] == [] and options["retry_on_timeout"] is False
    closed = []
    monkeypatch.setattr(pool, "disconnect", lambda **kwargs: closed.append(True))
    cache.close()
    cache.close()
    assert closed == [True] and cache.client is None


def test_real_redis_roundtrip_loss_and_cross_client_visibility():
    url = os.environ.get("MEMOVERSE_TEST_REDIS_URL")
    if not url:
        if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("CI must provide MEMOVERSE_TEST_REDIS_URL; real Redis coverage cannot be skipped")
        pytest.skip("No local Redis service configured; real Redis is required in Application CI")
    # An unavailable configured service is a failure, not a skip/fallback pass.
    first = RedisSharedState(url=url)
    second = RedisSharedState(url=url)
    server = "integration-" + uuid.uuid4().hex
    key = first.snapshot_key(USER, server, 7, STORAGE)
    try:
        assert first.client.ping()
        payload = snapshot(server=server)
        assert get(first, server=server) is None
        assert put(first, payload, server=server)
        assert get(second, server=server) == payload
        assert 0 < first.client.ttl(key) <= 60
        assert get(second, server=server, user="bob") is None
        first.client.delete(key)
        assert get(second, server=server) is None
        assert put(second, payload, server=server)
        assert get(first, server=server) == payload
    finally:
        try:
            first.client.delete(key)  # Only the exact UUID-scoped key owned here.
        finally:
            first.close()
            second.close()

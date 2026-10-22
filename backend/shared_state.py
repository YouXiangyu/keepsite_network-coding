"""Rebuildable, account-scoped Redis snapshots. SQL remains the source of truth.

Callers obtain identity/revision from a completed SQL transaction before using
this cache. Redis I/O must never run inside a SQL write transaction.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

from pydantic import TypeAdapter, ValidationError
from redis import ConnectionPool, Redis
from redis.backoff import NoBackoff
from redis.connection import parse_url
from redis.exceptions import RedisError
from redis.retry import Retry

from .contracts import FIELD_NAMES, Identifier, TicketFields
from .posters import validate_source

log = logging.getLogger(__name__)
FORMAT_VERSION = 1
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
IDENTIFIER = TypeAdapter(Identifier)


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value <= 2**53 - 1


def _media(value):
    if not isinstance(value, dict):
        return False
    identifier = value.get("assetId")
    return (isinstance(identifier, str) and len(identifier) == 64
            and all(char in "0123456789abcdef" for char in identifier)
            and value.get("sha256") == identifier
            and value.get("url") == "/api/media/" + identifier
            and value.get("mimeType") in ("image/png", "image/jpeg", "image/webp")
            and _integer(value.get("size"), 1) and value["size"] <= 12 * 1024 * 1024
            and _integer(value.get("width"), 1) and _integer(value.get("height"), 1)
            and value["width"] * value["height"] <= 40_000_000
            and value.get("cloudStatus") in ("saved", "not_configured"))


def _valid_snapshot(payload, user_id, server_id, revision):
    if not isinstance(payload, dict) or payload.get("initialized") is not True:
        return False
    if (payload.get("userId") != user_id or payload.get("serverId") != server_id
            or not _integer(payload.get("revision")) or payload["revision"] != revision):
        return False
    capabilities, data = payload.get("capabilities"), payload.get("data")
    if (not isinstance(capabilities, dict) or type(capabilities.get("processing")) is not bool
            or capabilities.get("deviceSync") is not False
            or type(capabilities.get("cloudStorage")) is not bool
            or "processingUnavailableReason" not in capabilities
            or capabilities["processingUnavailableReason"] is not None and (
                not isinstance(capabilities["processingUnavailableReason"], str) or len(capabilities["processingUnavailableReason"]) > 1000)
            or not isinstance(data, dict) or not isinstance(data.get("tickets"), list)):
        return False
    identities = set()
    try:
        for ticket in data["tickets"]:
            if not isinstance(ticket, dict):
                return False
            identifier = IDENTIFIER.validate_python(ticket.get("id"))
            IDENTIFIER.validate_python(ticket.get("operationId"))
            if identifier in identities:
                return False
            identities.add(identifier)
            fields, sources = ticket.get("fields"), ticket.get("fieldSources")
            if (not isinstance(fields, dict) or set(fields) != set(FIELD_NAMES)
                    or TicketFields.model_validate(fields).model_dump() != fields
                    or not isinstance(sources, dict) or set(sources) != set(FIELD_NAMES)
                    or any(source not in ("user", "ocr", "migration", "empty") for source in sources.values())):
                return False
            if (not _integer(ticket.get("createdAt")) or not _integer(ticket.get("updatedAt"))
                    or ticket["updatedAt"] < ticket["createdAt"]
                    or not _integer(ticket.get("version"), 1) or not _integer(ticket.get("inputVersion"), 1)
                    or "deletedAt" not in ticket or ticket["deletedAt"] is not None):
                return False
            media, processing = ticket.get("media"), ticket.get("processing")
            if (not isinstance(media, dict) or not _media(media.get("original"))
                    or "poster" not in media or media["poster"] is not None and (
                        not _media(media["poster"]) or not validate_source(media["poster"].get("source")))
                    or not isinstance(processing, dict) or processing.get("status") not in (
                        "not_started", "queued", "ocr", "poster", "ready", "no_match", "failed")
                    or "error" not in processing or processing["error"] is not None and (
                        not isinstance(processing["error"], str) or len(processing["error"]) > 1000)
                    or processing.get("status") == "ready" and (
                        media["poster"] is None or media["poster"].get("cloudStatus") != "saved")):
                return False
    except (ValidationError, TypeError, ValueError):
        return False
    return True


def _decode(raw):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate cache field")
            value[key] = item
        return value

    def constant(_value):
        raise ValueError("Non-finite cache value")

    return json.loads(raw, object_pairs_hook=unique, parse_constant=constant)


class RedisSharedState:
    """Best-effort snapshots; client is also available to later shared-state work."""

    def __init__(self, *, client=None, url=None, ttl_seconds=60):
        if type(ttl_seconds) is not int or ttl_seconds < 1:
            raise ValueError("Snapshot TTL must be a positive integer")
        if client is not None and url is not None:
            raise ValueError("Supply either a Redis client or URL")
        self.ttl_seconds = ttl_seconds
        self._pool = None
        if url:
            # URL options cannot override the latency budget or enable retries.
            options = {**parse_url(url), "socket_connect_timeout": 0.5, "socket_timeout": 0.5,
                       "decode_responses": False, "retry": Retry(NoBackoff(), 0),
                       "retry_on_timeout": False, "retry_on_error": [], "health_check_interval": 0}
            self._pool = ConnectionPool(**options)
            client = Redis(connection_pool=self._pool)
        self.client = client

    @classmethod
    def from_env(cls):
        url = os.environ.get("MEMOVERSE_REDIS_URL", "").strip()
        try:
            return cls(url=url or None)
        except (ValueError, TypeError, RedisError):
            # Never include a connection URL, credentials or cache contents.
            log.warning("Redis snapshot configuration unavailable; database reads remain enabled")
            return cls()

    @staticmethod
    def snapshot_key(user_id, server_id, revision, storage_identity):
        if (not isinstance(user_id, str) or not user_id or not isinstance(server_id, str) or not server_id
                or not _integer(revision) or not isinstance(storage_identity, str) or not storage_identity):
            raise ValueError("Snapshot identity is incomplete")
        scope = json.dumps([user_id, server_id, storage_identity], ensure_ascii=False, separators=(",", ":"))
        return f"memoverse:snapshot:v{FORMAT_VERSION}:{hashlib.sha256(scope.encode()).hexdigest()}:{revision}"

    def get_snapshot(self, user_id, server_id, revision, storage_identity):
        if self.client is None:
            return None
        try:
            raw = self.client.get(self.snapshot_key(user_id, server_id, revision, storage_identity))
            if not isinstance(raw, (bytes, str)) or len(raw) > MAX_SNAPSHOT_BYTES:
                return None
            envelope = _decode(raw)
            if (not isinstance(envelope, dict) or envelope.get("format") != FORMAT_VERSION
                    or envelope.get("storageIdentity") != storage_identity):
                return None
            payload = envelope.get("snapshot")
            return payload if _valid_snapshot(payload, user_id, server_id, revision) else None
        except (RedisError, OSError, UnicodeError, ValueError, TypeError, RecursionError):
            return None

    def put_snapshot(self, user_id, server_id, revision, storage_identity, payload):
        if self.client is None or not _valid_snapshot(payload, user_id, server_id, revision):
            return False
        try:
            key = self.snapshot_key(user_id, server_id, revision, storage_identity)
            raw = json.dumps({"format": FORMAT_VERSION, "storageIdentity": storage_identity, "snapshot": payload},
                             ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
            if len(raw) > MAX_SNAPSHOT_BYTES:
                return False
            return bool(self.client.set(key, raw, ex=self.ttl_seconds))
        except (RedisError, OSError, UnicodeError, ValueError, TypeError, RecursionError):
            return False

    def close(self):
        client, self.client = self.client, None
        try:
            if client is not None:
                client.close()
        except (RedisError, OSError):
            pass
        finally:
            if self._pool is not None:
                pool, self._pool = self._pool, None
                try:
                    pool.disconnect()
                except (RedisError, OSError):
                    pass

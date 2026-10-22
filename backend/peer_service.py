"""Disposable peer discovery and signalling; SQL and login sessions authorize every asset."""
import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import json
import sqlite3
import threading
import time
import uuid

from redis.exceptions import RedisError

from .auth import AuthError
from .peer_contracts import LIMITS, MAX_CONTROL_BYTES, MAX_FRAME_BYTES, PeerProtocolError, parse_message, strict_json, token
from .peer_registry import PeerActor, PeerRegistry, RegistryError
from .store import StoreError

ERROR_CODES = {"invalid", "not_found", "expired", "conflict", "capacity", "rate_limited", "unavailable", "unauthorized"}
TRANSFER_EVENTS = {"peer.request", "peer.authorized", "peer.signal", "peer.lease", "peer.pending"}


@dataclass
class PeerConnection:
    subscription: object
    connection_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    actor: object = None
    opted_in: bool = False
    closed: bool = False
    generation: int = 0
    queue: deque = field(default_factory=deque)
    queued_bytes: int = 0
    transfers: dict = field(default_factory=dict)
    tokens: float = 40
    discovery_tokens: float = 8
    checked_at: float = field(default_factory=time.monotonic)

    def check_rate(self, discovery=False):
        now = time.monotonic()
        elapsed, self.checked_at = max(0, now - self.checked_at), now
        self.tokens = min(40, self.tokens + elapsed * 20)
        self.discovery_tokens = min(8, self.discovery_tokens + elapsed * 4)
        if self.tokens < 1 or discovery and self.discovery_tokens < 1:
            raise RegistryError("rate_limited")
        self.tokens -= 1
        if discovery:
            self.discovery_tokens -= 1

    def pop(self):
        if not self.queue:
            return None
        message, size = self.queue.popleft()
        self.queued_bytes -= size
        return message


class PeerService:
    def __init__(self, store, shared_state, auth, server_id):
        self.store, self.client, self.auth, self.server_id = store, shared_state.client, auth, server_id
        self.process_id = uuid.uuid4().hex
        self.registry = PeerRegistry(self.client, server_id, store.storage.cache_identity)
        scope = json.dumps([server_id, store.storage.cache_identity], separators=(",", ":"))
        self.channel_prefix = "memoverse:peer:v1:" + hashlib.sha256(scope.encode()).hexdigest() + ":"
        self._loop = self._task = self._thread = None
        self._stopping, self._ready = threading.Event(), threading.Event()
        self._lock = threading.Lock()
        self._connections, self._incoming, self._dirty = {}, deque(), set()
        self._incoming_bytes = 0
        self._generation = 0
        self._unsubscribers = []
        self._workers = {}
        self._closing = {}

    async def _run(self, function, *args, cleanup=False, connection=None):
        """Cancellation stops the caller, never abandons Redis/parser work during shutdown."""
        if not cleanup and (self._stopping.is_set() or connection is not None and connection.closed):
            raise RegistryError("unavailable")
        task = asyncio.create_task(asyncio.to_thread(function, *args))
        self._workers[task] = connection
        def finished(worker):
            self._workers.pop(worker, None)
            if not worker.cancelled():
                worker.exception()  # Retrieve failures even when a timed-out caller has left.
        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _wait_workers(self, connection=None):
        workers = tuple(task for task, owner in self._workers.items() if connection is None or owner is connection)
        if workers:
            await asyncio.shield(asyncio.gather(*workers, return_exceptions=True))

    async def start(self):
        if self._loop is not None:
            raise RuntimeError("Peer service already started")
        self._loop = asyncio.get_running_loop()
        self._stopping.clear()
        self._unsubscribers.append(self.store.subscribe_changes(self._committed))
        self._unsubscribers.append(self.auth.subscribe_revocations(self._revoked))
        if self.client is not None:
            thread = threading.Thread(target=self._subscriber, name="memoverse-peer-subscriber", daemon=True)
            thread.start()
            self._thread = thread
        self._task = asyncio.create_task(self._pump(), name="memoverse-peer-maintenance")

    async def stop(self):
        self._stopping.set()
        self._ready.clear()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._wait_workers()
        connections = tuple(self._connections.values())
        semaphore = asyncio.Semaphore(8)
        async def cleanup(connection):
            async with semaphore:
                await self.detach(connection)
        await asyncio.gather(*(cleanup(connection) for connection in connections))
        while self._closing:
            await asyncio.shield(asyncio.gather(*tuple(self._closing.values()), return_exceptions=True))
        while self._workers:
            await self._wait_workers()
        if self._thread:
            await self._run(self._thread.join, cleanup=True)
            self._thread = None
        with self._lock:
            self._incoming.clear()
            self._incoming_bytes = 0
            self._dirty.clear()
        self._loop = None

    def attach(self, subscription):
        if self._stopping.is_set() or self._loop is None:
            raise RegistryError("unavailable")
        connection = PeerConnection(subscription)
        self._connections[connection.connection_id] = connection
        return connection

    async def detach(self, connection):
        task = self._closing.get(connection.connection_id)
        if task is None:
            if connection.closed and connection.connection_id not in self._connections:
                return
            connection.closed = True
            task = asyncio.create_task(self._finish_detach(connection))
            self._closing[connection.connection_id] = task
        await asyncio.shield(task)

    async def _finish_detach(self, connection):
        try:
            await self._wait_workers(connection)
            await self._run(self._unregister, connection, "disconnected", cleanup=True, connection=connection)
        finally:
            connection.queue.clear()
            connection.queued_bytes = 0
            self._connections.pop(connection.connection_id, None)
            self._closing.pop(connection.connection_id, None)

    def _committed(self, user_id, _cursor):
        with self._lock:
            if len(self._dirty) < 1024:
                self._dirty.add(user_id)

    def _revoked(self, session_key):
        # The existing WS hub closes matching connections. Queue a prompt transfer check too.
        for connection in tuple(self._connections.values()):
            if connection.subscription.session_key == session_key:
                self._committed(connection.subscription.user_id, 0)

    def _envelope(self, connection, message_type, request_id=None, **payload):
        return {"v": 1, "type": message_type, "serverId": self.server_id,
                "userId": connection.subscription.user_id,
                **({"requestId": request_id} if request_id is not None else {}), **payload}

    def _emit(self, connection, message_type, request_id=None, *, transfer=None, **payload):
        if self._loop is not None and not self._stopping.is_set():
            message = self._envelope(connection, message_type, request_id, **payload)
            self._loop.call_soon_threadsafe(self._enqueue, connection, message, transfer)

    @staticmethod
    def _enqueue(connection, message, transfer=None):
        if connection.closed or not connection.opted_in:
            return
        if message["type"] == "peer.cancelled" and "requestId" not in message and message["transferId"] not in connection.transfers:
            return
        size = len(json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode())
        maximum = MAX_FRAME_BYTES if message["type"] == "peer.signal" else MAX_CONTROL_BYTES
        if size > maximum or len(connection.queue) >= 32 or connection.queued_bytes + size > 256 * 1024:
            connection.subscription.close(1013)
            return
        if transfer is not None:
            connection.transfers[transfer["transferId"]] = transfer
        if message["type"] == "peer.cancelled":
            connection.transfers.pop(message["transferId"], None)
        connection.queue.append((message, size))
        connection.queued_bytes += size
        connection.subscription.changed.set()

    def _registered(self, connection, request_id=None, *, peer=None, reason=None):
        self._emit(connection, "device.registered", request_id, peerId=peer["peerId"] if peer else None,
                   enabled=peer is not None, reason=reason, heartbeatMs=15000, leaseMs=45000,
                   inventoryRevision=peer["manifestRevision"] if peer else 0,
                   limits=LIMITS, iceServers=[], turnEnabled=False)

    def _session(self, user_id, session_key):
        result = self.auth.websocket_session_key(session_key)
        if result["user"]["id"] != user_id or result["sessionKey"] != session_key:
            raise RegistryError("not_found")
        return result

    def _peer_session(self, user_id, session_key):
        try:
            return self._session(user_id, session_key)
        except AuthError as error:
            # A revoked remote peer must not invalidate the requester's own login.
            raise RegistryError("not_found" if error.status in (401, 403) else "unavailable") from None

    def _media(self, user_id, asset_id):
        with self.store.transaction(timeout=1) as db:
            asset = self.store.referenced_asset(db, user_id, asset_id)
            server_id = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
            if server_id != self.server_id:
                raise RegistryError("unavailable")
            return {"assetId": asset["id"], "sha256": asset["id"], "size": asset["size"], "mimeType": asset["mime"]}

    def _assets(self, user_id, identifiers):
        with self.store.transaction(timeout=1) as db:
            for identifier in identifiers:
                self.store.referenced_asset(db, user_id, identifier)

    def _authorize_transfer(self, actor, transfer_id):
        record = self.registry.get_transfer(actor, transfer_id)
        session_expiry = None
        for role in ("requester", "source"):
            session = self._peer_session(record["userId"], record[role]["sessionKey"])
            session_expiry = session["expiresAt"] if session_expiry is None else min(session_expiry, session["expiresAt"])
        if self._media(record["userId"], record["assetId"]) != record["media"]:
            raise RegistryError("not_found")
        record["leaseMs"] = min(record["leaseMs"], max(0, session_expiry - int(time.time() * 1000)))
        if record["leaseMs"] <= 0:
            raise RegistryError("expired")
        return record

    def _route(self, record, endpoint, message_type, request_id=None, **payload):
        target = record[endpoint]
        if target["processId"] == self.process_id:
            connection = self._connections.get(target["connectionId"])
            if connection and connection.actor and connection.actor.peer_id == target["peerId"]:
                self._emit(connection, message_type, request_id, transfer=record if message_type != "peer.cancelled" else None, **payload)
            return
        message = {"v": 1, "serverId": self.server_id, "userId": record["userId"],
                   "targetPeerId": target["peerId"], "targetConnectionId": target["connectionId"],
                   "type": message_type, "payload": payload}
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode()) > MAX_FRAME_BYTES + 2048:
            raise RegistryError("invalid")
        if self.client is None or self.client.publish(self.channel_prefix + target["processId"], raw) < 1:
            raise RegistryError("unavailable")

    def _cancel(self, connection, record, reason, request_id=None):
        try:
            if connection.actor:
                self.registry.cancel_transfer(connection.actor, record["transferId"], reason)
        except RegistryError:
            pass
        for endpoint in ("requester", "source"):
            try:
                own = record[endpoint]["connectionId"] == connection.connection_id and record[endpoint]["processId"] == self.process_id
                self._route(record, endpoint, "peer.cancelled", request_id if own else None,
                            transferId=record["transferId"], reason=reason)
            except (RegistryError, RedisError, OSError):
                pass
    def _unregister(self, connection, reason):
        actor, connection.actor = connection.actor, None
        records = list(connection.transfers.values())
        connection.transfers.clear()
        if actor:
            try:
                records.extend(self.registry.unregister(actor))
            except (RegistryError, RedisError, OSError):
                pass
        if self._stopping.is_set():
            # Shutdown closes sockets; remote peers also have <=10s asset leases.
            # Do not multiply Redis outage latency by publishing to every endpoint.
            return
        seen = set()
        for record in records:
            if record["transferId"] not in seen:
                seen.add(record["transferId"])
                self._cancel(connection, record, reason)

    def _disable(self, connection, reason="redis_unavailable"):
        self._unregister(connection, "unavailable")
        self._registered(connection, reason=reason)

    async def dispatch(self, connection, command):
        if command["type"] == "device.register":
            connection.opted_in = True
        elif not connection.opted_in:
            raise RegistryError("invalid")
        try:
            connection.check_rate(command["type"] in {"peer.find", "peer.open"})
            await self._run(self._execute, connection, command, connection=connection)
        except AuthError:
            raise
        except StoreError:
            self._emit(connection, "peer.error", command["requestId"], code="not_found",
                       **({"transferId": command["transferId"]} if "transferId" in command else {}))
        except (RegistryError, RedisError, OSError, sqlite3.Error) as error:
            code = error.code if isinstance(error, RegistryError) else "unavailable"
            if code not in ERROR_CODES:
                code = "unavailable"
            if code == "unavailable" and connection.actor:
                await self._run(self._disable, connection, connection=connection)
            if command["type"] == "device.register" and code == "unavailable":
                self._registered(connection, command["requestId"], reason="redis_unavailable")
                return
            self._emit(connection, "peer.error", command["requestId"], code=code,
                       **({"transferId": command["transferId"]} if "transferId" in command else {}))

    def _execute(self, connection, body):
        if connection.closed:
            return
        user_id, session_key = connection.subscription.user_id, connection.subscription.session_key
        self._session(user_id, session_key)
        kind, request_id = body["type"], body["requestId"]
        if kind == "device.unregister":
            self._unregister(connection, "disabled")
            self._emit(connection, "device.unregistered", request_id)
            return
        if kind == "device.register":
            if self.client is None or not self._ready.is_set():
                self._unregister(connection, "unavailable")
                self._registered(connection, request_id, reason="redis_unavailable")
                return
            if connection.actor and connection.generation != self._generation:
                self._unregister(connection, "unavailable")
            if connection.actor:
                try:
                    peer = self.registry.touch(connection.actor)
                    self._registered(connection, request_id, peer=peer)
                    return
                except RegistryError as error:
                    if error.code == "unavailable":
                        raise
                    self._unregister(connection, "expired")
            peer = self.registry.register(user_id, session_key, self.process_id, connection.connection_id, body["name"])
            connection.actor = PeerActor(user_id, peer["peerId"], session_key, self.process_id, connection.connection_id)
            connection.generation = self._generation
            if connection.closed:
                self._unregister(connection, "disconnected")
                return
            self._registered(connection, request_id, peer=peer)
            return
        actor = connection.actor
        if actor is None:
            raise RegistryError("unavailable")
        if not self._ready.is_set() or connection.generation != self._generation:
            raise RegistryError("unavailable")
        if kind == "device.heartbeat":
            peer = self.registry.touch(actor)
            self._emit(connection, "device.alive", request_id, leaseMs=45000, inventoryRevision=peer["manifestRevision"])
        elif kind == "device.list":
            self.registry.get_peer(user_id, actor.peer_id)
            peers = self.registry.list_peers(user_id)
            self._emit(connection, "device.peers", request_id, peers=[{"peerId": peer["peerId"], "name": peer["name"]} for peer in peers])
        elif kind in {"cache.replace", "cache.patch"}:
            identifiers = body["assetIds"] if kind == "cache.replace" else body["add"]
            if kind == "cache.replace" and body["batchIndex"] == body["batchCount"] - 1:
                identifiers = self.registry.staged_assets(actor, body["revision"]) + identifiers
            self._assets(user_id, identifiers)
            if kind == "cache.replace":
                ack = self.registry.replace_manifest(actor, body["revision"], body["batchIndex"], body["batchCount"], body["assetIds"])
            else:
                ack = self.registry.patch_manifest(actor, body["revision"], body["add"], body["remove"])
            self._emit(connection, "cache.ack", request_id, **ack)
        elif kind == "peer.find":
            self._media(user_id, body["assetId"])
            peers = self.registry.find_sources(actor, body["assetId"])
            valid = []
            for peer in peers:
                try:
                    self._session(user_id, peer["sessionKey"])
                    valid.append({"peerId": peer["peerId"], "name": peer["name"]})
                except AuthError:
                    continue
            self._emit(connection, "peer.sources", request_id, assetId=body["assetId"], peers=valid)
        elif kind == "peer.open":
            media = self._media(user_id, body["assetId"])
            source = self.registry.get_peer(user_id, body["targetPeerId"])
            self._peer_session(user_id, source["sessionKey"])
            record = self.registry.open_transfer(actor, body["targetPeerId"], body["assetId"], media)
            connection.transfers[record["transferId"]] = record
            try:
                self._route(record, "source", "peer.request", transferId=record["transferId"],
                            requesterPeerId=actor.peer_id, media=media, leaseMs=5000)
                self._emit(connection, "peer.pending", request_id, transfer=record, transferId=record["transferId"])
            except (RegistryError, RedisError, OSError):
                self._cancel(connection, record, "unavailable")
                raise RegistryError("unavailable") from None
        else:
            transfer_id = body["transferId"]
            record = self._authorize_transfer(actor, transfer_id)
            if kind == "peer.accept":
                record = self.registry.accept_transfer(actor, transfer_id)
                for endpoint in ("source", "requester"):
                    self._route(record, endpoint, "peer.authorized", request_id if endpoint == "source" else None,
                                transferId=transfer_id, media=record["media"], sourcePeerId=record["source"]["peerId"],
                                requesterPeerId=record["requester"]["peerId"], offererPeerId=record["requester"]["peerId"], leaseMs=record["leaseMs"])
            elif kind == "peer.signal":
                payload_bytes = len(json.dumps(body["payload"], ensure_ascii=False, separators=(",", ":")).encode())
                record = self.registry.signal_transfer(actor, transfer_id, body["kind"], payload_bytes)
                target = "source" if actor.peer_id == record["requester"]["peerId"] else "requester"
                self._route(record, target, "peer.signal", transferId=transfer_id, fromPeerId=actor.peer_id,
                            kind=body["kind"], payload=body["payload"])
                self._emit(connection, "peer.signalled", request_id, transferId=transfer_id)
            elif kind == "peer.renew":
                record = self.registry.renew_transfer(actor, transfer_id)
                self._emit(connection, "peer.lease", request_id, transfer=record, transferId=transfer_id, leaseMs=record["leaseMs"])
            elif kind == "peer.cancel":
                self._cancel(connection, record, body["reason"], request_id)

    async def prepare_outgoing(self, connection, message):
        if message["type"] not in TRANSFER_EVENTS:
            return message
        try:
            actor = connection.actor
            if actor is None or not self._ready.is_set() or connection.generation != self._generation:
                raise RegistryError("expired")
            record = await self._run(self._authorize_transfer, actor, message["transferId"], connection=connection)
            if "leaseMs" in message:
                message = {**message, "leaseMs": min(message["leaseMs"], record["leaseMs"])}
            return message
        except (RegistryError, AuthError, StoreError, RedisError, OSError, sqlite3.Error):
            connection.transfers.pop(message["transferId"], None)
            return self._envelope(connection, "peer.cancelled", message.get("requestId"),
                                  transferId=message["transferId"], reason="expired")

    def _subscriber(self):
        while not self._stopping.is_set():
            pubsub = None
            try:
                pubsub = self.client.pubsub()
                pubsub.subscribe(self.channel_prefix + self.process_id)
                while not self._stopping.is_set():
                    item = pubsub.get_message(timeout=0.5)
                    if item and item["type"] == "subscribe":
                        self._ready.set()
                    elif item and item["type"] == "message":
                        raw = item["data"]
                        if isinstance(raw, str):
                            try:
                                raw = raw.encode("utf-8")
                            except UnicodeError:
                                continue
                        if isinstance(raw, bytes) and len(raw) <= MAX_FRAME_BYTES + 2048:
                            overflow = False
                            with self._lock:
                                if len(self._incoming) < 64 and self._incoming_bytes + len(raw) <= 1024 * 1024:
                                    self._incoming.append(raw)
                                    self._incoming_bytes += len(raw)
                                else:
                                    overflow = True
                            if overflow:
                                break
            except (RedisError, OSError):
                pass
            finally:
                with self._lock:
                    if self._ready.is_set():
                        self._generation += 1
                    self._ready.clear()
                    self._incoming.clear()
                    self._incoming_bytes = 0
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except (RedisError, OSError):
                        pass
            self._stopping.wait(1)

    def _deliver(self, raw):
        try:
            message = strict_json(raw)
            if (not isinstance(message, dict) or set(message) != {"v", "serverId", "userId", "targetPeerId", "targetConnectionId", "type", "payload"}
                    or type(message["v"]) is not int or message["v"] != 1 or message["serverId"] != self.server_id
                    or message["type"] not in {"peer.request", "peer.authorized", "peer.signal", "peer.cancelled"}
                    or not token(message["targetConnectionId"]) or not token(message["targetPeerId"]) or not isinstance(message["payload"], dict)):
                return
            connection = self._connections.get(message["targetConnectionId"])
            if (not connection or not connection.actor or connection.generation != self._generation
                    or connection.actor.peer_id != message["targetPeerId"] or connection.subscription.user_id != message["userId"]):
                return
            payload = message["payload"]
            if not token(payload.get("transferId")):
                return
            if message["type"] == "peer.cancelled":
                if set(payload) != {"transferId", "reason"} or not token(payload["reason"]) or payload["transferId"] not in connection.transfers:
                    return
                self._emit(connection, "peer.cancelled", **payload)
            else:
                record = self._authorize_transfer(connection.actor, payload["transferId"])
                kind = message["type"]
                if kind == "peer.request":
                    if (set(payload) != {"transferId", "requesterPeerId", "media", "leaseMs"}
                            or record["state"] != "pending" or connection.actor.peer_id != record["source"]["peerId"]
                            or payload["requesterPeerId"] != record["requester"]["peerId"] or payload["media"] != record["media"]):
                        return
                elif kind == "peer.authorized":
                    if (set(payload) != {"transferId", "media", "sourcePeerId", "requesterPeerId", "offererPeerId", "leaseMs"}
                            or record["state"] != "active" or payload["media"] != record["media"]
                            or payload["sourcePeerId"] != record["source"]["peerId"]
                            or payload["requesterPeerId"] != record["requester"]["peerId"]
                            or payload["offererPeerId"] != record["requester"]["peerId"]):
                        return
                elif kind == "peer.signal":
                    other = "source" if connection.actor.peer_id == record["requester"]["peerId"] else "requester"
                    if (set(payload) != {"transferId", "fromPeerId", "kind", "payload"} or record["state"] != "active"
                            or payload["fromPeerId"] != record[other]["peerId"]):
                        return
                    parse_message(json.dumps({"v": 1, "type": "peer.signal", "requestId": "internal",
                                              "transferId": payload["transferId"], "kind": payload["kind"], "payload": payload["payload"]}))
                    if (payload["kind"] == "offer" and other != "requester"
                            or payload["kind"] == "answer" and other != "source"):
                        return
                if "leaseMs" in payload:
                    if type(payload["leaseMs"]) is not int or payload["leaseMs"] <= 0:
                        return
                    payload = {**payload, "leaseMs": min(payload["leaseMs"], record["leaseMs"])}
                self._emit(connection, message["type"], transfer=record, **payload)
        except (ValueError, TypeError, KeyError, RecursionError, PeerProtocolError, RegistryError, AuthError, StoreError, RedisError, OSError, sqlite3.Error):
            return

    def _maintain(self, dirty, periodic):
        for connection in tuple(self._connections.values()):
            if self._stopping.is_set():
                return
            if connection.closed:
                continue
            if connection.actor is None:
                continue
            if not self._ready.is_set() or connection.generation != self._generation:
                self._disable(connection)
                continue
            if not periodic and connection.subscription.user_id not in dirty:
                continue
            try:
                self._session(connection.subscription.user_id, connection.subscription.session_key)
                self.registry.get_peer(connection.subscription.user_id, connection.actor.peer_id)
            except (RegistryError, AuthError, RedisError, OSError, sqlite3.Error):
                self._disable(connection, "registration_expired")
                continue
            for record in tuple(connection.transfers.values()):
                try:
                    self._authorize_transfer(connection.actor, record["transferId"])
                except (RegistryError, AuthError, StoreError, RedisError, OSError, sqlite3.Error):
                    self._cancel(connection, record, "expired")

    async def _pump(self):
        next_check = time.monotonic() + 1
        while not self._stopping.is_set():
            with self._lock:
                incoming = [self._incoming.popleft() for _ in range(min(16, len(self._incoming)))]
                self._incoming_bytes -= sum(len(raw) for raw in incoming)
                dirty, self._dirty = self._dirty, set()
            for raw in incoming:
                await self._run(self._deliver, raw)
            periodic = time.monotonic() >= next_check
            if periodic or dirty:
                await self._run(self._maintain, dirty, periodic)
                if periodic:
                    next_check = time.monotonic() + 1
            await asyncio.sleep(0.05)

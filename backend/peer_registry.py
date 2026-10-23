"""Disposable, atomically fenced P2P presence. SQL authorization belongs to PeerService."""
from dataclasses import dataclass
import hashlib
import json
import re
import uuid

from redis.exceptions import RedisError, WatchError

PEER_MS = 45_000
STAGING_MS = 10_000
REPLAY_MS = 10_000
PENDING_MS = 5_000
LEASE_MS = 10_000
HARD_MS = 90_000
MAX_PEERS = 8
MAX_TRANSFERS = 8
MAX_PEER_TRANSFERS = 2
MAX_PROCESS_TRANSFERS = 32
MAX_ASSETS = 1024
MAX_BATCHES = 32
MAX_BATCH_ASSETS = 32
MAX_STATE_BYTES = 128 * 1024
MAX_MANIFEST_BYTES = 80 * 1024
MAX_SIGNAL_BYTES = 512 * 1024
MAX_CANDIDATES = 128
HASH = re.compile(r"^[a-f0-9]{64}$")


class RegistryError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PeerActor:
    user_id: str
    peer_id: str
    session_key: str
    process_id: str
    connection_id: str


def _text(value, maximum=128):
    if not isinstance(value, str) or not value or len(value) > maximum or "\0" in value:
        raise RegistryError("invalid")
    return value


def _integer(value, minimum=0, maximum=2**53 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise RegistryError("invalid")
    return value


def _hash(value):
    if not isinstance(value, str) or not HASH.fullmatch(value):
        raise RegistryError("invalid")
    return value


def _name(value):
    if not isinstance(value, str) or len(value) > 32 or "\0" in value:
        raise RegistryError("invalid")
    return value


def _assets(value, maximum=MAX_BATCH_ASSETS):
    if not isinstance(value, list) or len(value) > maximum:
        raise RegistryError("invalid")
    if any(not isinstance(item, str) or not HASH.fullmatch(item) for item in value) or len(set(value)) != len(value):
        raise RegistryError("invalid")
    return value


def _encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _decode(raw, maximum):
    if not isinstance(raw, (bytes, str)) or len(raw) > maximum:
        raise RegistryError("unavailable")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result
    try:
        invalid_number = lambda _: (_ for _ in ()).throw(ValueError())
        value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_number, parse_float=invalid_number)
        if len(_encode(value).encode("utf-8")) > maximum:
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise RegistryError("unavailable") from None


def _fingerprint(kind, value):
    return hashlib.sha256(_encode([kind, value]).encode()).hexdigest()


def _ack(value, revision, complete):
    if (not isinstance(value, dict) or set(value) != {"revision", "count", "complete", "nextBatchIndex"}
            or value["revision"] != revision or value["complete"] is not complete):
        raise RegistryError("invalid")
    _integer(value["count"], maximum=MAX_ASSETS)
    if complete:
        if value["nextBatchIndex"] is not None:
            raise RegistryError("invalid")
    else:
        _integer(value["nextBatchIndex"], 1, MAX_BATCHES - 1)


def _identity(peer):
    return {key: peer[key] for key in ("peerId", "sessionKey", "processId", "connectionId")}


class PeerRegistry:
    """No worker, local peer fallback or ownership of the borrowed Redis client."""

    def __init__(self, client, server_id, storage_identity):
        self.client = client
        _text(server_id, 512)
        _text(storage_identity, 4096)
        self.scope = hashlib.sha256(_encode([server_id, storage_identity]).encode()).hexdigest()
        self.prefix = "memoverse:peers:v1:" + self.scope + ":"

    def _account_key(self, user_id):
        return self.prefix + "account:" + hashlib.sha256(_text(user_id, 512).encode()).hexdigest()

    def _process_key(self, process_id):
        return self.prefix + "process:" + hashlib.sha256(_text(process_id).encode()).hexdigest()

    @staticmethod
    def _peer_key(account, peer_id):
        return account + ":peer:" + peer_id

    @staticmethod
    def _manifest_key(account, peer_id):
        return account + ":manifest:" + peer_id

    @staticmethod
    def _stage_key(account, peer_id):
        return account + ":staging:" + peer_id

    @staticmethod
    def _transfer_key(account, transfer_id):
        return account + ":transfer:" + transfer_id

    def _state(self, raw, user_id):
        if raw is None:
            return {"scope": self.scope, "userId": user_id, "peers": {}, "transfers": {}}
        state = _decode(raw, MAX_STATE_BYTES)
        try:
            if (not isinstance(state, dict) or state["scope"] != self.scope or state["userId"] != user_id
                    or not isinstance(state["peers"], dict) or len(state["peers"]) > MAX_PEERS
                    or not isinstance(state["transfers"], dict) or len(state["transfers"]) > MAX_TRANSFERS):
                raise ValueError()
            for identifier, peer in state["peers"].items():
                if peer["peerId"] != identifier or peer["userId"] != user_id:
                    raise ValueError()
                for key in ("peerId", "sessionKey", "processId", "connectionId", "processEpoch"):
                    _text(peer[key])
                _name(peer["name"])
                _integer(peer["expiresAt"])
                _integer(peer["manifestRevision"])
                _integer(peer["cacheCount"], maximum=MAX_ASSETS)
                stage = peer.get("staging")
                if stage is not None:
                    if not isinstance(stage, dict) or stage["revision"] != peer["manifestRevision"] + 1:
                        raise ValueError()
                    _integer(stage["batchCount"], 2, MAX_BATCHES)
                    _integer(stage["nextBatchIndex"], 1, stage["batchCount"] - 1)
                    _integer(stage["count"], 1, min(MAX_ASSETS, stage["nextBatchIndex"] * MAX_BATCH_ASSETS))
                    _integer(stage["expiresAt"])
                    _hash(stage["lastFingerprint"])
                    _ack(stage["lastAck"], stage["revision"], False)
                    if stage["lastAck"]["count"] != stage["count"] or stage["lastAck"]["nextBatchIndex"] != stage["nextBatchIndex"]:
                        raise ValueError()
                replay = peer.get("replay")
                if replay is not None:
                    if not isinstance(replay, dict):
                        raise ValueError()
                    _integer(replay["expiresAt"])
                    _hash(replay["fingerprint"])
                    _ack(replay["ack"], peer["manifestRevision"], True)
            for identifier, transfer in state["transfers"].items():
                if transfer["transferId"] != identifier or transfer["userId"] != user_id or transfer["state"] not in ("pending", "active"):
                    raise ValueError()
                _text(identifier)
                _hash(transfer["assetId"])
                self._media(transfer["assetId"], transfer["media"])
                for role in ("requester", "source"):
                    if set(transfer[role]) != {"peerId", "sessionKey", "processId", "connectionId"}:
                        raise ValueError()
                    for key in ("peerId", "sessionKey", "processId", "connectionId"):
                        _text(transfer[role][key])
                    _integer(transfer[role + "Candidates"], maximum=MAX_CANDIDATES)
                for key in ("createdAt", "hardExpiresAt", "expiresAt", "requesterExpiresAt", "sourceExpiresAt"):
                    _integer(transfer[key])
                if type(transfer["offerSent"]) is not bool or type(transfer["answerSent"]) is not bool:
                    raise ValueError()
                if transfer["answerSent"] and not transfer["offerSent"]:
                    raise ValueError()
                if transfer["requesterCandidates"] + transfer["sourceCandidates"] > MAX_CANDIDATES:
                    raise ValueError()
                _integer(transfer["signalBytes"], maximum=MAX_SIGNAL_BYTES)
        except (KeyError, TypeError, ValueError, RegistryError):
            raise RegistryError("unavailable") from None
        return state

    @staticmethod
    def _media(asset_id, media):
        if (not isinstance(media, dict) or set(media) != {"assetId", "sha256", "size", "mimeType"}
                or media["assetId"] != asset_id or media["sha256"] != asset_id
                or media["mimeType"] not in ("image/png", "image/jpeg", "image/webp")):
            raise RegistryError("invalid")
        _integer(media["size"], 1, 12 * 1024 * 1024)
        return media

    @staticmethod
    def _check_actor(actor):
        if not isinstance(actor, PeerActor):
            raise RegistryError("invalid")
        _text(actor.user_id, 512)
        for value in (actor.peer_id, actor.session_key, actor.process_id, actor.connection_id):
            _text(value)

    @staticmethod
    def _peer_view(peer, now):
        return {key: peer[key] for key in ("peerId", "userId", "sessionKey", "processId", "connectionId",
                                          "name", "expiresAt", "manifestRevision", "cacheCount")} | {
            "leaseMs": max(0, peer["expiresAt"] - now)}

    @staticmethod
    def _transfer_view(transfer, now):
        return json.loads(_encode(transfer)) | {"leaseMs": max(0, transfer["expiresAt"] - now)}

    def _mutate(self, user_id, action, *, processes=(), manifests=()):
        """WATCH leases too: expiry between validation and EXEC is a conflict, not a renewed grant."""
        if self.client is None:
            raise RegistryError("unavailable")
        account = self._account_key(user_id)
        for attempt in range(3):
            try:
                with self.client.pipeline() as pipe:
                    pipe.watch(account)
                    state = self._state(pipe.get(account), user_id)
                    original_state = _encode(state).encode()
                    old_peers = set(state["peers"])
                    old_transfers = set(state["transfers"])
                    peer_expiries = {identifier: peer["expiresAt"] for identifier, peer in state["peers"].items()}
                    transfer_expiries = {identifier: transfer["expiresAt"] for identifier, transfer in state["transfers"].items()}
                    process_ids = set(processes)
                    process_ids.update(peer["processId"] for peer in state["peers"].values())
                    for transfer in state["transfers"].values():
                        process_ids.update(transfer[role]["processId"] for role in ("requester", "source"))
                    process_keys = {identifier: self._process_key(identifier) for identifier in process_ids}
                    lease_keys = {("peer", identifier): self._peer_key(account, identifier) for identifier in old_peers}
                    lease_keys.update({("transfer", identifier): self._transfer_key(account, identifier) for identifier in old_transfers})
                    published_keys = {identifier: self._manifest_key(account, identifier) for identifier in old_peers}
                    stage_keys = {identifier: self._stage_key(account, identifier) for identifier, peer in state["peers"].items() if peer.get("staging")}
                    watched = [*lease_keys.values(), *published_keys.values(), *stage_keys.values(), *process_keys.values()]
                    if watched:
                        pipe.watch(*watched)
                    seconds, micros = pipe.time()
                    now = int(seconds) * 1000 + int(micros) // 1000
                    leases = dict(zip(lease_keys, pipe.mget(list(lease_keys.values())))) if lease_keys else {}
                    missing_published = set()
                    if published_keys and pipe.exists(*published_keys.values()) != len(published_keys):
                        missing_published = {identifier for identifier, key in published_keys.items() if not pipe.exists(key)}
                    missing_stage = set()
                    if stage_keys and pipe.exists(*stage_keys.values()) != len(stage_keys):
                        missing_stage = {identifier for identifier, key in stage_keys.items() if not pipe.exists(key)}
                    quotas, process_states, original_processes = {}, {}, {}
                    quota_raws = pipe.mget(list(process_keys.values())) if process_keys else []
                    for identifier, raw in zip(process_keys, quota_raws):
                        process = _decode(raw, 16 * 1024) if raw is not None else {
                            "epoch": uuid.uuid4().hex, "expiresAt": now + PEER_MS, "transfers": {}}
                        if (not isinstance(process, dict) or not isinstance(process.get("epoch"), str)
                                or not re.fullmatch(r"[a-f0-9]{32}", process["epoch"])
                                or type(process.get("expiresAt")) is not int
                                or not isinstance(process.get("transfers"), dict)
                                or len(process["transfers"]) > MAX_PROCESS_TRANSFERS
                                or any(not isinstance(key, str) or type(expiry) is not int
                                       for key, expiry in process["transfers"].items())):
                            raise RegistryError("unavailable")
                        original_processes[identifier] = _encode(process) if raw is not None else None
                        process["transfers"] = {key: expiry for key, expiry in process["transfers"].items() if expiry > now}
                        process_states[identifier] = process
                        quotas[identifier] = process["transfers"]
                    ctx = {"state": state, "now": now, "quotas": quotas, "published": {}, "staged": {},
                           "processes": process_states, "manifestWrites": {}, "stageWrites": {},
                           "expiredPeers": set(), "expiredTransfers": set()}
                    for identifier, peer in list(state["peers"].items()):
                        if (peer["expiresAt"] <= now or leases[("peer", identifier)] is None or identifier in missing_published
                                or peer["processEpoch"] != process_states[peer["processId"]]["epoch"]):
                            ctx["expiredPeers"].add(identifier)
                            del state["peers"][identifier]
                            continue
                        stage = peer.get("staging")
                        if stage and (stage["expiresAt"] <= now or identifier in missing_stage):
                            peer["staging"] = None
                    for identifier, transfer in list(state["transfers"].items()):
                        if (transfer["expiresAt"] <= now or transfer["hardExpiresAt"] <= now
                                or leases[("transfer", identifier)] is None
                                or any(transfer[role] != _identity(state["peers"].get(transfer[role]["peerId"], {}))
                                       if transfer[role]["peerId"] in state["peers"] else True
                                       for role in ("requester", "source"))):
                            ctx["expiredTransfers"].add(identifier)
                            self._remove_transfer(ctx, identifier)
                        elif any(quotas[transfer[role]["processId"]].get(identifier) != transfer["expiresAt"]
                                 for role in ("requester", "source")):
                            raise RegistryError("unavailable")
                    read_ids = set(state["peers"]) if manifests == "all" else set(manifests)
                    for identifier in read_ids & set(state["peers"]):
                        raw = pipe.get(self._manifest_key(account, identifier))
                        published = _decode(raw, MAX_MANIFEST_BYTES)
                        peer = state["peers"][identifier]
                        if (not isinstance(published, dict) or published.get("revision") != peer["manifestRevision"]
                                or not isinstance(published.get("assetIds"), list)):
                            raise RegistryError("unavailable")
                        try:
                            _assets(published["assetIds"], MAX_ASSETS)
                        except RegistryError:
                            raise RegistryError("unavailable") from None
                        if len(published["assetIds"]) != peer["cacheCount"]:
                            raise RegistryError("unavailable")
                        ctx["published"][identifier] = published
                        if peer.get("staging"):
                            staged = _decode(pipe.get(self._stage_key(account, identifier)), MAX_MANIFEST_BYTES)
                            try:
                                _assets(staged, MAX_ASSETS)
                            except RegistryError:
                                raise RegistryError("unavailable") from None
                            if len(staged) != peer["staging"]["count"]:
                                raise RegistryError("unavailable")
                            ctx["staged"][identifier] = staged
                    result = action(ctx)
                    encoded = _encode(state).encode()
                    if len(encoded) > MAX_STATE_BYTES:
                        raise RegistryError("capacity")
                    pipe.multi()
                    if encoded != original_state:
                        pipe.set(account, encoded, px=HARD_MS)
                    for identifier in old_peers - set(state["peers"]):
                        pipe.delete(self._peer_key(account, identifier), self._manifest_key(account, identifier), self._stage_key(account, identifier))
                    for identifier, peer in state["peers"].items():
                        expiry_changed = peer_expiries.get(identifier) != peer["expiresAt"]
                        if expiry_changed:
                            pipe.set(self._peer_key(account, identifier), identifier, pxat=peer["expiresAt"])
                        if identifier in ctx["manifestWrites"]:
                            pipe.set(self._manifest_key(account, identifier), _encode(ctx["manifestWrites"][identifier]), pxat=peer["expiresAt"])
                        elif expiry_changed:
                            pipe.pexpireat(self._manifest_key(account, identifier), peer["expiresAt"])
                        stage = peer.get("staging")
                        if not stage and identifier in stage_keys:
                            pipe.delete(self._stage_key(account, identifier))
                        elif identifier in ctx["stageWrites"]:
                            pipe.set(self._stage_key(account, identifier), _encode(ctx["stageWrites"][identifier]), pxat=stage["expiresAt"])
                    for identifier in old_transfers - set(state["transfers"]):
                        pipe.delete(self._transfer_key(account, identifier))
                    for identifier, transfer in state["transfers"].items():
                        if transfer_expiries.get(identifier) != transfer["expiresAt"]:
                            pipe.set(self._transfer_key(account, identifier), identifier, pxat=transfer["expiresAt"])
                    for peer in state["peers"].values():
                        process = process_states[peer["processId"]]
                        process["expiresAt"] = max(process["expiresAt"], peer["expiresAt"])
                    for identifier, process in process_states.items():
                        encoded_process = _encode(process)
                        if encoded_process == original_processes[identifier]:
                            continue
                        key = process_keys[identifier]
                        pipe.set(key, encoded_process, pxat=process["expiresAt"])
                    pipe.execute()
                    return result
            except WatchError:
                continue
            except (RedisError, OSError):
                raise RegistryError("unavailable") from None
        raise RegistryError("conflict")

    @staticmethod
    def _remove_transfer(ctx, transfer_id):
        transfer = ctx["state"]["transfers"].pop(transfer_id)
        for process in {transfer[role]["processId"] for role in ("requester", "source")}:
            ctx["quotas"][process].pop(transfer_id, None)
        return transfer

    @staticmethod
    def _owned(ctx, actor):
        peer = ctx["state"]["peers"].get(actor.peer_id)
        if peer is None:
            raise RegistryError("expired" if actor.peer_id in ctx["expiredPeers"] else "not_found")
        if (peer["sessionKey"], peer["processId"], peer["connectionId"]) != (actor.session_key, actor.process_id, actor.connection_id):
            raise RegistryError("not_found")
        return peer

    def register(self, user_id, session_key, process_id, connection_id, name):
        for value in (session_key, process_id, connection_id):
            _text(value)
        _name(name)
        def action(ctx):
            peers, now = ctx["state"]["peers"], ctx["now"]
            for peer in peers.values():
                if (peer["processId"], peer["connectionId"]) == (process_id, connection_id):
                    if peer["sessionKey"] != session_key:
                        raise RegistryError("conflict")
                    return self._peer_view(peer, now)
            if len(peers) >= MAX_PEERS:
                raise RegistryError("capacity")
            identifier = str(uuid.uuid4())
            peer = {"peerId": identifier, "userId": user_id, "sessionKey": session_key, "processId": process_id,
                    "connectionId": connection_id, "name": name, "expiresAt": now + PEER_MS,
                    "processEpoch": ctx["processes"][process_id]["epoch"],
                    "manifestRevision": 0, "cacheCount": 0, "staging": None, "replay": None}
            peers[identifier] = peer
            ctx["manifestWrites"][identifier] = {"revision": 0, "assetIds": []}
            return self._peer_view(peer, now)
        return self._mutate(user_id, action, processes=(process_id,))

    def touch(self, actor):
        self._check_actor(actor)
        def action(ctx):
            peer = self._owned(ctx, actor)
            peer["expiresAt"] = ctx["now"] + PEER_MS
            return self._peer_view(peer, ctx["now"])
        return self._mutate(actor.user_id, action)

    def get_peer(self, user_id, peer_id):
        _text(peer_id)
        def action(ctx):
            peer = ctx["state"]["peers"].get(peer_id)
            if peer is None:
                raise RegistryError("not_found")
            return self._peer_view(peer, ctx["now"])
        return self._mutate(user_id, action)

    def list_peers(self, user_id):
        return self._mutate(user_id, lambda ctx: [self._peer_view(peer, ctx["now"]) for peer in ctx["state"]["peers"].values()])

    def unregister(self, actor):
        self._check_actor(actor)
        def action(ctx):
            if actor.peer_id not in ctx["state"]["peers"]:
                return []
            self._owned(ctx, actor)
            cancelled = []
            for identifier, transfer in list(ctx["state"]["transfers"].items()):
                if any(transfer[role]["peerId"] == actor.peer_id for role in ("requester", "source")):
                    removed = self._remove_transfer(ctx, identifier)
                    cancelled.append(self._transfer_view(removed, ctx["now"]) | {"state": "cancelled", "reason": "disconnected", "leaseMs": 0})
            del ctx["state"]["peers"][actor.peer_id]
            return cancelled
        return self._mutate(actor.user_id, action)

    @staticmethod
    def _replay(peer, fingerprint, now):
        replay = peer.get("replay")
        return replay["ack"] if replay and replay["expiresAt"] > now and replay["fingerprint"] == fingerprint else None

    def replace_manifest(self, actor, revision, batch_index, batch_count, asset_ids):
        self._check_actor(actor)
        _integer(revision, 1)
        _integer(batch_count, 1, MAX_BATCHES)
        _integer(batch_index, 0, batch_count - 1)
        _assets(asset_ids)
        if not asset_ids and (batch_count != 1 or batch_index != 0):
            raise RegistryError("invalid")
        fingerprint = _fingerprint("replace", [revision, batch_index, batch_count, asset_ids])
        def action(ctx):
            peer, now = self._owned(ctx, actor), ctx["now"]
            replay = self._replay(peer, fingerprint, now)
            if replay is not None:
                return replay
            if revision != peer["manifestRevision"] + 1:
                raise RegistryError("conflict")
            stage = peer.get("staging")
            if stage:
                if stage["revision"] != revision or stage["batchCount"] != batch_count:
                    raise RegistryError("conflict")
                if stage["lastFingerprint"] == fingerprint:
                    return stage["lastAck"]
                if batch_index != stage["nextBatchIndex"]:
                    raise RegistryError("conflict")
                assets = ctx["staged"][actor.peer_id] + asset_ids
            else:
                if batch_index != 0:
                    raise RegistryError("conflict")
                stage = {"revision": revision, "batchCount": batch_count, "expiresAt": now + STAGING_MS}
                assets = list(asset_ids)
            _assets(assets, MAX_ASSETS)
            complete = batch_index + 1 == batch_count
            ack = {"revision": revision, "count": len(assets), "complete": complete,
                   "nextBatchIndex": None if complete else batch_index + 1}
            if complete:
                peer["manifestRevision"], peer["cacheCount"] = revision, len(assets)
                peer["staging"] = None
                peer["replay"] = {"fingerprint": fingerprint, "ack": ack, "expiresAt": now + REPLAY_MS}
                ctx["manifestWrites"][actor.peer_id] = {"revision": revision, "assetIds": sorted(assets)}
            else:
                stage.update(count=len(assets), nextBatchIndex=batch_index + 1, lastFingerprint=fingerprint, lastAck=ack)
                peer["staging"] = stage
                ctx["stageWrites"][actor.peer_id] = assets
            return ack
        return self._mutate(actor.user_id, action, manifests=(actor.peer_id,))

    def staged_assets(self, actor, revision):
        """Service rechecks the entire pending list in SQL before final publication."""
        self._check_actor(actor)
        _integer(revision, 1)
        def action(ctx):
            peer = self._owned(ctx, actor)
            stage = peer.get("staging")
            if not stage:
                return []
            if stage["revision"] != revision:
                raise RegistryError("conflict")
            return list(ctx["staged"][actor.peer_id])
        return self._mutate(actor.user_id, action, manifests=(actor.peer_id,))

    def patch_manifest(self, actor, revision, add, remove):
        self._check_actor(actor)
        _integer(revision, 1)
        _assets(add)
        _assets(remove)
        if len(add) + len(remove) > MAX_BATCH_ASSETS or set(add) & set(remove):
            raise RegistryError("invalid")
        fingerprint = _fingerprint("patch", [revision, add, remove])
        def action(ctx):
            peer, now = self._owned(ctx, actor), ctx["now"]
            replay = self._replay(peer, fingerprint, now)
            if replay is not None:
                return replay
            if revision != peer["manifestRevision"] + 1 or peer.get("staging"):
                raise RegistryError("conflict")
            assets = (set(ctx["published"][actor.peer_id]["assetIds"]) - set(remove)) | set(add)
            if len(assets) > MAX_ASSETS:
                raise RegistryError("capacity")
            ack = {"revision": revision, "count": len(assets), "complete": True, "nextBatchIndex": None}
            peer["manifestRevision"], peer["cacheCount"] = revision, len(assets)
            peer["replay"] = {"fingerprint": fingerprint, "ack": ack, "expiresAt": now + REPLAY_MS}
            ctx["manifestWrites"][actor.peer_id] = {"revision": revision, "assetIds": sorted(assets)}
            return ack
        return self._mutate(actor.user_id, action, manifests=(actor.peer_id,))

    def find_sources(self, actor, asset_id):
        self._check_actor(actor)
        _hash(asset_id)
        def action(ctx):
            self._owned(ctx, actor)
            return [self._peer_view(peer, ctx["now"]) for identifier, peer in ctx["state"]["peers"].items()
                    if identifier != actor.peer_id and asset_id in ctx["published"][identifier]["assetIds"]]
        return self._mutate(actor.user_id, action, manifests="all")

    def _transfer(self, ctx, actor, transfer_id):
        self._owned(ctx, actor)
        transfer = ctx["state"]["transfers"].get(transfer_id)
        if transfer is None:
            raise RegistryError("expired" if transfer_id in ctx["expiredTransfers"] else "not_found")
        if not any(transfer[role] == {"peerId": actor.peer_id, "sessionKey": actor.session_key,
                                      "processId": actor.process_id, "connectionId": actor.connection_id}
                   for role in ("requester", "source")):
            raise RegistryError("not_found")
        return transfer

    @staticmethod
    def _set_transfer_expiry(ctx, transfer):
        peers = ctx["state"]["peers"]
        transfer["expiresAt"] = min(transfer["hardExpiresAt"], transfer["requesterExpiresAt"], transfer["sourceExpiresAt"],
                                    peers[transfer["requester"]["peerId"]]["expiresAt"], peers[transfer["source"]["peerId"]]["expiresAt"])
        for process in {transfer[role]["processId"] for role in ("requester", "source")}:
            ctx["quotas"][process][transfer["transferId"]] = transfer["expiresAt"]

    def open_transfer(self, actor, source_peer_id, asset_id, media):
        self._check_actor(actor)
        _text(source_peer_id)
        _hash(asset_id)
        self._media(asset_id, media)
        if source_peer_id == actor.peer_id:
            raise RegistryError("invalid")
        def action(ctx):
            requester = self._owned(ctx, actor)
            source = ctx["state"]["peers"].get(source_peer_id)
            if source is None or asset_id not in ctx["published"].get(source_peer_id, {}).get("assetIds", []):
                raise RegistryError("not_found")
            transfers, now = ctx["state"]["transfers"], ctx["now"]
            for transfer in transfers.values():
                if transfer["requester"]["peerId"] == actor.peer_id and transfer["assetId"] == asset_id:
                    if transfer["source"]["peerId"] != source_peer_id or transfer["media"] != media:
                        raise RegistryError("conflict")
                    return self._transfer_view(transfer, now)
            if len(transfers) >= MAX_TRANSFERS:
                raise RegistryError("capacity")
            for peer_id in (actor.peer_id, source_peer_id):
                if sum(any(transfer[role]["peerId"] == peer_id for role in ("requester", "source"))
                       for transfer in transfers.values()) >= MAX_PEER_TRANSFERS:
                    raise RegistryError("capacity")
            for process in {requester["processId"], source["processId"]}:
                if len(ctx["quotas"][process]) >= MAX_PROCESS_TRANSFERS:
                    raise RegistryError("capacity")
            identifier = str(uuid.uuid4())
            transfer = {"transferId": identifier, "userId": actor.user_id, "assetId": asset_id, "media": dict(media),
                        "requester": _identity(requester), "source": _identity(source), "state": "pending",
                        "createdAt": now, "hardExpiresAt": now + HARD_MS, "expiresAt": now + PENDING_MS,
                        "requesterExpiresAt": now + PENDING_MS, "sourceExpiresAt": now + PENDING_MS,
                        "offerSent": False, "answerSent": False, "requesterCandidates": 0, "sourceCandidates": 0,
                        "signalBytes": 0}
            transfers[identifier] = transfer
            self._set_transfer_expiry(ctx, transfer)
            return self._transfer_view(transfer, now)
        return self._mutate(actor.user_id, action, manifests=(source_peer_id,))

    def accept_transfer(self, actor, transfer_id):
        self._check_actor(actor)
        _text(transfer_id)
        def action(ctx):
            transfer = self._transfer(ctx, actor, transfer_id)
            if transfer["source"]["peerId"] != actor.peer_id:
                raise RegistryError("conflict")
            if transfer["state"] == "pending":
                transfer["state"] = "active"
                transfer["requesterExpiresAt"] = transfer["sourceExpiresAt"] = ctx["now"] + LEASE_MS
                self._set_transfer_expiry(ctx, transfer)
            return self._transfer_view(transfer, ctx["now"])
        return self._mutate(actor.user_id, action)

    def get_transfer(self, actor, transfer_id):
        self._check_actor(actor)
        _text(transfer_id)
        return self._mutate(actor.user_id, lambda ctx: self._transfer_view(self._transfer(ctx, actor, transfer_id), ctx["now"]))

    def renew_transfer(self, actor, transfer_id):
        self._check_actor(actor)
        _text(transfer_id)
        def action(ctx):
            transfer = self._transfer(ctx, actor, transfer_id)
            if transfer["state"] != "active":
                raise RegistryError("conflict")
            role = "requester" if transfer["requester"]["peerId"] == actor.peer_id else "source"
            transfer[role + "ExpiresAt"] = ctx["now"] + LEASE_MS
            self._set_transfer_expiry(ctx, transfer)
            return self._transfer_view(transfer, ctx["now"])
        return self._mutate(actor.user_id, action)

    def signal_transfer(self, actor, transfer_id, kind, payload_bytes):
        self._check_actor(actor)
        _text(transfer_id)
        # PeerService bounds SDP text itself to 48 KiB and the outer WS frame to
        # 64 KiB. This count includes JSON escaping/envelope, not only SDP text.
        _integer(payload_bytes, 1, 64 * 1024 if kind in ("offer", "answer") else 2 * 1024)
        if kind not in ("offer", "answer", "ice"):
            raise RegistryError("invalid")
        def action(ctx):
            transfer = self._transfer(ctx, actor, transfer_id)
            role = "requester" if transfer["requester"]["peerId"] == actor.peer_id else "source"
            if transfer["state"] != "active":
                raise RegistryError("conflict")
            if kind == "offer":
                if role != "requester" or transfer["offerSent"]:
                    raise RegistryError("conflict")
                transfer["offerSent"] = True
            elif kind == "answer":
                if role != "source" or not transfer["offerSent"] or transfer["answerSent"]:
                    raise RegistryError("conflict")
                transfer["answerSent"] = True
            else:
                if not transfer["offerSent"] or transfer["requesterCandidates"] + transfer["sourceCandidates"] >= MAX_CANDIDATES:
                    raise RegistryError("conflict")
                transfer[role + "Candidates"] += 1
            if transfer["signalBytes"] + payload_bytes > MAX_SIGNAL_BYTES:
                raise RegistryError("capacity")
            transfer["signalBytes"] += payload_bytes
            return self._transfer_view(transfer, ctx["now"])
        return self._mutate(actor.user_id, action)

    def cancel_transfer(self, actor, transfer_id, reason):
        self._check_actor(actor)
        _text(transfer_id)
        _text(reason, 80)
        def action(ctx):
            self._transfer(ctx, actor, transfer_id)
            removed = self._remove_transfer(ctx, transfer_id)
            return self._transfer_view(removed, ctx["now"]) | {"state": "cancelled", "reason": reason, "leaseMs": 0}
        return self._mutate(actor.user_id, action)

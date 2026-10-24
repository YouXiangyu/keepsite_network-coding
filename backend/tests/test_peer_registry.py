"""Disposable Redis presence, manifests, endpoint fencing and atomic capacity."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
import time
import uuid

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError, WatchError

from backend.peer_registry import PeerActor, PeerRegistry, RegistryError, LEASE_MS, MAX_PROCESS_TRANSFERS
from backend.shared_state import RedisSharedState


def asset(number=0):
    return hashlib.sha256(str(number).encode()).hexdigest()


def media(identifier):
    return {"assetId": identifier, "sha256": identifier, "size": 1234, "mimeType": "image/png"}


def actor(registry, user="alice", process="process", connection=None):
    connection = connection or uuid.uuid4().hex
    peer = registry.register(user, "session-" + user, process, connection, "页面")
    return PeerActor(user, peer["peerId"], "session-" + user, process, connection)


def publish(registry, source, identifiers, revision=1):
    batches = [identifiers[index:index + 32] for index in range(0, len(identifiers), 32)] or [[]]
    for index, batch in enumerate(batches):
        ack = registry.replace_manifest(source, revision, index, len(batches), batch)
    return ack


@pytest.fixture
def registry():
    client = fakeredis.FakeRedis()
    value = PeerRegistry(client, "test-" + uuid.uuid4().hex, "local:test-storage")
    yield value
    client.close()


def raises(code, operation):
    with pytest.raises(RegistryError) as error:
        operation()
    assert error.value.code == code


def read_state(registry, user="alice"):
    return json.loads(registry.client.get(registry._account_key(user)))


def write_state(registry, state):
    registry.client.set(registry._account_key(state["userId"]), json.dumps(state), px=90_000)


def test_peer_identity_connection_fence_namespaces_and_idempotent_disconnect(registry):
    first = actor(registry, connection="one")
    replay = registry.register("alice", first.session_key, first.process_id, "one", "renamed")
    assert replay["peerId"] == first.peer_id and replay["manifestRevision"] == 0
    assert replay["cacheCount"] == 0 and 0 < replay["leaseMs"] <= 45_000
    for forged in (replace(first, user_id="bob"), replace(first, session_key="wrong"),
                   replace(first, process_id="other-process"), replace(first, connection_id="other-connection")):
        raises("not_found", lambda: registry.touch(forged))
        assert registry.get_peer("alice", first.peer_id)["connectionId"] == "one"
    assert registry.list_peers("bob") == []
    other_space = PeerRegistry(registry.client, "other-server", "local:test-storage")
    other_store = PeerRegistry(registry.client, "test", "other-storage")
    assert other_space.list_peers("alice") == other_store.list_peers("alice") == []
    assert registry.unregister(first) == registry.unregister(first) == []
    assert registry.list_peers("alice") == [] and registry.client.ping()


def test_peer_capacity_is_atomic_across_registry_instances(registry):
    other = PeerRegistry(registry.client, "unused", "unused")
    other.prefix, other.scope = registry.prefix, registry.scope
    def register(index):
        try:
            return actor(registry if index % 2 else other, connection=str(index))
        except RegistryError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(register, range(16)))
    peers = registry.list_peers("alice")
    assert len(peers) <= 8
    assert len({peer["peerId"] for peer in peers}) == sum(isinstance(item, PeerActor) for item in result)
    assert all(isinstance(item, PeerActor) or item in ("capacity", "conflict") for item in result)
    while len(registry.list_peers("alice")) < 8:
        actor(registry)
    raises("capacity", lambda: actor(other))


def test_manifest_staging_is_invisible_and_ack_replays_do_not_extend_deadline(registry):
    requester, source = actor(registry), actor(registry)
    old, first, last = asset(0), asset(1), asset(2)
    publish(registry, source, [old])
    ack = registry.replace_manifest(source, 2, 0, 2, [first])
    assert ack == {"revision": 2, "count": 1, "complete": False, "nextBatchIndex": 1}
    stage_key = registry._stage_key(registry._account_key("alice"), source.peer_id)
    ttl = registry.client.pttl(stage_key)
    time.sleep(0.01)
    assert registry.replace_manifest(source, 2, 0, 2, [first]) == ack
    assert registry.client.pttl(stage_key) < ttl
    assert registry.staged_assets(source, 2) == [first]
    assert [peer["peerId"] for peer in registry.find_sources(requester, old)] == [source.peer_id]
    assert registry.find_sources(requester, first) == []
    raises("conflict", lambda: registry.replace_manifest(source, 2, 0, 2, [last]))
    raises("invalid", lambda: registry.replace_manifest(source, 2, 1, 2, [first]))
    assert registry.staged_assets(source, 2) == [first]
    completed = registry.replace_manifest(source, 2, 1, 2, [last])
    assert completed == {"revision": 2, "count": 2, "complete": True, "nextBatchIndex": None}
    assert registry.replace_manifest(source, 2, 1, 2, [last]) == completed
    raises("conflict", lambda: registry.replace_manifest(source, 2, 0, 2, [first]))
    assert registry.find_sources(requester, old) == []
    assert registry.find_sources(requester, first)[0]["peerId"] == source.peer_id
    assert not registry.client.exists(stage_key)


def test_expired_stage_keeps_published_and_requires_restart_at_batch_zero(registry):
    requester, source = actor(registry), actor(registry)
    publish(registry, source, [asset(0)])
    registry.replace_manifest(source, 2, 0, 2, [asset(1)])
    registry.client.delete(registry._stage_key(registry._account_key("alice"), source.peer_id))
    assert registry.staged_assets(source, 2) == []
    raises("conflict", lambda: registry.replace_manifest(source, 2, 1, 2, [asset(2)]))
    assert registry.find_sources(requester, asset(0))
    publish(registry, source, [], revision=2)
    assert not registry.find_sources(requester, asset(0))


def test_patch_is_atomic_revision_checked_and_bounded(registry):
    requester, source = actor(registry), actor(registry)
    publish(registry, source, [asset(0), asset(1)])
    ack = registry.patch_manifest(source, 2, [asset(2)], [asset(0)])
    assert ack == {"revision": 2, "count": 2, "complete": True, "nextBatchIndex": None}
    assert registry.patch_manifest(source, 2, [asset(2)], [asset(0)]) == ack
    raises("conflict", lambda: registry.patch_manifest(source, 2, [], [asset(1)]))
    raises("invalid", lambda: registry.patch_manifest(source, 3, [asset(3)] * 2, []))
    raises("invalid", lambda: registry.patch_manifest(source, 3, [asset(3)], [asset(3)]))
    assert registry.find_sources(requester, asset(1)) and not registry.find_sources(requester, asset(0))
    publish(registry, source, [asset(index) for index in range(1024)], revision=3)
    raises("capacity", lambda: registry.patch_manifest(source, 4, [asset(2000)], []))
    assert registry.get_peer("alice", source.peer_id)["cacheCount"] == 1024


def test_heartbeat_does_not_read_or_rewrite_full_manifest(registry, monkeypatch):
    source = actor(registry)
    publish(registry, source, [asset(index) for index in range(1024)])
    manifest_key = registry._manifest_key(registry._account_key("alice"), source.peer_id)
    original = registry.client.pipeline
    commands = []
    def pipeline(*args, **kwargs):
        value = original(*args, **kwargs)
        execute = value.execute_command
        def recorded(*parts, **options):
            commands.append(parts)
            return execute(*parts, **options)
        value.execute_command = recorded
        return value
    monkeypatch.setattr(registry.client, "pipeline", pipeline)
    registry.touch(source)
    assert not any(parts[0] in ("GET", "SET") and parts[1] == manifest_key for parts in commands)
    assert len(registry.client.get(registry._account_key("alice"))) < 4096


def transfer_fixture(registry):
    requester, source = actor(registry, connection="requester"), actor(registry, connection="source")
    identifier = asset()
    publish(registry, source, [identifier])
    record = registry.open_transfer(requester, source.peer_id, identifier, media(identifier))
    return requester, source, record


def test_transfer_accept_signal_roles_and_retry_deduplication(registry):
    requester, source, pending = transfer_fixture(registry)
    identifier = pending["transferId"]
    assert pending["state"] == "pending" and 0 < pending["leaseMs"] <= 5000
    assert registry.open_transfer(requester, source.peer_id, asset(), media(asset()))["transferId"] == identifier
    raises("conflict", lambda: registry.accept_transfer(requester, identifier))
    raises("conflict", lambda: registry.signal_transfer(requester, identifier, "offer", 500))
    active = registry.accept_transfer(source, identifier)
    assert active["state"] == "active" and 0 < active["leaseMs"] <= LEASE_MS
    raises("conflict", lambda: registry.signal_transfer(source, identifier, "offer", 500))
    raises("conflict", lambda: registry.signal_transfer(source, identifier, "answer", 500))
    assert registry.signal_transfer(requester, identifier, "offer", 500)["offerSent"]
    assert registry.signal_transfer(source, identifier, "answer", 500)["answerSent"]
    for endpoint in (requester, source):
        assert registry.signal_transfer(endpoint, identifier, "ice", 100)["signalBytes"] >= 1100
    raises("conflict", lambda: registry.signal_transfer(requester, identifier, "offer", 500))
    raises("invalid", lambda: registry.signal_transfer(source, identifier, "ice", 2049))
    raises("invalid", lambda: registry.signal_transfer(source, identifier, "candidate", 100))
    raises("not_found", lambda: registry.get_transfer(replace(source, connection_id="old"), identifier))
    raises("not_found", lambda: registry.get_transfer(replace(source, user_id="bob"), identifier))
    cancelled = registry.cancel_transfer(requester, identifier, "user_cancelled")
    assert cancelled["state"] == "cancelled" and cancelled["leaseMs"] == 0
    raises("not_found", lambda: registry.get_transfer(source, identifier))


def test_one_endpoint_cannot_extend_the_other_lease_or_resurrect_expired_transfer(registry):
    requester, source, pending = transfer_fixture(registry)
    identifier = pending["transferId"]
    active = registry.accept_transfer(source, identifier)
    time.sleep(0.01)
    renewed = registry.renew_transfer(requester, identifier)
    assert renewed["requesterExpiresAt"] > active["requesterExpiresAt"]
    assert renewed["sourceExpiresAt"] == active["sourceExpiresAt"] == renewed["expiresAt"]
    time.sleep(0.01)
    both = registry.renew_transfer(source, identifier)
    assert both["expiresAt"] > active["expiresAt"]
    registry.client.delete(registry._transfer_key(registry._account_key("alice"), identifier))
    raises("expired", lambda: registry.renew_transfer(source, identifier))
    assert registry.open_transfer(requester, source.peer_id, asset(), media(asset()))["transferId"] != identifier


def test_hard_deadline_remains_fixed_across_accept_and_renew(registry):
    requester, source, pending = transfer_fixture(registry)
    active = registry.accept_transfer(source, pending["transferId"])
    state = read_state(registry)
    record = state["transfers"][active["transferId"]]
    record["hardExpiresAt"] = record["expiresAt"]
    write_state(registry, state)
    registry.renew_transfer(requester, active["transferId"])
    renewed = registry.renew_transfer(source, active["transferId"])
    assert renewed["hardExpiresAt"] == renewed["expiresAt"] == active["expiresAt"]


def test_peer_quota_combines_sender_and_receiver_and_disconnect_cancels_both(registry):
    a, b, c, d = [actor(registry) for _ in range(4)]
    for endpoint in (a, b, c, d):
        publish(registry, endpoint, [asset(1), asset(2)])
    first = registry.open_transfer(a, b.peer_id, asset(1), media(asset(1)))
    second = registry.open_transfer(c, a.peer_id, asset(2), media(asset(2)))
    raises("capacity", lambda: registry.open_transfer(a, d.peer_id, asset(2), media(asset(2))))
    cancelled = registry.unregister(a)
    assert {record["transferId"] for record in cancelled} == {first["transferId"], second["transferId"]}
    assert all(record["state"] == "cancelled" for record in cancelled)
    assert registry.open_transfer(c, b.peer_id, asset(1), media(asset(1)))["state"] == "pending"


def test_partial_redis_loss_disables_old_peers_and_never_reuses_local_state(registry):
    requester, source, pending = transfer_fixture(registry)
    registry.client.delete(registry._process_key(requester.process_id))
    raises("expired", lambda: registry.touch(requester))
    raises("expired", lambda: registry.accept_transfer(source, pending["transferId"]))
    fresh = actor(registry, connection="requester")
    assert fresh.peer_id != requester.peer_id
    raises("not_found", lambda: registry.touch(source))
    registry.client.delete(registry._account_key("alice"))
    raises("not_found", lambda: registry.touch(fresh))
    assert registry.list_peers("alice") == []
    assert registry.client.ping()


def test_missing_published_manifest_cannot_advertise_or_serve(registry):
    requester, source, pending = transfer_fixture(registry)
    registry.client.delete(registry._manifest_key(registry._account_key("alice"), source.peer_id))
    assert registry.find_sources(requester, asset()) == []
    raises("not_found", lambda: registry.get_transfer(requester, pending["transferId"]))


def test_finite_watch_retries_and_network_failure_are_explicit(registry, monkeypatch):
    original = registry.client.pipeline
    attempts = []
    def pipeline(*args, **kwargs):
        value = original(*args, **kwargs)
        def conflict():
            attempts.append(True)
            raise WatchError()
        value.execute = conflict
        return value
    monkeypatch.setattr(registry.client, "pipeline", pipeline)
    raises("conflict", lambda: actor(registry))
    assert len(attempts) == 3
    monkeypatch.setattr(registry.client, "pipeline", lambda *args, **kwargs: (_ for _ in ()).throw(RedisConnectionError()))
    raises("unavailable", lambda: registry.list_peers("alice"))
    raises("unavailable", lambda: PeerRegistry(None, "server", "store").list_peers("alice"))


def test_transaction_rollback_never_publishes_partial_manifest(registry, monkeypatch):
    requester, source = actor(registry), actor(registry)
    publish(registry, source, [asset(0)])
    registry.replace_manifest(source, 2, 0, 2, [asset(1)])
    original = registry.client.pipeline
    def pipeline(*args, **kwargs):
        value = original(*args, **kwargs)
        value.execute = lambda: (_ for _ in ()).throw(WatchError())
        return value
    monkeypatch.setattr(registry.client, "pipeline", pipeline)
    raises("conflict", lambda: registry.replace_manifest(source, 2, 1, 2, [asset(2)]))
    monkeypatch.setattr(registry.client, "pipeline", original)
    assert registry.staged_assets(source, 2) == [asset(1)]
    assert registry.find_sources(requester, asset(0)) and not registry.find_sources(requester, asset(1))


@pytest.mark.parametrize("lost_key", ["peer", "transfer", "process", "manifest"])
def test_loss_between_watch_and_exec_cannot_resurrect_an_old_authorization(registry, monkeypatch, lost_key):
    requester, source, pending = transfer_fixture(registry)
    identifier = pending["transferId"]
    registry.accept_transfer(source, identifier)
    account = registry._account_key("alice")
    targets = {"peer": registry._peer_key(account, requester.peer_id),
               "transfer": registry._transfer_key(account, identifier),
               "process": registry._process_key(requester.process_id),
               "manifest": registry._manifest_key(account, requester.peer_id)}
    original = registry.client.pipeline
    injected = []
    def pipeline(*args, **kwargs):
        value = original(*args, **kwargs)
        execute = value.execute
        def race():
            if not injected:
                injected.append(True)
                registry.client.delete(targets[lost_key])
            return execute()
        value.execute = race
        return value
    monkeypatch.setattr(registry.client, "pipeline", pipeline)
    raises("expired", lambda: registry.renew_transfer(requester, identifier))
    assert injected and not registry.client.exists(targets[lost_key])


def quota_scenario(registry, process, *, concurrent=False):
    requests = []
    for number in range(5):
        user = f"quota-user-{number}"
        peers = [actor(registry, user=user, process=process) for _ in range(8)]
        for source in peers[4:]:
            publish(registry, source, [asset(1), asset(2)])
        for index in range(4):
            requests.extend([(peers[index], peers[4 + index], asset(1)),
                             (peers[index], peers[4 + (index + 1) % 4], asset(2))])
    def opening(values):
        requester, source, identifier = values
        for _ in range(20):
            try:
                pending = registry.open_transfer(requester, source.peer_id, identifier, media(identifier))
                return pending
            except RegistryError as error:
                if error.code != "conflict":
                    return error.code
        return "conflict"
    if concurrent:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(opening, requests))
        # Three WATCH retries are the production contract, not a promise of
        # scheduler fairness. Resolve legal contention only after peers stop racing.
        assert sum(isinstance(result, dict) for result in results) <= MAX_PROCESS_TRANSFERS
        for index, result in enumerate(results):
            if result == "conflict":
                results[index] = opening(requests[index])
    else:
        results = [opening(request) for request in requests]
    successes = [result for result in results if isinstance(result, dict)]
    assert len(successes) == MAX_PROCESS_TRANSFERS
    assert all(isinstance(result, dict) or result == "capacity" for result in results)
    assert all(sum(item["userId"] == f"quota-user-{number}" for item in successes) <= 8 for number in range(5))
    quota = json.loads(registry.client.get(registry._process_key(process)))
    assert len(quota["transfers"]) == MAX_PROCESS_TRANSFERS
    seconds, micros = registry.client.time()
    assert min(quota["transfers"].values()) > seconds * 1000 + micros // 1000, "Capacity check must observe simultaneous live leases"


@pytest.mark.parametrize("concurrent", [False, True])
def test_process_quota_is_shared_across_accounts_and_counts_same_process_once(registry, concurrent, monkeypatch):
    import backend.peer_registry as module
    # This test isolates simultaneous capacity, not the separately tested 5-second
    # acceptance deadline. Avoid counting naturally expired slots on slow CI hosts.
    monkeypatch.setattr(module, "PENDING_MS", 30_000)
    quota_scenario(registry, "one-process", concurrent=concurrent)


def test_signal_total_budget_and_ice_count_are_atomic(registry):
    requester, source, pending = transfer_fixture(registry)
    identifier = pending["transferId"]
    registry.accept_transfer(source, identifier)
    registry.signal_transfer(requester, identifier, "offer", 64 * 1024)
    registry.signal_transfer(source, identifier, "answer", 64 * 1024)
    for _ in range(64):
        registry.signal_transfer(requester, identifier, "ice", 2048)
    for _ in range(64):
        registry.signal_transfer(source, identifier, "ice", 2048)
    state = registry.get_transfer(source, identifier)
    assert state["signalBytes"] == 384 * 1024
    assert state["requesterCandidates"] + state["sourceCandidates"] == 128
    raises("conflict", lambda: registry.signal_transfer(source, identifier, "ice", 1))
    raises("conflict", lambda: registry.signal_transfer(requester, identifier, "ice", 1))
    after = registry.get_transfer(source, identifier)
    assert after["signalBytes"] == state["signalBytes"] and after["sourceCandidates"] == 64


def test_signal_byte_budget_failure_does_not_consume_a_candidate(registry, monkeypatch):
    import backend.peer_registry as module
    assert module.MAX_SIGNAL_BYTES == 512 * 1024
    requester, source, pending = transfer_fixture(registry)
    identifier = pending["transferId"]
    registry.accept_transfer(source, identifier)
    # Exercise the independent byte budget with a small test limit; production's
    # 512 KiB ceiling has slack beyond the stricter SDP/ICE-count constraints.
    monkeypatch.setattr(module, "MAX_SIGNAL_BYTES", 1024)
    registry.signal_transfer(requester, identifier, "offer", 512)
    registry.signal_transfer(source, identifier, "answer", 512)
    raises("capacity", lambda: registry.signal_transfer(source, identifier, "ice", 1))
    after = registry.get_transfer(source, identifier)
    assert after["signalBytes"] == 1024 and after["sourceCandidates"] == 0


def test_process_epoch_loss_fences_peers_across_other_accounts(registry):
    a = actor(registry, user="alice", process="same-process", connection="old-a")
    b = actor(registry, user="bob", process="same-process", connection="old-b")
    registry.client.delete(registry._process_key("same-process"))
    fresh = actor(registry, user="bob", process="same-process", connection="new-b")
    raises("expired", lambda: registry.touch(a))
    raises("not_found", lambda: registry.touch(b))
    assert registry.touch(fresh)["peerId"] == fresh.peer_id


@pytest.mark.parametrize("damage", ["wrong-owner", "stage-shape", "replay-shape", "signal-counter",
                                    "manifest-shape", "quota-shape", "exponent-overflow", "lone-surrogate"])
def test_corrupted_disposable_redis_records_fail_closed_with_safe_codes(registry, damage):
    requester, source, pending = transfer_fixture(registry)
    state = read_state(registry)
    if damage == "wrong-owner":
        state["userId"] = "another-user"
    elif damage == "stage-shape":
        state["peers"][source.peer_id]["staging"] = {"revision": 2}
    elif damage == "replay-shape":
        state["peers"][source.peer_id]["replay"]["ack"]["complete"] = "true"
    elif damage == "signal-counter":
        state["transfers"][pending["transferId"]]["requesterCandidates"] = "not-an-integer"
    elif damage == "manifest-shape":
        registry.client.set(registry._manifest_key(registry._account_key("alice"), source.peer_id), '{"revision":1,"assetIds":null}')
    elif damage == "quota-shape":
        registry.client.set(registry._process_key(source.process_id), '{"epoch":[],"transfers":{}}')
    elif damage == "exponent-overflow":
        registry.client.set(registry._account_key("alice"), json.dumps(state)[:-1] + ',"extra":1e999}')
    elif damage == "lone-surrogate":
        registry.client.set(registry._account_key("alice"), json.dumps(state)[:-1] + r',"extra":"\ud800"}')
    if damage not in ("manifest-shape", "quota-shape", "exponent-overflow", "lone-surrogate"):
        registry.client.set(registry._account_key("alice"), json.dumps(state), px=90_000)
    raises("unavailable", lambda: registry.find_sources(requester, asset()))


def test_real_redis_atomic_limits_expiry_and_recovery(monkeypatch):
    url = os.environ.get("KEEPSITE_TEST_REDIS_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("CI must provide KEEPSITE_TEST_REDIS_URL for the peer registry")
        pytest.skip("Local real Redis URL not configured")
    shared = RedisSharedState(url=url)
    registry = PeerRegistry(shared.client, "peer-test-" + uuid.uuid4().hex, "isolated-test-storage")
    try:
        import backend.peer_registry as module
        with monkeypatch.context() as capacity:
            capacity.setattr(module, "PENDING_MS", 30_000)
            quota_scenario(registry, "one-process", concurrent=True)
        a, b = actor(registry, user="expiry", process="expiry-process"), actor(registry, user="expiry", process="expiry-process")
        publish(registry, b, [asset()])
        pending = registry.open_transfer(a, b.peer_id, asset(), media(asset()))
        registry.client.pexpire(registry._transfer_key(registry._account_key("expiry"), pending["transferId"]), 1)
        time.sleep(0.02)
        raises("expired", lambda: registry.accept_transfer(b, pending["transferId"]))
        fresh = registry.open_transfer(a, b.peer_id, asset(), media(asset()))
        assert fresh["transferId"] != pending["transferId"]
        registry.accept_transfer(b, fresh["transferId"])
        lease_key = registry._transfer_key(registry._account_key("expiry"), fresh["transferId"])
        original_pipeline = registry.client.pipeline
        expired_during_exec = []
        def pipeline(*args, **kwargs):
            value = original_pipeline(*args, **kwargs)
            execute = value.execute
            def expire_before_exec():
                if not expired_during_exec:
                    expired_during_exec.append(True)
                    registry.client.pexpire(lease_key, 1)
                    time.sleep(0.02)
                return execute()
            value.execute = expire_before_exec
            return value
        with monkeypatch.context() as patching:
            patching.setattr(registry.client, "pipeline", pipeline)
            raises("expired", lambda: registry.renew_transfer(a, fresh["transferId"]))
        assert expired_during_exec and not registry.client.exists(lease_key)
        assert registry.client.ping()
    finally:
        # Only this test's random scope. Never FLUSHDB or unrelated namespace cleanup.
        owned = list(shared.client.scan_iter(match=registry.prefix + "*"))
        assert all(key.decode().startswith(registry.prefix) for key in owned)
        if owned:
            shared.client.delete(*owned)
        shared.close()

"""Notification loss, bounds, ownership and real Redis transport."""
import asyncio
from contextlib import contextmanager
import json
import os
import threading
import uuid
from types import SimpleNamespace

import pytest

from backend.auth import AuthError
from backend.shared_state import RedisSharedState
from backend.sync_notifications import SyncNotificationHub, MAX_PENDING_ACCOUNTS


class HintStore:
    def __init__(self, server_id=None):
        self.server_id = server_id or str(uuid.uuid4())
        self.values = {"alice": 0, "bob": 0}
        self.listeners = set()
        self.reads = []
        self.on_read = None

    @contextmanager
    def transaction(self, **kwargs):
        yield SimpleNamespace(execute=lambda sql: SimpleNamespace(fetchone=lambda: [self.server_id]))

    def subscribe_changes(self, callback):
        self.listeners.add(callback)
        return lambda: self.listeners.discard(callback)

    def watermark(self, user_id):
        self.reads.append(user_id)
        if self.on_read:
            self.on_read(user_id)
        return {"serverId": self.server_id, "cursor": self.values.get(user_id, 0)}

    def commit(self, user_id, cursor, notify=True):
        self.values[user_id] = cursor
        if notify:
            for callback in tuple(self.listeners):
                callback(user_id, cursor)


class HintAuth:
    cookie_name = "fixture_session"

    def __init__(self):
        self.sessions = {"alice": "alice", "bob": "bob"}
        self.listeners = set()
        self.error = None
        self.checks = 0

    def subscribe_revocations(self, callback):
        self.listeners.add(callback)
        return lambda: self.listeners.discard(callback)

    def websocket_session(self, sid, origin):
        self.checks += 1
        if self.error:
            raise AuthError(self.error, "fixture auth failure")
        if origin != "http://localhost" or sid not in self.sessions:
            raise AuthError(401, "fixture session unavailable")
        return {"user": {"id": self.sessions[sid]}, "sessionKey": sid, "expiresAt": 9_999_999_999_999}

    def revoke(self, sid):
        self.sessions.pop(sid, None)
        for callback in tuple(self.listeners):
            callback(sid)


def hub_fixture(*, heartbeat=0.03, shared=None):
    store, auth = HintStore(), HintAuth()
    hub = SyncNotificationHub(store, shared or SimpleNamespace(client=None), auth, heartbeat_seconds=heartbeat)
    return hub, store, auth


async def eventually(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


def test_thread_callbacks_coalesce_highest_and_isolate_accounts():
    async def run():
        hub, store, auth = hub_fixture(heartbeat=10)
        await hub.start()
        try:
            alice, bob = hub.register("alice", "alice"), hub.register("bob", "bob")
            def changes():
                for cursor in (3, 2, 3, 9):
                    store.commit("alice", cursor)
            thread = threading.Thread(target=changes)
            thread.start()
            thread.join()
            await eventually(lambda: alice.cursor == 9)
            assert bob.cursor == 0 and not bob.changed.is_set()
            assert len(hub.connections) == 2 and not hub._local
            auth.revoke("alice")
            await eventually(lambda: alice.close_code == 1008)
            assert bob.close_code is None
        finally:
            await hub.stop()
        assert not store.listeners and not auth.listeners and not hub._threads
    asyncio.run(run())


def test_lost_publish_while_socket_online_recovers_from_sql_per_account():
    async def run():
        hub, store, _ = hub_fixture()
        await hub.start()
        try:
            one, two = hub.register("alice", "alice"), hub.register("alice", "alice")
            store.commit("alice", 7, notify=False)
            await eventually(lambda: one.cursor == two.cursor == 7)
            assert store.reads == ["alice"]
            assert one.close_code is None
        finally:
            await hub.stop()
    asyncio.run(run())


def test_bounds_and_notification_validation():
    async def run():
        hub, store, _ = hub_fixture(heartbeat=10)
        await hub.start()
        try:
            for _ in range(8):
                hub.register("alice", "alice")
            with pytest.raises(RuntimeError, match="limit"):
                hub.register("alice", "alice")
            for index in range(56):
                hub.register(str(index), str(index))
            with pytest.raises(RuntimeError, match="limit"):
                hub.register("bob", "bob")
            with pytest.raises(RuntimeError, match="capacity"):
                hub.begin_handshake()
            for index in range(1100):
                hub._committed(str(index), index)
            assert len(hub._local) == MAX_PENDING_ACCOUNTS
            for raw in (b"[", b"x" * 4097, json.dumps({"serverId": "other", "userId": "alice", "cursor": 999}),
                        json.dumps({"serverId": store.server_id, "userId": "alice", "cursor": True})):
                hub._incoming(raw)
            await asyncio.sleep(0)
            assert all(item.cursor == 0 for item in hub.connections if item.user_id == "alice")
        finally:
            await hub.stop()
    asyncio.run(run())


def test_pending_handshakes_share_global_connection_budget():
    async def run():
        hub, _, _ = hub_fixture()
        await hub.start()
        try:
            for _ in range(64):
                hub.begin_handshake()
            with pytest.raises(RuntimeError, match="capacity"):
                hub.begin_handshake()
            for _ in range(64):
                hub.end_handshake()
            hub.begin_handshake()
            hub.end_handshake()
            assert hub.handshakes == 0
        finally:
            await hub.stop()
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["second-subscription", "second-thread"])
def test_partial_start_failure_releases_every_successfully_acquired_resource(monkeypatch, failure):
    import fakeredis
    async def run():
        client = fakeredis.FakeRedis()
        hub, store, auth = hub_fixture(shared=SimpleNamespace(client=client))
        started = []
        if failure == "second-subscription":
            def fail_subscribe(callback):
                raise RuntimeError("injected second subscription failure")
            monkeypatch.setattr(auth, "subscribe_revocations", fail_subscribe)
        else:
            original_start = threading.Thread.start
            def start(thread):
                if thread.name == "memoverse-sync-subscriber":
                    raise RuntimeError("injected second thread startup failure")
                result = original_start(thread)
                if thread.name == "memoverse-sync-publisher":
                    started.append(thread)
                return result
            monkeypatch.setattr(threading.Thread, "start", start)
        try:
            with pytest.raises(RuntimeError, match="injected second"):
                await hub.start()
        finally:
            await hub.stop()
        assert not store.listeners and not auth.listeners
        assert not hub._threads and not hub._unsubscribers and hub._loop is None
        assert all(not thread.is_alive() for thread in started)
        assert client.ping(), "Cleanup cannot close a borrowed Redis client"
        client.close()
    asyncio.run(run())


def test_watermark_failure_and_server_change_close_connections():
    async def run():
        hub, store, _ = hub_fixture()
        await hub.start()
        try:
            alice = hub.register("alice", "alice")
            store.server_id = "restored-server"
            await eventually(lambda: alice.close_code == 1013)
        finally:
            await hub.stop()
    asyncio.run(run())


def test_real_redis_cross_hub_notifications_and_reconnect_calibration():
    url = os.environ.get("MEMOVERSE_TEST_REDIS_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("CI must provide MEMOVERSE_TEST_REDIS_URL for real sync PubSub coverage")
        pytest.skip("Local real Redis URL not configured")

    async def run():
        shared_one, shared_two = RedisSharedState(url=url), RedisSharedState(url=url)
        subscriber_name = "memoverse-sync-test-" + uuid.uuid4().hex
        # Fresh pool, before any connections exist. A unique server-side name lets
        # the test disconnect only its own PubSub socket without touching readers.
        shared_two.client.connection_pool.connection_kwargs["client_name"] = subscriber_name
        store = HintStore()
        one = SyncNotificationHub(store, shared_one, HintAuth(), heartbeat_seconds=10)
        other_store = HintStore(store.server_id)
        other_store.values = store.values  # Shared SQL facts, independent post-commit subscribers.
        subscribe_entered, allow_subscribe = threading.Event(), threading.Event()
        original_pubsub = shared_two.client.pubsub
        original_publish = shared_one.client.publish
        publish_finished = threading.Event()

        def observed_publish(*args, **kwargs):
            result = original_publish(*args, **kwargs)
            publish_finished.set()
            return result

        shared_one.client.publish = observed_publish

        class DelayedPubSub:
            def __init__(self):
                self.inner = original_pubsub()

            def subscribe(self, channel):
                subscribe_entered.set()
                if not allow_subscribe.wait(5):
                    raise RuntimeError("Test did not release Redis subscription")
                self.inner.subscribe(channel)

            def get_message(self, **kwargs):
                return self.inner.get_message(**kwargs)

            def close(self):
                self.inner.close()

        shared_two.client.pubsub = DelayedPubSub
        two = SyncNotificationHub(other_store, shared_two, HintAuth(), heartbeat_seconds=10)
        await one.start()
        await two.start()
        try:
            alice, bob = two.register("alice", "alice"), two.register("bob", "bob")
            async def subscribed(count):
                async with asyncio.timeout(5):
                    while dict(await asyncio.to_thread(shared_one.client.pubsub_numsub, one.channel)).get(one.channel.encode(), 0) != count:
                        await asyncio.sleep(0.01)
            await eventually(subscribe_entered.is_set)
            await subscribed(1)
            # Socket's ready path may read SQL before global Redis SUBSCRIBE confirms.
            # A different hub commits in that gap; no receiver exists for its publication.
            assert other_store.watermark("alice")["cursor"] == 0
            store.commit("alice", 12)
            await eventually(publish_finished.is_set)
            assert alice.cursor == 0
            allow_subscribe.set()
            await subscribed(2)
            # Subscription confirmation triggers immediate SQL calibration, not a ten-second wait.
            await eventually(lambda: alice.cursor == 12)
            assert bob.cursor == 0
            # Disconnect from the Redis server, as a network failure would. Calling
            # pool.disconnect() here would close another thread's parser buffer and
            # manufacture an unrelated cross-thread ValueError in redis-py cleanup.
            clients = await asyncio.to_thread(shared_one.client.client_list, _type="pubsub")
            owned = [client for client in clients if client["name"] == subscriber_name]
            assert len(owned) == 1
            old_connection_id = owned[0]["id"]
            other_store.commit("alice", 14, notify=False)
            assert await asyncio.to_thread(shared_one.client.client_kill_filter, _id=old_connection_id) == 1
            await eventually(lambda: alice.cursor == 14, timeout=5)
            await subscribed(2)
            clients = await asyncio.to_thread(shared_one.client.client_list, _type="pubsub")
            reconnected = [client for client in clients if client["name"] == subscriber_name]
            assert len(reconnected) == 1 and reconnected[0]["id"] != old_connection_id
            assert all(thread.is_alive() for thread in two._threads)
            # Also prove ordinary cross-hub delivery, independently of SQL scans.
            live_hint = threading.Event()
            original_incoming = two._incoming
            def observed_incoming(raw):
                original_incoming(raw)
                if json.loads(raw).get("cursor") == 16:
                    live_hint.set()
            two._incoming = observed_incoming
            store.commit("alice", 16)
            await eventually(lambda: live_hint.is_set() and alice.cursor == 16, timeout=3)
            assert bob.cursor == 0
        finally:
            allow_subscribe.set()
            await one.stop()
            await two.stop()
            assert shared_one.client.ping() and shared_two.client.ping(), "Hub must not close borrowed pools"
            shared_one.close()
            shared_two.close()
    asyncio.run(run())

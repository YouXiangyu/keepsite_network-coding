"""Bounded disposable hints; SQL remains authoritative without Redis."""
import asyncio
import json
import threading

from redis.exceptions import RedisError

HEARTBEAT_SECONDS = 15
MAX_CONNECTIONS = 64
MAX_ACCOUNT_CONNECTIONS = 8
MAX_PENDING_ACCOUNTS = 1024
MAX_MESSAGE_BYTES = 4096


class Subscription:
    def __init__(self, user_id, session_key):
        self.user_id, self.session_key = user_id, session_key
        self.cursor, self.close_code = 0, None
        self.changed = asyncio.Event()

    def hint(self, cursor):
        self.cursor = max(self.cursor, cursor)
        self.changed.set()

    def close(self, code):
        self.close_code = code
        self.changed.set()


class SyncNotificationHub:
    def __init__(self, store, shared_state, auth_service, *, heartbeat_seconds=HEARTBEAT_SECONDS):
        self.store, self.client, self.auth = store, shared_state.client, auth_service
        self.heartbeat_seconds = heartbeat_seconds
        self.server_id = None
        self.connections = set()
        self.handshakes = 0
        self._lock = threading.Lock()
        self._local, self._publish, self._revoked = {}, {}, set()
        self._scheduled = self._revoke_all = False
        self._stopping = threading.Event()
        self._publish_wake = threading.Event()
        self._threads, self._unsubscribers = [], []
        self._loop = self._calibration = self._calibrate_wake = None

    def _server_id(self):
        with self.store.transaction(timeout=1) as db:
            return db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]

    async def start(self):
        if self._loop is not None:
            raise RuntimeError("Sync hub already started")
        self._stopping.clear()
        self.server_id = await asyncio.to_thread(self._server_id)
        self.channel = "memoverse:sync:v1:" + self.server_id
        self._loop = asyncio.get_running_loop()
        self._calibrate_wake = asyncio.Event()
        self._unsubscribers.append(self.store.subscribe_changes(self._committed))
        self._unsubscribers.append(self.auth.subscribe_revocations(self._revoked_session))
        if self.client is not None:
            for target in (self._publisher, self._subscriber):
                thread = threading.Thread(target=target, name="memoverse-sync-" + target.__name__[1:], daemon=True)
                thread.start()
                self._threads.append(thread)
        self._calibration = asyncio.create_task(self._calibrate_loop(), name="memoverse-sync-calibration")

    async def stop(self):
        self._stopping.set()
        self._publish_wake.set()
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        for subscription in tuple(self.connections):
            subscription.close(1012)
        self.connections.clear()
        if self._calibration:
            self._calibration.cancel()
            await asyncio.gather(self._calibration, return_exceptions=True)
            self._calibration = None
        # Calls inherit the shared client's bounded socket timeouts; only our threads are joined.
        await asyncio.gather(*(asyncio.to_thread(thread.join) for thread in self._threads))
        self._threads.clear()
        with self._lock:
            self._local.clear()
            self._publish.clear()
            self._revoked.clear()
            self._scheduled = self._revoke_all = False
        self._loop = None

    def register(self, user_id, session_key):
        if self._loop is None or self._stopping.is_set():
            raise RuntimeError("Sync hub unavailable")
        if (len(self.connections) >= MAX_CONNECTIONS
                or sum(item.user_id == user_id for item in self.connections) >= MAX_ACCOUNT_CONNECTIONS):
            raise RuntimeError("Sync connection limit reached")
        subscription = Subscription(user_id, session_key)
        self.connections.add(subscription)
        return subscription

    def begin_handshake(self):
        if self._loop is None or self._stopping.is_set() or len(self.connections) + self.handshakes >= MAX_CONNECTIONS:
            raise RuntimeError("Sync handshake capacity unavailable")
        self.handshakes += 1

    def end_handshake(self):
        self.handshakes -= 1

    def unregister(self, subscription):
        self.connections.discard(subscription)

    def _schedule_locked(self):
        if not self._scheduled and self._loop is not None and not self._stopping.is_set():
            self._scheduled = True
            self._loop.call_soon_threadsafe(self._drain)

    @staticmethod
    def _merge(pending, user_id, cursor):
        if user_id not in pending and len(pending) >= MAX_PENDING_ACCOUNTS:
            pending.pop(next(iter(pending)))  # SQL calibration recovers discarded hints.
        pending[user_id] = max(pending.get(user_id, 0), cursor)

    def _committed(self, user_id, cursor):
        # SQL is already committed and closed. No network or per-event task allocation.
        with self._lock:
            if self._stopping.is_set():
                return
            self._merge(self._local, user_id, cursor)
            if self.client is not None:
                self._merge(self._publish, user_id, cursor)
                self._publish_wake.set()
            self._schedule_locked()

    def _revoked_session(self, session_key):
        with self._lock:
            if self._stopping.is_set():
                return
            if len(self._revoked) >= MAX_PENDING_ACCOUNTS:
                self._revoke_all = True
            else:
                self._revoked.add(session_key)
            self._schedule_locked()

    def _drain(self):
        with self._lock:
            local, revoked, revoke_all = self._local, self._revoked, self._revoke_all
            self._local, self._revoked = {}, set()
            self._scheduled = self._revoke_all = False
        for subscription in tuple(self.connections):
            if revoke_all or subscription.session_key in revoked:
                subscription.close(1008)
            elif subscription.user_id in local:
                subscription.hint(local[subscription.user_id])

    def _publisher(self):
        while not self._stopping.is_set():
            self._publish_wake.wait(0.5)
            self._publish_wake.clear()
            with self._lock:
                pending, self._publish = self._publish, {}
            for user_id, cursor in pending.items():
                if self._stopping.is_set():
                    return
                try:
                    self.client.publish(self.channel, json.dumps({"serverId": self.server_id,
                        "userId": user_id, "cursor": cursor}, separators=(",", ":")))
                except (RedisError, OSError):
                    break  # No retries on a write response; periodic SQL calibration follows.

    def _incoming(self, raw):
        if not isinstance(raw, (bytes, str)) or len(raw) > MAX_MESSAGE_BYTES:
            return
        try:
            value = json.loads(raw)
            if (not isinstance(value, dict) or value.get("serverId") != self.server_id
                    or not isinstance(value.get("userId"), str) or not 1 <= len(value["userId"]) <= 512
                    or type(value.get("cursor")) is not int or not 0 <= value["cursor"] <= 2**53 - 1):
                return
        except (ValueError, UnicodeError, RecursionError):
            return
        with self._lock:
            self._merge(self._local, value["userId"], value["cursor"])
            self._schedule_locked()

    def _subscriber(self):
        while not self._stopping.is_set():
            pubsub = None
            try:
                pubsub = self.client.pubsub()
                pubsub.subscribe(self.channel)
                while not self._stopping.is_set():
                    item = pubsub.get_message(timeout=0.5)
                    if item and item["type"] == "subscribe":
                        self._loop.call_soon_threadsafe(self._calibrate_wake.set)
                    elif item and item["type"] == "message":
                        self._incoming(item["data"])
            except (RedisError, OSError):
                pass
            finally:
                if pubsub is not None:
                    try:
                        pubsub.close()  # Own connection; never close the borrowed client/pool.
                    except (RedisError, OSError):
                        pass
            self._stopping.wait(1)

    async def _calibrate_account(self, user_id):
        try:
            watermark = await asyncio.to_thread(self.store.watermark, user_id)
            if watermark["serverId"] != self.server_id:
                raise RuntimeError("Sync server identity changed")
            for subscription in tuple(self.connections):
                if subscription.user_id == user_id:
                    subscription.hint(watermark["cursor"])
        except Exception:
            for subscription in tuple(self.connections):
                if subscription.user_id == user_id:
                    subscription.close(1013)

    async def _calibrate_loop(self):
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._calibrate_wake.wait(), self.heartbeat_seconds)
            except TimeoutError:
                pass
            self._calibrate_wake.clear()
            await asyncio.gather(*(self._calibrate_account(user_id)
                                   for user_id in {item.user_id for item in self.connections}))

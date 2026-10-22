"""Bounded immutable snapshot views and contiguous durable change pages."""
from dataclasses import dataclass, field
import json
import secrets
import threading
import time

from .state import cached_account_state, capabilities
from .store import StoreError, encode

PAGE_BYTES = 1024 * 1024
SNAPSHOT_BYTES = 16 * 1024 * 1024
TOTAL_BYTES = 128 * 1024 * 1024
MAX_TICKETS = 10_000
MAX_VIEWS = 32
MAX_ACCOUNT_VIEWS = 4
SNAPSHOT_SECONDS = 10 * 60


class SyncError(StoreError):
    def __init__(self, code, message):
        super().__init__(410, message)
        self.code = code


def snapshot_expired():
    return SyncError("snapshot_expired", "同步快照已失效，请重新读取票夹。")


@dataclass
class SnapshotView:
    identifier: str
    user_id: str
    server_id: str
    cursor: int
    items: tuple[bytes, ...]
    capability_bytes: bytes
    expires_at: int
    deadline: float
    size: int
    tokens: dict[int, str] = field(default_factory=dict)


class SyncHTTP:
    def __init__(self, store, shared_state, *, clock=time.time, monotonic=time.monotonic):
        self.store, self.shared_state = store, shared_state
        self.clock, self.monotonic = clock, monotonic
        self.lock = threading.Lock()
        self.views = {}
        self.tokens = {}
        self.bytes_used = 0
        self.reservations = {}

    def _drop(self, identifier):
        view = self.views.pop(identifier, None)
        if view:
            self.bytes_used -= view.size
            for token in view.tokens.values():
                self.tokens.pop(token, None)

    def _expire(self):
        for identifier in [key for key, value in self.views.items() if value.deadline <= self.monotonic()]:
            self._drop(identifier)

    def close(self):
        with self.lock:
            self.views.clear()
            self.tokens.clear()
            self.bytes_used = 0

    def _reserve(self, user_id):
        with self.lock:
            self._expire()
            reserved = sum(self.reservations.values())
            own = sum(view.user_id == user_id for view in self.views.values()) + self.reservations.get(user_id, 0)
            if own >= MAX_ACCOUNT_VIEWS or len(self.views) + reserved >= MAX_VIEWS:
                raise StoreError(429, "正在读取的同步快照过多，请完成或等待现有快照过期。")
            if self.bytes_used + (reserved + 1) * SNAPSHOT_BYTES > TOTAL_BYTES:
                raise StoreError(503, "同步快照容量暂不可用，请稍后重试。")
            self.reservations[user_id] = self.reservations.get(user_id, 0) + 1

    def _release(self, user_id):
        count = self.reservations[user_id] - 1
        if count:
            self.reservations[user_id] = count
        else:
            del self.reservations[user_id]

    def snapshot(self, user_id, *, limit=100, page_token=None):
        if not 1 <= limit <= 200:
            raise StoreError(422, "同步分页数量应为 1 至 200。")
        if page_token is not None:
            with self.lock:
                self._expire()
                position = self.tokens.get(page_token)
                if position is None:
                    raise snapshot_expired()
                identifier, offset = position
                view = self.views.get(identifier)
                if view is None or view.user_id != user_id:
                    raise snapshot_expired()
                return self._page(view, offset, limit)
        self._reserve(user_id)
        try:
            source = cached_account_state(self.store, self.shared_state, user_id,
                                          maximum_tickets=MAX_TICKETS, maximum_bytes=SNAPSHOT_BYTES)
            items = []
            capability_bytes = encode(source["capabilities"]).encode()
            size = len(capability_bytes) + 1024
            if size > SNAPSHOT_BYTES:
                raise StoreError(413, "同步快照配置超过容量，未截断内容。")
            for ticket in source["data"]["tickets"]:
                raw = encode(ticket).encode()
                size += len(raw) + 1
                if size > SNAPSHOT_BYTES:
                    raise StoreError(413, "票夹超过同步快照字节容量，未截断任何记录。")
                items.append(raw)
            view = SnapshotView(secrets.token_urlsafe(32), user_id, source["serverId"], source["revision"],
                                tuple(items), capability_bytes, int(self.clock() * 1000) + SNAPSHOT_SECONDS * 1000,
                                self.monotonic() + SNAPSHOT_SECONDS, size)
            with self.lock:
                self.views[view.identifier] = view
                self.bytes_used += view.size
                try:
                    return self._page(view, 0, limit)
                except BaseException:
                    self._drop(view.identifier)
                    raise
        finally:
            with self.lock:
                self._release(user_id)

    def _page(self, view, offset, limit):
        total = len(view.items)
        if not 0 <= offset <= total:
            raise snapshot_expired()
        response = {"serverId": view.server_id, "userId": view.user_id, "snapshotId": view.identifier,
                    "cursor": view.cursor, "total": total, "offset": offset, "tickets": [],
                    "nextPageToken": None, "expiresAt": view.expires_at,
                    "capabilities": json.loads(view.capability_bytes)}
        selected, raw_size = [], 0
        for raw in view.items[offset:offset + limit]:
            next_offset = offset + len(selected) + 1
            response["nextPageToken"] = "x" * 43 if next_offset < total else None
            prospective = len(encode(response).encode()) + raw_size + len(raw) + len(selected)
            if prospective > PAGE_BYTES:
                break
            selected.append(raw)
            raw_size += len(raw)
        if not selected and offset < total:
            raise StoreError(413, "单条票根超过同步页容量，未截断记录。")
        next_offset = offset + len(selected)
        if next_offset < total:
            token = view.tokens.get(next_offset)
            if token is None:
                token = secrets.token_urlsafe(32)
                view.tokens[next_offset] = token
                self.tokens[token] = (view.identifier, next_offset)
            response["nextPageToken"] = token
        else:
            response["nextPageToken"] = None
        response["tickets"] = [json.loads(raw) for raw in selected]
        if response["nextPageToken"] is None:
            # Finished views do not consume active quotas. A lost final response
            # requires a fresh snapshot, instead of a second replay cache.
            self._drop(view.identifier)
        return response

    def changes(self, user_id, server_id, after, *, limit=100):
        if type(after) is not int or after < 0 or not 1 <= limit <= 200:
            raise StoreError(422, "同步游标或分页数量无效。")
        self.store.prune_changes(user_id)
        with self.store.transaction(timeout=1) as db:
            user = self.store.require_user(db, user_id)
            server = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
            head = user["revision"]
            floor = db.execute("SELECT cursor FROM sync_baselines WHERE user_id=?", (user_id,)).fetchone()[0]
            if server_id != server:
                raise SyncError("server_changed", "同步空间已改变，请重新读取票夹。")
            if after < floor:
                raise SyncError("cursor_expired", "同步游标已过期，请重新读取票夹。")
            if after > head:
                raise SyncError("cursor_ahead", "同步游标超出当前空间，请重新读取票夹。")
            rows = db.execute("SELECT cursor,ticket_id,payload FROM ticket_changes WHERE user_id=? AND cursor>? AND cursor<=? ORDER BY cursor LIMIT ?",
                              (user_id, after, head, limit)).fetchall()
        response = {"serverId": server, "userId": user_id, "fromCursor": after, "nextCursor": after,
                    "headCursor": head, "hasMore": after < head, "changes": [], "capabilities": capabilities(self.store)}
        expected, size, limited = after + 1, 0, False
        for row in rows:
            if row["cursor"] != expected:
                raise SyncError("cursor_gap", "同步记录不连续，请重新读取票夹。")
            try:
                ticket = json.loads(row["payload"])
                if ticket["id"] != row["ticket_id"]:
                    raise ValueError()
                event = ({"cursor": row["cursor"], "type": "delete", "id": ticket["id"],
                          "version": ticket["version"], "deletedAt": ticket["deletedAt"]}
                         if ticket["deletedAt"] is not None else {"cursor": row["cursor"], "type": "upsert", "ticket": ticket})
            except (ValueError, KeyError, TypeError):
                raise SyncError("cursor_gap", "同步记录无法读取，请重新读取票夹。") from None
            raw_size = len(encode(event).encode())
            # Use a safe upper bound for cursor digit growth and list separators.
            if len(encode({**response, "changes": []}).encode()) + size + raw_size + len(response["changes"]) + 32 > PAGE_BYTES:
                limited = True
                break
            response["changes"].append(event)
            response["nextCursor"] = row["cursor"]
            size += raw_size
            expected += 1
        if not response["changes"] and after < head:
            if limited:
                raise StoreError(413, "单条变更超过同步页容量，未跳过变更。")
            raise SyncError("cursor_gap", "同步记录不连续，请重新读取票夹。")
        if not limited and len(rows) < limit and response["nextCursor"] < head:
            raise SyncError("cursor_gap", "同步记录不连续，请重新读取票夹。")
        response["hasMore"] = response["nextCursor"] < head
        return response

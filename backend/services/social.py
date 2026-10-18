"""Account-owned shares and community interactions."""
from __future__ import annotations

import json
from datetime import date as Date

from ..contracts import Like
from ..state import state_response
from ..store import StoreError, encode
from .collections import collect_item


def share_record(db, user_id: str, identifier: str):
    row = db.execute("SELECT payload FROM shares WHERE user_id=? AND id=?", (user_id, identifier)).fetchone()
    if not row:
        raise StoreError(404, "分享不存在或无权访问。")
    return json.loads(row[0])


def community_record(db, user_id: str, identifier: str):
    row = db.execute("SELECT payload FROM community WHERE user_id=? AND id=?", (user_id, identifier)).fetchone()
    if not row:
        raise StoreError(404, "社区示例不存在或无权访问。")
    return json.loads(row[0])


def read_all(store, worker, user_id: str):
    with store.transaction(True) as db:
        changed = False
        for row in db.execute("SELECT * FROM shares WHERE user_id=?", (user_id,)).fetchall():
            payload = json.loads(row["payload"])
            if payload["unread"]:
                payload["unread"] = False
                db.execute("UPDATE shares SET payload=? WHERE user_id=? AND id=?", (encode(payload), user_id, row["id"]))
                changed = True
        return state_response(store, worker, db, user_id, changed=changed)


def read_share(store, worker, user_id: str, identifier: str):
    with store.transaction(True) as db:
        payload = share_record(db, user_id, identifier)
        changed = payload["unread"]
        if changed:
            payload["unread"] = False
            db.execute("UPDATE shares SET payload=? WHERE user_id=? AND id=?", (encode(payload), user_id, identifier))
        return state_response(store, worker, db, user_id, changed=changed)


def collect_share(store, worker, user_id: str, identifier: str):
    with store.transaction(True) as db:
        changed = collect_item(store, db, user_id, share_record(db, user_id, identifier)["item"])
        result = state_response(store, worker, db, user_id, changed=changed)
    worker.wake()
    return result


def view_community(store, worker, user_id: str, identifier: str):
    with store.transaction(True) as db:
        payload = community_record(db, user_id, identifier)
        payload["exposures"] += 1
        db.execute("UPDATE community SET payload=? WHERE user_id=? AND id=?", (encode(payload), user_id, identifier))
        return state_response(store, worker, db, user_id, changed=True)


def like_community(store, worker, user_id: str, identifier: str, body: Like):
    with store.transaction(True) as db:
        payload = community_record(db, user_id, identifier)
        liked = bool(db.execute("SELECT 1 FROM likes WHERE user_id=? AND id=?", (user_id, identifier)).fetchone())
        if liked == body.liked:
            return state_response(store, worker, db, user_id)
        if body.liked:
            db.execute("INSERT INTO likes VALUES (?,?)", (user_id, identifier))
        else:
            db.execute("DELETE FROM likes WHERE user_id=? AND id=?", (user_id, identifier))
        payload["likes"] = max(0, payload["likes"] + (1 if body.liked else -1))
        db.execute("UPDATE community SET payload=? WHERE user_id=? AND id=?", (encode(payload), user_id, identifier))
        return state_response(store, worker, db, user_id, changed=True)


def collect_community(store, worker, user_id: str, identifier: str):
    with store.transaction(True) as db:
        payload = community_record(db, user_id, identifier)
        item = {"id": f"community-{identifier}", "title": payload["title"], "category": payload["category"], "art": payload["art"], "tags": [], "note": payload["description"][:500], "source": "friend", "sender": payload["author"], "date": Date.today().isoformat(), "assetId": payload["assetId"], "modelId": payload.get("modelId")}
        changed = collect_item(store, db, user_id, item)
        if changed:
            payload["saves"] += 1
            db.execute("UPDATE community SET payload=? WHERE user_id=? AND id=?", (encode(payload), user_id, identifier))
        return state_response(store, worker, db, user_id, changed=changed)

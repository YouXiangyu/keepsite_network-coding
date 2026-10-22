"""Atomic field updates, operation replay, and deletion tombstones."""
import hashlib
import json
from ..state import account_state
from ..store import StoreError, encode, now_ms


def apply(store, user_id, identifier, action, body):
    fingerprint = hashlib.sha256(encode({"action": action, "ticketId": identifier,
                                       "body": body.model_dump(exclude_unset=True)}).encode()).hexdigest()
    with store.transaction(True) as db:
        store.require_user(db, user_id)
        operation = db.execute("SELECT fingerprint FROM operations WHERE user_id=? AND id=?", (user_id, body.operationId)).fetchone()
        if operation:
            if operation[0] != fingerprint:
                raise StoreError(409, "操作标识已用于不同请求，请使用新的操作标识。")
            return account_state(store, db, user_id)
        stamp = now_ms()
        if action == "create":
            if db.execute("SELECT 1 FROM tickets WHERE user_id=? AND id=?", (user_id, identifier)).fetchone():
                raise StoreError(409, "电影票已创建或已删除，不能重复创建。")
            store.attach_asset(db, user_id, body.originalAssetId)
            fields = body.fields.model_dump()
            sources = {key: "user" if key in body.fields.model_fields_set else "empty" for key in fields}
            db.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,1,1,?,?,NULL,?)",
                       (user_id, identifier, encode(fields), encode(sources), body.originalAssetId, None, stamp, stamp, body.operationId))
        elif action == "update":
            row = store.ticket(db, user_id, identifier)
            fields, sources = json.loads(row["fields"]), json.loads(row["field_sources"])
            changes = body.fields.model_dump(exclude_unset=True)
            fields.update(changes)
            sources.update({key: "user" for key in changes})
            original = body.originalAssetId or row["original_asset_id"]
            store.attach_asset(db, user_id, original)
            input_changed = original != row["original_asset_id"] or any(key in changes for key in ("title", "showtime"))
            db.execute("UPDATE tickets SET fields=?,field_sources=?,original_asset_id=?,poster_asset_id=?,version=version+1,input_version=input_version+?,updated_at=?,operation_id=? WHERE user_id=? AND id=?",
                       (encode(fields), encode(sources), original, row["poster_asset_id"], int(input_changed), stamp, body.operationId, user_id, identifier))
        elif action == "delete":
            store.ticket(db, user_id, identifier)
            db.execute("UPDATE tickets SET deleted_at=?,updated_at=?,version=version+1,input_version=input_version+1,operation_id=? WHERE user_id=? AND id=?",
                       (stamp, stamp, body.operationId, user_id, identifier))
        else:
            raise ValueError("Unknown ticket action")
        db.execute("INSERT INTO operations VALUES (?,?,?,?)", (user_id, body.operationId, fingerprint, stamp))
        store.bump(db, user_id)
        return account_state(store, db, user_id)

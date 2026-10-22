"""Account-scoped snapshot; incremental synchronization follows in M4."""
import json
from .object_storage import S3ObjectStore


def media_ref(store, db, user_id, identifier, assets=None):
    asset = assets[identifier] if assets is not None else store.asset(db, user_id, identifier)
    return {"assetId": asset["id"], "sha256": asset["id"], "mimeType": asset["mime"],
            "size": asset["size"], "width": asset["width"], "height": asset["height"],
            "url": f"/api/media/{asset['id']}",
            "cloudStatus": "saved" if isinstance(store.storage, S3ObjectStore) else "not_configured"}


def present_ticket(store, db, user_id, row, assets=None):
    return {"id": row["id"], "fields": json.loads(row["fields"]),
            "fieldSources": json.loads(row["field_sources"]), "createdAt": row["created_at"],
            "updatedAt": row["updated_at"], "version": row["version"], "inputVersion": row["input_version"],
            "deletedAt": row["deleted_at"], "operationId": row["operation_id"],
            "media": {"original": media_ref(store, db, user_id, row["original_asset_id"], assets),
                      "poster": media_ref(store, db, user_id, row["poster_asset_id"], assets) if row["poster_asset_id"] else None},
            "processing": {"status": "not_started", "error": None}}


def account_state(store, db, user_id):
    user = store.require_user(db, user_id)
    server = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
    assets = {row["id"]: row for row in db.execute("""SELECT a.* FROM assets a
        JOIN asset_grants g ON g.asset_id=a.id WHERE g.user_id=? AND a.id IN (
            SELECT original_asset_id FROM tickets WHERE user_id=? AND deleted_at IS NULL
            UNION SELECT poster_asset_id FROM tickets WHERE user_id=? AND deleted_at IS NULL
        )""", (user_id, user_id, user_id))}
    tickets = [present_ticket(store, db, user_id, row, assets) for row in db.execute(
        "SELECT * FROM tickets WHERE user_id=? AND deleted_at IS NULL ORDER BY created_at,id", (user_id,))]
    return {"initialized": True, "serverId": server, "userId": user_id, "revision": user["revision"],
            "data": {"tickets": tickets}, "capabilities": {
                "processing": False, "cloudStorage": isinstance(store.storage, S3ObjectStore), "deviceSync": False}}


def cached_account_state(store, shared_state, user_id):
    # Redis must run after each SQL transaction has released its connection.
    with store.transaction() as db:
        user = store.require_user(db, user_id)
        revision = user["revision"]
        server_id = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
    identity = store.storage.cache_identity
    cached = shared_state.get_snapshot(user_id, server_id, revision, identity)
    if cached is not None:
        return cached
    with store.transaction() as db:
        result = account_state(store, db, user_id)
    shared_state.put_snapshot(user_id, result["serverId"], result["revision"], identity, result)
    return result

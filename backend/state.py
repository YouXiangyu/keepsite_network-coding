"""Account-scoped snapshot; incremental synchronization follows in M4."""
import json
from .object_storage import S3ObjectStore


def media_ref(store, db, user_id, identifier):
    asset = store.asset(db, user_id, identifier)
    return {"assetId": asset["id"], "sha256": asset["id"], "mimeType": asset["mime"],
            "size": asset["size"], "width": asset["width"], "height": asset["height"],
            "url": f"/api/media/{asset['id']}",
            "cloudStatus": "saved" if isinstance(store.storage, S3ObjectStore) else "not_configured"}


def present_ticket(store, db, user_id, row):
    return {"id": row["id"], "fields": json.loads(row["fields"]),
            "fieldSources": json.loads(row["field_sources"]), "createdAt": row["created_at"],
            "updatedAt": row["updated_at"], "version": row["version"], "inputVersion": row["input_version"],
            "deletedAt": row["deleted_at"], "operationId": row["operation_id"],
            "media": {"original": media_ref(store, db, user_id, row["original_asset_id"]),
                      "poster": media_ref(store, db, user_id, row["poster_asset_id"]) if row["poster_asset_id"] else None},
            "processing": {"status": "not_started", "error": None}}


def account_state(store, db, user_id):
    user = store.require_user(db, user_id)
    server = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
    tickets = [present_ticket(store, db, user_id, row) for row in db.execute(
        "SELECT * FROM tickets WHERE user_id=? AND deleted_at IS NULL ORDER BY created_at,id", (user_id,))]
    return {"initialized": True, "serverId": server, "userId": user_id, "revision": user["revision"],
            "data": {"tickets": tickets}, "capabilities": {
                "processing": False, "cloudStorage": isinstance(store.storage, S3ObjectStore), "deviceSync": False}}

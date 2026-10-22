"""Account-scoped snapshot; incremental synchronization follows in M4."""
import json
import hashlib
from .object_storage import S3ObjectStore


def capabilities(store):
    return {"processing": store.processing_enabled, "processingUnavailableReason": store.processing_unavailable_reason,
            "cloudStorage": isinstance(store.storage, S3ObjectStore), "deviceSync": False}


def media_ref(store, db, user_id, identifier, assets=None):
    asset = assets[identifier] if assets is not None else store.asset(db, user_id, identifier)
    return {"assetId": asset["id"], "sha256": asset["id"], "mimeType": asset["mime"],
            "size": asset["size"], "width": asset["width"], "height": asset["height"],
            "url": f"/api/media/{asset['id']}",
            "cloudStatus": "saved" if isinstance(store.storage, S3ObjectStore) else "not_configured"}


def present_ticket(store, db, user_id, row, assets=None, processing=None, sources=None):
    if processing is None:
        job = db.execute("""SELECT j.* FROM ticket_processing p JOIN processing_jobs j ON p.job_id=j.id
            WHERE p.user_id=? AND p.ticket_id=? AND j.input_version=? AND j.original_asset_id=?""",
                         (user_id, row["id"], row["input_version"], row["original_asset_id"])).fetchone()
    else:
        job = processing.get(row["id"])
    status = job["status"] if job and job["status"] in {"queued", "ocr", "poster", "ready", "no_match", "failed"} else "not_started"
    poster = media_ref(store, db, user_id, row["poster_asset_id"], assets) if row["poster_asset_id"] else None
    if poster:
        source = (sources.get(row["id"]) if sources is not None else db.execute(
            "SELECT * FROM poster_sources WHERE user_id=? AND ticket_id=?", (user_id, row["id"])).fetchone())
        if source and source["asset_id"] == row["poster_asset_id"]:
            poster["source"] = json.loads(source["source"])
    return {"id": row["id"], "fields": json.loads(row["fields"]),
            "fieldSources": json.loads(row["field_sources"]), "createdAt": row["created_at"],
            "updatedAt": row["updated_at"], "version": row["version"], "inputVersion": row["input_version"],
            "deletedAt": row["deleted_at"], "operationId": row["operation_id"],
            "media": {"original": media_ref(store, db, user_id, row["original_asset_id"], assets),
                      "poster": poster},
            "processing": {"status": status, "error": job["error"] if job and status != "not_started" else None}}


def account_state(store, db, user_id):
    user = store.require_user(db, user_id)
    server = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
    assets = {row["id"]: row for row in db.execute("""SELECT a.* FROM assets a
        JOIN asset_grants g ON g.asset_id=a.id WHERE g.user_id=? AND a.id IN (
            SELECT original_asset_id FROM tickets WHERE user_id=? AND deleted_at IS NULL
            UNION SELECT poster_asset_id FROM tickets WHERE user_id=? AND deleted_at IS NULL
        )""", (user_id, user_id, user_id))}
    processing = {row["ticket_id"]: row for row in db.execute("""SELECT j.* FROM ticket_processing p
        JOIN processing_jobs j ON j.id=p.job_id JOIN tickets t ON t.user_id=p.user_id AND t.id=p.ticket_id
        WHERE p.user_id=? AND j.input_version=t.input_version AND j.original_asset_id=t.original_asset_id""", (user_id,))}
    sources = {row["ticket_id"]: row for row in db.execute("SELECT * FROM poster_sources WHERE user_id=?", (user_id,))}
    tickets = [present_ticket(store, db, user_id, row, assets, processing, sources) for row in db.execute(
        "SELECT * FROM tickets WHERE user_id=? AND deleted_at IS NULL ORDER BY created_at,id", (user_id,))]
    return {"initialized": True, "serverId": server, "userId": user_id, "revision": user["revision"],
            "data": {"tickets": tickets}, "capabilities": capabilities(store)}


def cached_account_state(store, shared_state, user_id):
    # Redis must run after each SQL transaction has released its connection.
    with store.transaction() as db:
        user = store.require_user(db, user_id)
        revision = user["revision"]
        server_id = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
    configuration = json.dumps(capabilities(store), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    identity = store.storage.cache_identity + ":capabilities:" + hashlib.sha256(configuration.encode()).hexdigest()
    cached = shared_state.get_snapshot(user_id, server_id, revision, identity)
    if cached is not None:
        return cached
    with store.transaction() as db:
        result = account_state(store, db, user_id)
    shared_state.put_snapshot(user_id, result["serverId"], result["revision"], identity, result)
    return result

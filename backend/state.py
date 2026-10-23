"""Consistent account snapshot capture and reusable ticket representations."""
import json
import hashlib
from .object_storage import S3ObjectStore
from .store import StoreError, encode


def capabilities(store):
    return {"processing": store.processing_enabled, "processingUnavailableReason": store.processing_unavailable_reason,
            "cloudStorage": isinstance(store.storage, S3ObjectStore), "deviceSync": True}


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


def account_state(store, db, user_id, *, maximum_tickets=None, maximum_bytes=None):
    user = store.require_user(db, user_id)
    server = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
    current_capabilities = capabilities(store)
    size = len(encode(current_capabilities).encode()) + 1024
    tickets = []
    rows = db.execute("SELECT * FROM tickets WHERE user_id=? AND deleted_at IS NULL ORDER BY created_at,id", (user_id,))
    while batch := rows.fetchmany(200):
        identifiers = [row["id"] for row in batch]
        media_ids = sorted({row[name] for row in batch for name in ("original_asset_id", "poster_asset_id") if row[name]})
        ticket_marks, media_marks = ",".join("?" for _ in identifiers), ",".join("?" for _ in media_ids)
        assets = {row["id"]: row for row in db.execute(
            f"SELECT a.* FROM assets a JOIN asset_grants g ON g.asset_id=a.id WHERE g.user_id=? AND a.id IN ({media_marks})",
            (user_id, *media_ids))}
        processing = {row["ticket_id"]: row for row in db.execute(f"""SELECT j.* FROM ticket_processing p
            JOIN processing_jobs j ON j.id=p.job_id JOIN tickets t ON t.user_id=p.user_id AND t.id=p.ticket_id
            WHERE p.user_id=? AND p.ticket_id IN ({ticket_marks}) AND j.input_version=t.input_version
            AND j.original_asset_id=t.original_asset_id""", (user_id, *identifiers))}
        sources = {row["ticket_id"]: row for row in db.execute(
            f"SELECT * FROM poster_sources WHERE user_id=? AND ticket_id IN ({ticket_marks})", (user_id, *identifiers))}
        for row in batch:
            ticket = present_ticket(store, db, user_id, row, assets, processing, sources)
            size += len(encode(ticket).encode()) + 1
            if maximum_bytes is not None and size > maximum_bytes:
                raise StoreError(413, "票夹超过同步快照字节容量，未截断任何记录。")
            if maximum_tickets is not None and len(tickets) >= maximum_tickets:
                raise StoreError(413, "票夹超过同步快照容量，未截断任何记录。")
            tickets.append(ticket)
    return {"initialized": True, "serverId": server, "userId": user_id, "revision": user["revision"],
            "data": {"tickets": tickets}, "capabilities": current_capabilities}


def cached_account_state(store, shared_state, user_id, *, maximum_tickets=None, maximum_bytes=None):
    # Redis must run after each SQL transaction has released its connection.
    with store.transaction() as db:
        user = store.require_user(db, user_id)
        revision = user["revision"]
        server_id = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
        if maximum_tickets is not None and db.execute("SELECT COUNT(*) FROM tickets WHERE user_id=? AND deleted_at IS NULL", (user_id,)).fetchone()[0] > maximum_tickets:
            raise StoreError(413, "票夹超过同步快照容量，未截断任何记录。")
    configuration = json.dumps(capabilities(store), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    identity = store.storage.cache_identity + ":capabilities:" + hashlib.sha256(configuration.encode()).hexdigest()
    cached = shared_state.get_snapshot(user_id, server_id, revision, identity)
    if cached is not None:
        if maximum_tickets is not None and len(cached["data"]["tickets"]) > maximum_tickets:
            raise StoreError(413, "票夹超过同步快照容量，未截断任何记录。")
        if maximum_bytes is not None and len(encode(cached).encode()) > maximum_bytes:
            raise StoreError(413, "票夹超过同步快照字节容量，未截断任何记录。")
        return cached
    with store.transaction() as db:
        if maximum_tickets is not None and db.execute("SELECT COUNT(*) FROM tickets WHERE user_id=? AND deleted_at IS NULL", (user_id,)).fetchone()[0] > maximum_tickets:
            raise StoreError(413, "票夹超过同步快照容量，未截断任何记录。")
        result = account_state(store, db, user_id, maximum_tickets=maximum_tickets, maximum_bytes=maximum_bytes)
    shared_state.put_snapshot(user_id, result["serverId"], result["revision"], identity, result)
    return result

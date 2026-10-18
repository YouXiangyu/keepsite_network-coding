"""Collection mutations and generation requests, with one transaction per action."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections import Counter

from ..contracts import CreateCollection, UpdateCollection, Version
from ..generation.repository import enqueue
from ..state import state_response
from ..store import StoreError, encode, now_ms


def infer_tag(db, user_id, title):
    normalize = lambda value: unicodedata.normalize("NFKC", value).strip().lower()
    query = normalize(title)
    keywords = [part for part in re.split(r"[\W_]+", query) if part]
    counts = Counter()
    for row in db.execute("SELECT payload FROM collections WHERE user_id=? ORDER BY rowid", (user_id,)):
        item = json.loads(row[0])
        name = normalize(item["title"])
        if name and (query in name or name in query or any(part in name for part in keywords)):
            counts.update(list(dict.fromkeys(item["tags"])))
    return [counts.most_common(1)[0][0]] if counts else []


def create(store, worker, user_id: str, body: CreateCollection):
    fingerprint = hashlib.sha256(encode(body.model_dump()).encode()).hexdigest()
    with store.transaction(True) as db:
        existing = db.execute("SELECT * FROM collections WHERE user_id=? AND id=?", (user_id, body.id)).fetchone()
        if existing:
            if existing["create_fingerprint"] == fingerprint:
                return state_response(store, worker, db, user_id)
            raise StoreError(409, "这件收藏已创建且内容不同，请刷新后编辑。")
        store.asset(db, user_id, body.assetId)
        payload = body.model_dump(exclude={"assetId"})
        payload["tags"] = list(dict.fromkeys(body.tags)) if body.tags is not None else infer_tag(db, user_id, body.title)
        payload.update(addedAt=now_ms(), source="mine")
        revision = db.execute("SELECT COALESCE(MAX(input_revision),0)+1 FROM jobs WHERE user_id=? AND collection_id=?", (user_id, body.id)).fetchone()[0]
        db.execute("INSERT INTO collections(user_id,id,payload,asset_id,create_fingerprint,input_revision) VALUES (?,?,?,?,?,?)", (user_id, body.id, encode(payload), body.assetId, fingerprint, revision))
        enqueue(store, db, user_id, body.id, body.assetId, revision)
        result = state_response(store, worker, db, user_id, changed=True)
    worker.wake()
    return result


def update(store, worker, user_id: str, identifier: str, body: UpdateCollection):
    with store.transaction(True) as db:
        row = store.collection(db, user_id, identifier)
        if row["version"] != body.version:
            raise StoreError(409, "这件收藏已更新，草稿仍保留，请重新读取。")
        asset_id = row["asset_id"]
        if "assetId" in body.model_fields_set:
            if not body.assetId:
                raise StoreError(422, "收藏必须保留图片。")
            store.asset(db, user_id, body.assetId)
            asset_id = body.assetId
        payload = json.loads(row["payload"])
        regenerate = asset_id != row["asset_id"] or body.art != payload["art"]
        changes = body.model_dump(exclude={"version", "assetId"}, exclude_none=True)
        changes["tags"] = list(dict.fromkeys(changes["tags"]))
        payload.update(changes)
        revision = row["input_revision"] + int(regenerate)
        db.execute("UPDATE collections SET payload=?,asset_id=?,version=version+1,input_revision=?,model_id=? WHERE user_id=? AND id=?", (encode(payload), asset_id, revision, None if regenerate else row["model_id"], user_id, identifier))
        if regenerate:
            if row["job_id"]:
                db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE user_id=? AND id=? AND status!='ready'", (now_ms(), user_id, row["job_id"]))
            enqueue(store, db, user_id, identifier, asset_id, revision)
        result = state_response(store, worker, db, user_id, changed=True)
    if regenerate and row["job_id"]:
        worker.cancel(user_id, row["job_id"])
        result["generationRecovery"] = worker.recovery_for(user_id)
    worker.wake()
    return result


def delete(store, worker, user_id: str, identifier: str, body: Version):
    with store.transaction(True) as db:
        row = store.collection(db, user_id, identifier)
        if row["version"] != body.version:
            raise StoreError(409, "收藏已更新，请刷新后再删除。")
        db.execute("DELETE FROM collections WHERE user_id=? AND id=?", (user_id, identifier))
        db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE user_id=? AND collection_id=? AND status!='ready'", (now_ms(), user_id, identifier))
        result = state_response(store, worker, db, user_id, changed=True)
    if row["job_id"]:
        worker.cancel(user_id, row["job_id"])
        result["generationRecovery"] = worker.recovery_for(user_id)
    return result


def retry_generation(store, worker, user_id: str, identifier: str):
    with store.transaction(True) as db:
        row = store.collection(db, user_id, identifier)
        if not row["job_id"]:
            raise StoreError(422, "此模板模型已经就绪，无需重新生成。")
        job = store.job(db, user_id, row["job_id"])
        pending = worker.has_completion(user_id, job["id"])
        changed = not pending and job["status"] in {"failed", "blocked"}
        if changed:
            db.execute("UPDATE jobs SET status='queued',error='',updated_at=? WHERE user_id=? AND id=?", (now_ms(), user_id, job["id"]))
        result = state_response(store, worker, db, user_id, changed=changed)
    if pending:
        worker.retry_publication(user_id, job["id"])
        result["generationRecovery"] = worker.recovery_for(user_id)
    worker.wake()
    return result


def job_detail(store, user_id: str, identifier: str):
    with store.transaction() as db:
        job = store.job(db, user_id, identifier)
        result = {"id": job["id"], "collectionId": job["collection_id"], "status": job["status"], "error": job["error"], "createdAt": job["created_at"], "updatedAt": job["updated_at"], "inputRevision": job["input_revision"]}
        if job["status"] == "ready" and job["model_id"]:
            store.model(db, user_id, job["model_id"])
            result["modelUrl"] = f"/api/models/{job['model_id']}/model.glb"
        return result


def collect_item(store, db, user_id: str, item):
    if db.execute("SELECT 1 FROM collections WHERE user_id=? AND id=?", (user_id, item["id"])).fetchone():
        return False
    store.asset(db, user_id, item["assetId"])
    model_id = item.get("modelId")
    if model_id and store.model(db, user_id, model_id)["asset_id"] != item["assetId"]:
        raise StoreError(409, "分享模型与原图不一致。")
    payload = {key: copy.deepcopy(value) for key, value in item.items() if key not in {"assetId", "modelId", "customImage", "version", "generation", "generationJobId", "modelUrl", "generationError", "modelCamera"}}
    payload.update(addedAt=now_ms(), source="friend")
    revision = db.execute("SELECT COALESCE(MAX(input_revision),0)+1 FROM jobs WHERE user_id=? AND collection_id=?", (user_id, payload["id"])).fetchone()[0]
    db.execute("INSERT INTO collections(user_id,id,payload,asset_id,model_id,input_revision) VALUES (?,?,?,?,?,?)", (user_id, payload["id"], encode(payload), item["assetId"], model_id, revision))
    if not model_id:
        enqueue(store, db, user_id, payload["id"], item["assetId"], revision)
    return True

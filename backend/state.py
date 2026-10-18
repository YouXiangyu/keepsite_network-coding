"""Build account snapshots and the shared HTTP state response."""
import copy
import json

from .jobs import engine_status
from .store import Store, StoreError


def _decorate_content(store: Store, db, user_id: str, original):
    item = copy.deepcopy(original)
    for key in ("customImage", "modelUrl", "modelCamera", "generationJobId", "generationError"):
        item.pop(key, None)
    if item.get("assetId"):
        store.asset(db, user_id, item["assetId"])
        item["customImage"] = f"/api/media/{item['assetId']}"
    if item.get("modelId"):
        model = store.model(db, user_id, item["modelId"])
        if model["asset_id"] != item.get("assetId"):
            raise StoreError(409, "模型与图片引用不一致。")
        item["generation"] = "ready"
        item["modelUrl"] = f"/api/models/{model['id']}/model.glb"
        item["modelCamera"] = json.loads(model["camera"])
    return item


def _present_item(store: Store, db, user_id: str, row):
    item = json.loads(row["payload"])
    item.update(version=row["version"], assetId=row["asset_id"], generation="queued")
    item.pop("modelId", None)
    if row["model_id"]:
        item["modelId"] = row["model_id"]
    item = _decorate_content(store, db, user_id, item)
    if row["job_id"]:
        job = store.job(db, user_id, row["job_id"])
        item["generation"] = job["status"]
        item["generationJobId"] = job["id"]
        if job["error"]:
            item["generationError"] = job["error"]
        if job["status"] != "ready":
            item.pop("modelUrl", None)
            item.pop("modelCamera", None)
    return item


def account_state(store: Store, db, user_id: str):
    user = store.require_user(db, user_id)
    server = db.execute("SELECT value FROM meta WHERE key='server_id'").fetchone()[0]
    collections = [_present_item(store, db, user_id, row) for row in db.execute("SELECT * FROM collections WHERE user_id=? ORDER BY rowid", (user_id,))]
    shares = []
    for row in db.execute("SELECT payload FROM shares WHERE user_id=? ORDER BY rowid", (user_id,)):
        share = json.loads(row[0])
        share["item"] = _decorate_content(store, db, user_id, share["item"])
        shares.append(share)
    community = [_decorate_content(store, db, user_id, json.loads(row[0])) for row in db.execute("SELECT payload FROM community WHERE user_id=? ORDER BY rowid", (user_id,))]
    profile = db.execute("SELECT payload FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    return {"initialized": True, "serverId": server, "userId": user_id, "revision": user["revision"], "data": {
        "collections": collections, "shares": shares, "community": community,
        "profile": json.loads(profile[0]),
        "likedCommunityIds": [row[0] for row in db.execute("SELECT id FROM likes WHERE user_id=? ORDER BY rowid", (user_id,))],
    }}


def state_response(store: Store, worker, db, user_id: str, *, changed=False):
    if changed:
        store.bump(db, user_id)
    response = account_state(store, db, user_id)
    response["generationEngine"] = engine_status()
    response["generationRecovery"] = worker.recovery_for(user_id)
    return response

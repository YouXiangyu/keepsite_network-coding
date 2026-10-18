"""Atomically initialize a verified account from the immutable template."""
import copy

from .store import Store, StoreError, encode, now_ms


def ensure_account(store: Store, user_id: str, email: str, name: str):
    if not isinstance(user_id, str) or not user_id or len(user_id) > 512:
        raise StoreError(401, "认证身份无效。")
    with store.transaction() as db:
        if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            return
    if store.template is None:
        raise StoreError(503, "账户初始化模板尚未配置。")
    # Potentially slow object transfer is outside the SQL write transaction.
    store.template.prepare_objects(store.storage)
    with store.transaction(True) as db:
        if db.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            return
        display_name = (name or email.partition("@")[0] or "新朋友").strip()[:24] or "新朋友"
        db.execute("INSERT INTO users VALUES (?,?,?,?,?,?)", (user_id, email, name, 1, store.template.version, now_ms()))
        for asset in store.template.assets.values():
            store.register_asset(db, asset)
            db.execute("INSERT INTO asset_grants VALUES (?,?)", (user_id, asset["id"]))
        for model in store.template.models.values():
            store.register_model(db, model)
            db.execute("INSERT INTO model_grants VALUES (?,?)", (user_id, model["id"]))
        initial = copy.deepcopy(store.template.data)
        initial["profile"]["name"] = display_name
        db.execute("INSERT INTO profiles VALUES (?,?)", (user_id, encode(initial["profile"])))
        for item in initial["collections"]:
            payload = {key: value for key, value in item.items() if key not in {"assetId", "modelId", "generation", "version"}}
            db.execute("INSERT INTO collections(user_id,id,payload,asset_id,model_id,version) VALUES (?,?,?,?,?,?)", (user_id, item["id"], encode(payload), item["assetId"], item["modelId"], item.get("version", 1)))
        for share in initial["shares"]:
            db.execute("INSERT INTO shares VALUES (?,?,?)", (user_id, share["id"], encode(share)))
        for item in initial["community"]:
            db.execute("INSERT INTO community VALUES (?,?,?)", (user_id, item["id"], encode(item)))
        for identifier in initial["likedCommunityIds"]:
            db.execute("INSERT INTO likes VALUES (?,?)", (user_id, identifier))

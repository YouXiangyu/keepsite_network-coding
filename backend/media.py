from __future__ import annotations

import hashlib
import io
import warnings

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
FORMATS = {"PNG": ("image/png", ".png"), "JPEG": ("image/jpeg", ".jpg"), "WEBP": ("image/webp", ".webp")}


def image_info(raw: bytes):
    if len(raw) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "图片不能超过 12 MB。")
    if not raw:
        raise HTTPException(422, "图片内容为空。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as im:
                fmt, width, height = im.format, im.width, im.height
                if fmt not in FORMATS or width * height > MAX_IMAGE_PIXELS:
                    raise HTTPException(422, "请选择有效的 JPG、PNG 或 WebP 图片，且像素不超过 4000 万。")
                im.verify()
            with Image.open(io.BytesIO(raw)) as im:
                im.load()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, "无法解码这张图片，请选择完整的 JPG、PNG 或 WebP 文件。")
    mime, suffix = FORMATS[fmt]
    digest = hashlib.sha256(raw).hexdigest()
    return {"assetId": digest, "url": f"/api/media/{digest}", "sha256": digest, "size": len(raw), "width": width, "height": height, "mimeType": mime, "filename": digest + suffix}



def persist_image(store, user_id: str, raw: bytes):
    """Validate and store bytes before a short account-grant transaction."""
    with store.transaction() as db:
        store.require_user(db, user_id)
    info = image_info(raw)
    descriptor = {
        "id": info["assetId"], "path": "media/" + info["filename"],
        "mimeType": info["mimeType"], "size": info["size"],
        "width": info["width"], "height": info["height"], "sha256": info["sha256"],
    }
    store.storage.put_bytes(descriptor["path"], raw, descriptor["sha256"], descriptor["mimeType"])
    with store.transaction(True) as db:
        store.require_user(db, user_id)
        store.register_asset(db, descriptor)
        changed = db.execute("INSERT OR IGNORE INTO asset_grants VALUES (?,?)", (user_id, descriptor["id"])).rowcount
        if changed:
            store.bump(db, user_id)
    return {key: value for key, value in info.items() if key != "filename"}

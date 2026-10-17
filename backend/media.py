from __future__ import annotations

import base64
import hashlib
import io
import os
import tempfile
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


def persist_image(store, db, raw: bytes):
    from .store import now_ms
    info = image_info(raw)
    path = store.root / "media" / info["filename"]
    if not path.exists():
        fd, temporary = tempfile.mkstemp(prefix="upload-", dir=store.root / "media")
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    db.execute("INSERT OR IGNORE INTO assets VALUES (?,?,?,?,?,?,?)", (info["assetId"], info["mimeType"], info["filename"], info["size"], info["width"], info["height"], now_ms()))
    return {key: value for key, value in info.items() if key != "filename"}


def import_data_url(store, db, value: str):
    if not value.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")):
        raise HTTPException(422, "旧图片必须是本地 JPG、PNG 或 WebP 数据，未读取外部图片地址。")
    encoded = value.split(",", 1)[1]
    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
        raise HTTPException(413, "旧图片超过 12 MB，未覆盖原收藏。")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        raise HTTPException(422, "旧图片编码损坏，未覆盖原收藏。")
    return persist_image(store, db, raw)

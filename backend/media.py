from __future__ import annotations

import hashlib
import io
import warnings
import sqlite3
import logging
import threading

from .object_storage import StorageError
from .store import StoreError, now_ms

from fastapi import HTTPException
from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
AUTO_RECOVERY_WINDOW_MS = 24 * 60 * 60 * 1000
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





def _descriptor(intent):
    return {"id": intent["asset_id"], "path": intent["object_key"], "mimeType": intent["mime"],
            "size": intent["size"], "width": intent["width"], "height": intent["height"],
            "sha256": intent["asset_id"]}


def _asset_response(intent):
    return {"assetId": intent["asset_id"], "sha256": intent["asset_id"],
            "url": f"/api/media/{intent['asset_id']}", "mimeType": intent["mime"],
            "size": intent["size"], "width": intent["width"], "height": intent["height"]}


def _intent(store, user_id, upload_id):
    with store.transaction() as db:
        store.require_user(db, user_id)
        row = db.execute("SELECT * FROM upload_intents WHERE user_id=? AND id=?", (user_id, upload_id)).fetchone()
        if not row:
            raise StoreError(404, "上传记录不存在或无权访问。")
        if row["storage_identity"] != store.storage.cache_identity:
            raise StoreError(409, "存储配置已改变，请核对原上传或重新选择照片。")
        return dict(row)


def _complete_upload(store, user_id, upload_id):
    """Publish only a verified object; never recreate a consumed receipt."""
    with store.transaction(True) as db:
        row = db.execute("SELECT * FROM upload_intents WHERE user_id=? AND id=?", (user_id, upload_id)).fetchone()
        if not row or row["storage_identity"] != store.storage.cache_identity:
            raise StoreError(409, "上传记录已改变，请重新核对。")
        if row["status"] == "pending":
            store.register_asset(db, _descriptor(row))
            db.execute("INSERT OR IGNORE INTO asset_grants VALUES (?,?)", (user_id, row["asset_id"]))
            db.execute("INSERT OR IGNORE INTO upload_receipts VALUES (?,?)", (user_id, row["asset_id"]))
            db.execute("UPDATE upload_intents SET status='stored',updated_at=? WHERE user_id=? AND id=?", (now_ms(), user_id, upload_id))
        return dict(db.execute("SELECT * FROM upload_intents WHERE user_id=? AND id=?", (user_id, upload_id)).fetchone())


def persist_image(store, user_id: str, upload_id: str, raw: bytes):
    """Persist the validated upload intention before any object-storage I/O."""
    info = image_info(raw)
    stamp = now_ms()
    values = (info["assetId"], "media/" + info["filename"], info["mimeType"], info["size"], info["width"], info["height"])
    with store.transaction(True) as db:
        store.require_user(db, user_id)
        previous = db.execute("SELECT * FROM upload_intents WHERE user_id=? AND id=?", (user_id, upload_id)).fetchone()
        if previous:
            actual = tuple(previous[key] for key in ("asset_id", "object_key", "mime", "size", "width", "height"))
            if actual != values or previous["storage_identity"] != store.storage.cache_identity:
                raise StoreError(409, "上传标识已用于不同图片或存储配置。")
        else:
            db.execute("INSERT INTO upload_intents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (user_id, upload_id, *values, "pending", store.storage.cache_identity, stamp, stamp, 0))
    intent = _intent(store, user_id, upload_id)
    if intent["status"] != "attached":
        store.storage.put_bytes(intent["object_key"], raw, intent["asset_id"], intent["mime"])
        if not store.storage.verify(intent["object_key"], intent["size"], intent["asset_id"], intent["mime"]):
            raise StorageError("Uploaded object was not found during verification")
        intent = _complete_upload(store, user_id, upload_id)
    return {**_asset_response(intent), "uploadId": upload_id}


def upload_status(store, user_id: str, upload_id: str, *, stopping=lambda: False):
    intent = _intent(store, user_id, upload_id)
    if intent["status"] == "attached":
        return {"uploadId": upload_id, "status": "attached", "asset": _asset_response(intent)}
    with store.transaction(True) as db:
        db.execute("UPDATE upload_intents SET last_checked_at=? WHERE user_id=? AND id=?", (now_ms(), user_id, upload_id))
    found = store.storage.verify(intent["object_key"], intent["size"], intent["asset_id"], intent["mime"], stopping=stopping)
    if not found or stopping():
        return {"uploadId": upload_id, "status": "pending", "asset": None}
    intent = _complete_upload(store, user_id, upload_id)
    return {"uploadId": upload_id, "status": intent["status"], "asset": _asset_response(intent)}


def recover_pending_uploads(store, *, limit=3, stopping=lambda: False):
    """Bounded recovery within 24 hours; older intentions remain manually recoverable."""
    with store.transaction() as db:
        rows = db.execute("SELECT user_id,id FROM upload_intents WHERE status='pending' AND storage_identity=? AND created_at>=? ORDER BY last_checked_at,created_at,id LIMIT ?",
                          (store.storage.cache_identity, now_ms() - AUTO_RECOVERY_WINDOW_MS, max(0, min(limit, 20)))).fetchall()
    results = {"checked": 0, "stored": 0, "pending": 0, "failed": 0}
    for row in rows:
        if stopping():
            break
        results["checked"] += 1
        try:
            result = upload_status(store, row["user_id"], row["id"], stopping=stopping)
            results["stored" if result["status"] in {"stored", "attached"} else "pending"] += 1
        except (StorageError, StoreError, sqlite3.Error):
            results["failed"] += 1
    return results


class UploadRecovery:
    """Small periodic reconciliation batches; no OCR or generation worker."""
    def __init__(self, store, *, interval=30):
        self.store, self.interval = store, interval
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._run, name="keepsite-upload-recovery", daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        while not self.stopping.is_set():
            try:
                recover_pending_uploads(self.store, stopping=self.stopping.is_set)
            except sqlite3.Error:
                logging.getLogger(__name__).warning("Upload reconciliation database temporarily unavailable")
            if self.stopping.wait(self.interval):
                break

    def stop(self):
        self.stopping.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=20)
        if self.thread.is_alive():
            logging.getLogger(__name__).warning("Upload reconciliation is finishing an in-flight storage request")

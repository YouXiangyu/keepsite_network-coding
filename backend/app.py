from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import unicodedata
import uuid
import zipfile
from collections import Counter
from contextlib import asynccontextmanager, closing
from datetime import date as Date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .jobs import JobWorker, engine_status
from .media import MAX_IMAGE_BYTES, persist_image
from .object_storage import ObjectMissing, StorageError, configured_store, object_key
from .store import Store, StoreError, encode, now_ms, require_schema2_root
from .template import InitialTemplate

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger(__name__)

Category = Literal["movie", "travel", "daily", "exhibition"]
Identifier = Annotated[str, Field(min_length=1, max_length=120, pattern=r"^[A-Za-z0-9_-]+$")]
Tags = Annotated[list[Annotated[str, Field(min_length=1, max_length=100)]], Field(max_length=30)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class CollectionFields(Model):
    title: Annotated[str, Field(min_length=1, max_length=70)]
    category: Category
    tags: Tags | None = None
    note: Annotated[str, Field(max_length=500)] = ""
    date: str
    art: Annotated[str, Field(min_length=1, max_length=80)]

    @field_validator("date")
    @classmethod
    def valid_date(cls, value):
        if Date.fromisoformat(value).isoformat() != value:
            raise ValueError("日期应为 YYYY-MM-DD。")
        return value


class CreateCollection(CollectionFields):
    id: Identifier
    assetId: Identifier


class UpdateCollection(Model):
    version: Annotated[int, Field(ge=1)]
    title: Annotated[str, Field(min_length=1, max_length=70)]
    category: Category
    tags: Tags
    note: Annotated[str, Field(max_length=500)]
    art: Annotated[str, Field(min_length=1, max_length=80)]
    date: str | None = None
    assetId: Identifier | None = None

    @field_validator("date")
    @classmethod
    def valid_date(cls, value):
        if value is not None and Date.fromisoformat(value).isoformat() != value:
            raise ValueError("日期应为 YYYY-MM-DD。")
        return value


class Version(Model):
    version: Annotated[int, Field(ge=1)]


class Profile(Model):
    name: Annotated[str, Field(min_length=1, max_length=24)]
    bio: Annotated[str, Field(max_length=100)] = ""


class Like(Model):
    liked: bool



def current_user(request: Request):
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict) or not isinstance(user.get("id"), str):
        raise StoreError(401, "请先登录。")
    return user["id"]


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


def create_app(data_dir: Path | None = None, run_worker: bool = True, *, auth_provider=None, object_store=None, template_dir: Path | None = None) -> FastAPI:
    root = Path(data_dir or os.environ.get("MEMOVERSE_DATA_DIR", ROOT / "backend/data-v2"))
    require_schema2_root(root)
    template = InitialTemplate(Path(template_dir or os.environ.get("MEMOVERSE_TEMPLATE_DIR", ROOT / ".cache/initial-template-v1")))
    storage = object_store if object_store is not None else configured_store(root)
    store = Store(root, storage=storage, template=template)
    worker = JobWorker(store)

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(template.prepare_objects, storage)
        if run_worker:
            worker.start()
        try:
            yield
        finally:
            if run_worker:
                worker.stop()

    app = FastAPI(title="MemoVerse Account API", version="0.3.0", lifespan=lifespan)
    app.state.store, app.state.worker = store, worker

    @app.exception_handler(StoreError)
    async def business_error(request, error):
        return JSONResponse({"detail": error.message}, status_code=error.status_code)

    @app.exception_handler(StorageError)
    async def storage_error(request, error):
        log.warning("Object storage operation failed: %s", type(error).__name__)
        return JSONResponse({"detail": "媒体暂时无法读取，记录仍保留，请稍后重试。"}, status_code=404 if isinstance(error, ObjectMissing) else 503)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return JSONResponse({"detail": "提交的信息不完整或格式不正确。"}, status_code=422)

    @app.middleware("http")
    async def request_limits(request: Request, call_next):
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            length = request.headers.get("content-length", "0")
            maximum = MAX_IMAGE_BYTES + 1024 * 1024 if request.url.path == "/api/media" else 2 * 1024 * 1024
            if length.isdigit() and int(length) > maximum:
                return JSONResponse({"detail": "请求内容过大，原数据未修改。"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, private"
        return response

    def state_for(db, user_id, *, changed=False):
        if changed:
            store.bump(db, user_id)
        response = store.state(db, user_id)
        response["generationEngine"] = engine_status()
        response["generationRecovery"] = worker.recovery_for(user_id)
        return response

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/state")
    def state(request: Request):
        with store.transaction() as db:
            return state_for(db, current_user(request))

    @app.post("/api/media")
    async def upload(request: Request, file: UploadFile = File()):
        user_id = current_user(request)
        try:
            raw = await file.read(MAX_IMAGE_BYTES + 1)
            return await run_in_threadpool(persist_image, store, user_id, raw)
        finally:
            await file.close()

    def stream_object(key, content_type):
        reader = storage.open(key)
        return StreamingResponse(reader.chunks(), media_type=content_type,
                                 headers={"Content-Length": str(reader.size), "Cache-Control": "no-store, private"},
                                 background=BackgroundTask(reader.close))

    @app.get("/api/media/{asset_id}")
    def media(request: Request, asset_id: str):
        with store.transaction() as db:
            asset = dict(store.asset(db, current_user(request), asset_id))
        return stream_object(asset["object_key"], asset["mime"])

    @app.get("/api/models/{identifier}/model.glb")
    def model(request: Request, identifier: str):
        with store.transaction() as db:
            row = dict(store.model(db, current_user(request), identifier))
        return stream_object(row["model_key"], "model/gltf-binary")

    @app.post("/api/collections")
    def create(request: Request, body: CreateCollection):
        user_id = current_user(request)
        fingerprint = hashlib.sha256(encode(body.model_dump()).encode()).hexdigest()
        with store.transaction(True) as db:
            existing = db.execute("SELECT * FROM collections WHERE user_id=? AND id=?", (user_id, body.id)).fetchone()
            if existing:
                if existing["create_fingerprint"] == fingerprint:
                    return state_for(db, user_id)
                raise StoreError(409, "这件收藏已创建且内容不同，请刷新后编辑。")
            store.asset(db, user_id, body.assetId)
            payload = body.model_dump(exclude={"assetId"})
            payload["tags"] = list(dict.fromkeys(body.tags)) if body.tags is not None else infer_tag(db, user_id, body.title)
            payload.update(addedAt=now_ms(), source="mine")
            revision = db.execute("SELECT COALESCE(MAX(input_revision),0)+1 FROM jobs WHERE user_id=? AND collection_id=?", (user_id, body.id)).fetchone()[0]
            db.execute("INSERT INTO collections(user_id,id,payload,asset_id,create_fingerprint,input_revision) VALUES (?,?,?,?,?,?)", (user_id, body.id, encode(payload), body.assetId, fingerprint, revision))
            store.enqueue(db, user_id, body.id, body.assetId, revision)
            result = state_for(db, user_id, changed=True)
        worker.wake()
        return result

    @app.patch("/api/collections/{identifier}")
    def update(request: Request, identifier: str, body: UpdateCollection):
        user_id = current_user(request)
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
                store.enqueue(db, user_id, identifier, asset_id, revision)
            result = state_for(db, user_id, changed=True)
        if regenerate and row["job_id"]:
            worker.cancel(user_id, row["job_id"])
            result["generationRecovery"] = worker.recovery_for(user_id)
        worker.wake()
        return result

    @app.delete("/api/collections/{identifier}")
    def delete(request: Request, identifier: str, body: Version):
        user_id = current_user(request)
        with store.transaction(True) as db:
            row = store.collection(db, user_id, identifier)
            if row["version"] != body.version:
                raise StoreError(409, "收藏已更新，请刷新后再删除。")
            db.execute("DELETE FROM collections WHERE user_id=? AND id=?", (user_id, identifier))
            db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE user_id=? AND collection_id=? AND status!='ready'", (now_ms(), user_id, identifier))
            result = state_for(db, user_id, changed=True)
        if row["job_id"]:
            worker.cancel(user_id, row["job_id"])
            result["generationRecovery"] = worker.recovery_for(user_id)
        return result

    @app.post("/api/collections/{identifier}/retry-generation")
    def retry(request: Request, identifier: str):
        user_id = current_user(request)
        with store.transaction(True) as db:
            row = store.collection(db, user_id, identifier)
            if not row["job_id"]:
                raise StoreError(422, "此模板模型已经就绪，无需重新生成。")
            job = store.job(db, user_id, row["job_id"])
            pending = worker.has_completion(user_id, job["id"])
            changed = not pending and job["status"] in {"failed", "blocked"}
            if changed:
                db.execute("UPDATE jobs SET status='queued',error='',updated_at=? WHERE user_id=? AND id=?", (now_ms(), user_id, job["id"]))
            result = state_for(db, user_id, changed=changed)
        if pending:
            worker.retry_publication(user_id, job["id"])
            result["generationRecovery"] = worker.recovery_for(user_id)
        worker.wake()
        return result

    @app.get("/api/jobs/{identifier}")
    def job_detail(request: Request, identifier: str):
        user_id = current_user(request)
        with store.transaction() as db:
            job = store.job(db, user_id, identifier)
            result = {"id": job["id"], "collectionId": job["collection_id"], "status": job["status"], "error": job["error"], "createdAt": job["created_at"], "updatedAt": job["updated_at"], "inputRevision": job["input_revision"]}
            if job["status"] == "ready" and job["model_id"]:
                store.model(db, user_id, job["model_id"])
                result["modelUrl"] = f"/api/models/{job['model_id']}/model.glb"
            return result

    @app.patch("/api/profile")
    def profile(request: Request, body: Profile):
        user_id = current_user(request)
        with store.transaction(True) as db:
            store.require_user(db, user_id)
            db.execute("UPDATE profiles SET payload=? WHERE user_id=?", (encode(body.model_dump()), user_id))
            return state_for(db, user_id, changed=True)

    @app.post("/api/shares/read-all")
    def read_all(request: Request):
        user_id = current_user(request)
        with store.transaction(True) as db:
            changed = False
            for row in db.execute("SELECT * FROM shares WHERE user_id=?", (user_id,)).fetchall():
                payload = json.loads(row["payload"])
                if payload["unread"]:
                    payload["unread"] = False
                    db.execute("UPDATE shares SET payload=? WHERE user_id=? AND id=?", (encode(payload), user_id, row["id"]))
                    changed = True
            return state_for(db, user_id, changed=changed)

    def share_record(db, user_id, identifier):
        row = db.execute("SELECT payload FROM shares WHERE user_id=? AND id=?", (user_id, identifier)).fetchone()
        if not row:
            raise StoreError(404, "分享不存在或无权访问。")
        return json.loads(row[0])

    @app.post("/api/shares/{identifier}/read")
    def read_share(request: Request, identifier: str):
        user_id = current_user(request)
        with store.transaction(True) as db:
            payload = share_record(db, user_id, identifier)
            changed = payload["unread"]
            if changed:
                payload["unread"] = False
                db.execute("UPDATE shares SET payload=? WHERE user_id=? AND id=?", (encode(payload), user_id, identifier))
            return state_for(db, user_id, changed=changed)

    def collect_item(db, user_id, item):
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
            store.enqueue(db, user_id, payload["id"], item["assetId"], revision)
        return True

    @app.post("/api/shares/{identifier}/collect")
    def collect_share(request: Request, identifier: str):
        user_id = current_user(request)
        with store.transaction(True) as db:
            changed = collect_item(db, user_id, share_record(db, user_id, identifier)["item"])
            result = state_for(db, user_id, changed=changed)
        worker.wake()
        return result

    def community_record(db, user_id, identifier):
        row = db.execute("SELECT payload FROM community WHERE user_id=? AND id=?", (user_id, identifier)).fetchone()
        if not row:
            raise StoreError(404, "社区示例不存在或无权访问。")
        return json.loads(row[0])

    @app.post("/api/community/{identifier}/view")
    def view_community(request: Request, identifier: str):
        user_id = current_user(request)
        with store.transaction(True) as db:
            payload = community_record(db, user_id, identifier)
            payload["exposures"] += 1
            db.execute("UPDATE community SET payload=? WHERE user_id=? AND id=?", (encode(payload), user_id, identifier))
            return state_for(db, user_id, changed=True)

    @app.put("/api/community/{identifier}/like")
    def like_community(request: Request, identifier: str, body: Like):
        user_id = current_user(request)
        with store.transaction(True) as db:
            payload = community_record(db, user_id, identifier)
            liked = bool(db.execute("SELECT 1 FROM likes WHERE user_id=? AND id=?", (user_id, identifier)).fetchone())
            if liked == body.liked:
                return state_for(db, user_id)
            if body.liked:
                db.execute("INSERT INTO likes VALUES (?,?)", (user_id, identifier))
            else:
                db.execute("DELETE FROM likes WHERE user_id=? AND id=?", (user_id, identifier))
            payload["likes"] = max(0, payload["likes"] + (1 if body.liked else -1))
            db.execute("UPDATE community SET payload=? WHERE user_id=? AND id=?", (encode(payload), user_id, identifier))
            return state_for(db, user_id, changed=True)

    @app.post("/api/community/{identifier}/collect")
    def collect_community(request: Request, identifier: str):
        user_id = current_user(request)
        with store.transaction(True) as db:
            payload = community_record(db, user_id, identifier)
            item = {"id": f"community-{identifier}", "title": payload["title"], "category": payload["category"], "art": payload["art"], "tags": [], "note": payload["description"][:500], "source": "friend", "sender": payload["author"], "date": Date.today().isoformat(), "assetId": payload["assetId"], "modelId": payload.get("modelId")}
            changed = collect_item(db, user_id, item)
            if changed:
                payload["saves"] += 1
                db.execute("UPDATE community SET payload=? WHERE user_id=? AND id=?", (encode(payload), user_id, identifier))
            return state_for(db, user_id, changed=changed)

    @app.get("/api/backup")
    def backup(request: Request):
        user_id = current_user(request)
        snapshot, assets, models, collection_rows, job_rows = store.archive_snapshot(user_id)
        descriptor, name = tempfile.mkstemp(prefix="account-backup-", suffix=".zip", dir=store.root / "exports")
        os.close(descriptor)
        archive = Path(name)
        try:
            files = {}
            for asset in assets.values():
                files[object_key(asset["object_key"])] = {"path": object_key(asset["object_key"]), "sha256": asset["id"], "size": asset["size"]}
            for model in models.values():
                for file in json.loads(model["files"]):
                    key = object_key(file["path"])
                    files[key] = {"path": key, "size": file["size"], "sha256": file["sha256"]}
            # SQL transaction has already closed. All object reads are private,
            # streamed and closed, including failures while constructing the ZIP.
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for key, record in sorted(files.items()):
                    digest, size = hashlib.sha256(), 0
                    with closing(storage.open(key)) as incoming, bundle.open(record["path"], "w") as outgoing:
                        while chunk := incoming.read():
                            outgoing.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    if size != record["size"] or digest.hexdigest() != record["sha256"]:
                        raise StorageError("Backup object integrity check failed")
                data = snapshot["data"]
                for item in [*data["collections"], *[share["item"] for share in data["shares"]], *data["community"]]:
                    if item.get("assetId"):
                        item["customImage"] = assets[item["assetId"]]["object_key"]
                    if item.get("modelId") and item.get("generation") == "ready":
                        item["modelUrl"] = models[item["modelId"]]["model_key"]
                bundle.writestr("manifest.json", encode({"format": "memoverse-account-backup", "version": 2, "createdAt": now_ms(), **snapshot,
                                                        "files": list(files.values()), "collections": collection_rows, "jobs": job_rows,
                                                        "assets": list(assets.values()), "models": list(models.values())}))
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
        return FileResponse(archive, media_type="application/zip", filename="memoverse-account-backup.zip", background=BackgroundTask(archive.unlink, missing_ok=True))

    if auth_provider is None:
        from .auth import install_auth
        auth_provider = install_auth
    # No runtime flag disables authentication. Tests explicitly supply a named
    # factory which implements authenticated sessions, not arbitrary user headers.
    app.state.auth = auth_provider(app, root, on_authenticated=lambda user: store.ensure_account(user["id"], user["email"], user.get("name", "")))
    return app

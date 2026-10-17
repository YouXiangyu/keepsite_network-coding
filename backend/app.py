from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
import uuid
import zipfile
from collections import Counter
from contextlib import asynccontextmanager
from datetime import date as Date
from pathlib import Path
from typing import Annotated, Literal

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .jobs import JobWorker, engine_status
from .media import MAX_IMAGE_BYTES, import_data_url, persist_image
from .store import Store, encode, now_ms

ROOT = Path(__file__).resolve().parents[1]
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


class LegacyItem(CollectionFields):
    id: Identifier
    version: Annotated[int, Field(ge=1)] = 1
    tags: Tags
    addedAt: Annotated[int, Field(ge=0)]
    source: Literal["mine", "friend"]
    sender: Annotated[str, Field(max_length=100)] | None = None
    customImage: str | None = None
    generation: Literal["reference", "pending"]


class LegacyShare(Model):
    id: Identifier
    item: LegacyItem
    sender: Annotated[str, Field(min_length=1, max_length=100)]
    receivedAt: str
    unread: bool


class Community(Model):
    id: Identifier
    title: Annotated[str, Field(min_length=1, max_length=70)]
    category: Category
    art: Annotated[str, Field(min_length=1, max_length=80)]
    author: Annotated[str, Field(max_length=100)]
    description: Annotated[str, Field(max_length=1000)]
    exposures: Annotated[int, Field(ge=0)]
    saves: Annotated[int, Field(ge=0)]
    likes: Annotated[int, Field(ge=0)]


class LegacyData(Model):
    collections: list[LegacyItem]
    shares: list[LegacyShare]
    community: list[Community]
    profile: Profile
    likedCommunityIds: list[Identifier]


class Bootstrap(Model):
    migrationId: Identifier
    data: LegacyData


def require_initialized(store, db):
    if store.meta(db, "initialized") != "1":
        raise HTTPException(409, "本机工作区尚未完成初始化。")


def require_item(db, identifier):
    row = db.execute("SELECT * FROM collections WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise HTTPException(404, "这件收藏已不存在，请刷新列表。")
    return row


def require_asset(db, identifier):
    row = db.execute("SELECT * FROM assets WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise HTTPException(404, "图片尚未上传或已不存在，请重新选择图片。")
    return row


def infer_tag(db, title):
    normalize = lambda value: unicodedata.normalize("NFKC", value).strip().lower()
    query = normalize(title)
    keywords = [part for part in re.split(r"[\W_]+", query) if part]
    counts = Counter()
    for row in db.execute("SELECT payload FROM collections ORDER BY rowid"):
        item = json.loads(row[0])
        name = normalize(item["title"])
        if name and (query in name or name in query or any(part in name for part in keywords)):
            counts.update(list(dict.fromkeys(item["tags"])))
    return [counts.most_common(1)[0][0]] if counts else []


def create_app(data_dir: Path | None = None, run_worker: bool = True) -> FastAPI:
    store = Store(data_dir or Path(os.environ.get("MEMOVERSE_DATA_DIR", ROOT / "backend" / "data")))
    worker = JobWorker(store)

    @asynccontextmanager
    async def lifespan(app):
        if run_worker:
            worker.start()
        try:
            yield
        finally:
            if run_worker:
                worker.stop()

    app = FastAPI(title="MemoVerse Local API", version="0.2.0", lifespan=lifespan)
    app.state.store, app.state.worker = store, worker
    origins = os.environ.get("MEMOVERSE_ALLOWED_ORIGINS", "http://127.0.0.1:4173,http://localhost:4173").split(",")
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"], allow_headers=["Content-Type", "X-MemoVerse-Client"])

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": "提交的信息不完整或格式不正确，请检查图片、名称、标签和日期。"}, status_code=422)

    @app.middleware("http")
    async def local_guard(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return JSONResponse({"detail": "当前来源不允许访问本机工作区。"}, status_code=403)
        if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
            if request.headers.get("X-MemoVerse-Client") != "local":
                return JSONResponse({"detail": "缺少本机客户端请求标识。"}, status_code=403)
            length = request.headers.get("content-length", "0")
            maximum = MAX_IMAGE_BYTES + 1024 * 1024 if request.url.path == "/api/media" else 128 * 1024 * 1024
            if length.isdigit() and int(length) > maximum:
                return JSONResponse({"detail": "请求内容过大，原数据未修改。"}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def mutation_state(db):
        store.bump(db)
        return store.state(db)

    @app.get("/api/health")
    def health():
        status = engine_status()
        return {"status": "ok", "mode": "local", "database": "sqlite", "storage": "local-files", "generation": {"engine": "AnyImage", **status}}

    @app.get("/api/state")
    def state():
        with store.transaction() as db:
            return store.state(db)

    @app.post("/api/bootstrap")
    def bootstrap(body: Bootstrap):
        with store.transaction(True) as db:
            if db.execute("SELECT 1 FROM migrations WHERE id=?", (body.migrationId,)).fetchone():
                return store.state(db)
            if store.meta(db, "initialized") == "1":
                raise HTTPException(409, "后端已有数据，此浏览器的旧收藏尚未迁移。两份数据均已保留，请先核对，不能直接覆盖。")
            old = body.data
            for entries in (old.collections, old.shares, old.community):
                ids = [entry.id for entry in entries]
                if len(ids) != len(set(ids)):
                    raise HTTPException(422, "旧数据含重复标识，未导入任何收藏。")
            community_ids = {entry.id for entry in old.community}
            if any(key not in community_ids for key in old.likedCommunityIds):
                raise HTTPException(422, "喜欢记录引用了不存在的内容。")

            def migrate_item(item):
                payload = item.model_dump(exclude_none=True)
                image = payload.pop("customImage", None)
                payload.pop("generation", None)
                payload.pop("version", None)
                asset_id = import_data_url(store, db, image)["assetId"] if image else None
                return payload, asset_id

            for original in old.collections:
                payload, asset_id = migrate_item(original)
                db.execute("INSERT INTO collections(id,payload,asset_id) VALUES (?,?,?)", (original.id, encode(payload), asset_id))
                if asset_id:
                    store.enqueue(db, original.id, asset_id, 1)
            for original in old.shares:
                payload = original.model_dump(exclude_none=True)
                item, asset_id = migrate_item(original.item)
                item["generation"], item["version"] = "reference", 1
                if asset_id:
                    item["assetId"] = asset_id
                    item["customImage"] = f"/api/media/{asset_id}"
                payload["item"] = item
                db.execute("INSERT INTO shares VALUES (?,?)", (original.id, encode(payload)))
            for original in old.community:
                db.execute("INSERT INTO community VALUES (?,?)", (original.id, encode(original.model_dump())))
            for identifier in set(old.likedCommunityIds):
                db.execute("INSERT INTO likes VALUES (?)", (identifier,))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('profile',?)", (encode(old.profile.model_dump()),))
            db.execute("UPDATE meta SET value='1' WHERE key='initialized'")
            db.execute("INSERT INTO migrations VALUES (?,?)", (body.migrationId, now_ms()))
            result = mutation_state(db)
        worker.wake()
        return result

    @app.post("/api/media")
    async def upload(file: UploadFile = File()):
        try:
            raw = await file.read(MAX_IMAGE_BYTES + 1)
            def persist():
                with store.transaction(True) as db:
                    require_initialized(store, db)
                    return persist_image(store, db, raw)
            return await run_in_threadpool(persist)
        finally:
            await file.close()

    @app.get("/api/media/{asset_id}")
    def media(asset_id: str):
        with store.transaction() as db:
            row = require_asset(db, asset_id)
        path = store.root / "media" / row["filename"]
        if not path.is_file():
            raise HTTPException(404, "本地图片文件缺失，收藏记录仍保留。")
        return FileResponse(path, media_type=row["mime"])

    @app.post("/api/collections")
    def create(body: CreateCollection):
        fingerprint = hashlib.sha256(encode(body.model_dump()).encode()).hexdigest()
        with store.transaction(True) as db:
            require_initialized(store, db)
            existing = db.execute("SELECT * FROM collections WHERE id=?", (body.id,)).fetchone()
            if existing:
                if existing["create_fingerprint"] == fingerprint:
                    return store.state(db)
                raise HTTPException(409, "这件收藏已创建且内容不同，请刷新后编辑。")
            require_asset(db, body.assetId)
            payload = body.model_dump(exclude={"assetId"})
            payload["tags"] = list(dict.fromkeys(body.tags)) if body.tags is not None else infer_tag(db, body.title)
            payload.update(addedAt=now_ms(), source="mine")
            input_revision = db.execute("SELECT COALESCE(MAX(input_revision),0)+1 FROM jobs WHERE collection_id=?", (body.id,)).fetchone()[0]
            db.execute("INSERT INTO collections(id,payload,asset_id,create_fingerprint,input_revision) VALUES (?,?,?,?,?)", (body.id, encode(payload), body.assetId, fingerprint, input_revision))
            store.enqueue(db, body.id, body.assetId, input_revision)
            result = mutation_state(db)
        worker.wake()
        return result

    @app.patch("/api/collections/{identifier}")
    def update(identifier: str, body: UpdateCollection):
        with store.transaction(True) as db:
            row = require_item(db, identifier)
            if row["version"] != body.version:
                raise HTTPException(409, "这件收藏已在其他页面更新，草稿仍保留，请重新读取后再编辑。")
            asset_id = row["asset_id"]
            if "assetId" in body.model_fields_set:
                if not body.assetId:
                    raise HTTPException(422, "收藏必须保留图片。")
                require_asset(db, body.assetId)
                asset_id = body.assetId
            payload = json.loads(row["payload"])
            regenerate = asset_id != row["asset_id"] or body.art != payload["art"]
            changes = body.model_dump(exclude={"version", "assetId"}, exclude_none=True)
            changes["tags"] = list(dict.fromkeys(changes["tags"]))
            payload.update(changes)
            input_revision = row["input_revision"] + int(regenerate)
            db.execute("UPDATE collections SET payload=?,asset_id=?,version=version+1,input_revision=? WHERE id=?", (encode(payload), asset_id, input_revision, identifier))
            if regenerate and asset_id:
                if row["job_id"]:
                    db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE id=? AND status!='ready'", (now_ms(), row["job_id"]))
                store.enqueue(db, identifier, asset_id, input_revision)
            result = mutation_state(db)
        worker.wake()
        return result

    @app.delete("/api/collections/{identifier}")
    def delete(identifier: str, body: Version):
        with store.transaction(True) as db:
            row = require_item(db, identifier)
            if row["version"] != body.version:
                raise HTTPException(409, "收藏已更新，请刷新后再删除。")
            db.execute("DELETE FROM collections WHERE id=?", (identifier,))
            db.execute("UPDATE jobs SET status='cancelled',updated_at=? WHERE collection_id=? AND status!='ready'", (now_ms(), identifier))
            # Media are content-addressed and may be shared by independent received copies.
            return mutation_state(db)

    @app.post("/api/collections/{identifier}/retry-generation")
    def retry(identifier: str):
        with store.transaction(True) as db:
            row = require_item(db, identifier)
            if not row["job_id"]:
                raise HTTPException(422, "示例图片没有生成任务，请先导入真实图片。")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (row["job_id"],)).fetchone()
            if job["status"] in {"failed", "blocked"}:
                db.execute("UPDATE jobs SET status='queued',error='',updated_at=? WHERE id=?", (now_ms(), job["id"]))
                result = mutation_state(db)
            else:
                result = store.state(db)
        worker.wake()
        return result

    @app.get("/api/jobs/{identifier}")
    def job_detail(identifier: str):
        with store.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=?", (identifier,)).fetchone()
            if not job:
                raise HTTPException(404, "生成任务不存在。")
            result = {"id": job["id"], "collectionId": job["collection_id"], "status": job["status"], "error": job["error"], "createdAt": job["created_at"], "updatedAt": job["updated_at"], "inputRevision": job["input_revision"]}
            if job["status"] == "ready":
                result["modelUrl"] = f"/api/models/{identifier}/model.glb"
            return result

    @app.get("/api/models/{identifier}/model.glb")
    def model(identifier: str):
        with store.transaction() as db:
            job = db.execute("SELECT * FROM jobs WHERE id=? AND status='ready'", (identifier,)).fetchone()
            if not job:
                raise HTTPException(404, "模型尚未生成完成。")
        path = store.root / "models" / job["id"] / "model.glb"
        if not path.is_file():
            raise HTTPException(404, "本地模型文件缺失，原图已保留。")
        return FileResponse(path, media_type="model/gltf-binary")

    @app.patch("/api/profile")
    def profile(body: Profile):
        with store.transaction(True) as db:
            require_initialized(store, db)
            db.execute("INSERT OR REPLACE INTO meta VALUES ('profile',?)", (encode(body.model_dump()),))
            return mutation_state(db)

    @app.post("/api/shares/read-all")
    def read_all():
        with store.transaction(True) as db:
            require_initialized(store, db)
            changed = False
            for row in db.execute("SELECT * FROM shares").fetchall():
                payload = json.loads(row["payload"])
                if payload["unread"]:
                    payload["unread"] = False
                    db.execute("UPDATE shares SET payload=? WHERE id=?", (encode(payload), row["id"]))
                    changed = True
            return mutation_state(db) if changed else store.state(db)

    @app.post("/api/shares/{identifier}/read")
    def read_share(identifier: str):
        with store.transaction(True) as db:
            row = db.execute("SELECT * FROM shares WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise HTTPException(404, "分享不存在。")
            payload = json.loads(row["payload"])
            if not payload["unread"]:
                return store.state(db)
            payload["unread"] = False
            db.execute("UPDATE shares SET payload=? WHERE id=?", (encode(payload), identifier))
            return mutation_state(db)

    def collect_item(db, item):
        if db.execute("SELECT 1 FROM collections WHERE id=?", (item["id"],)).fetchone():
            return False
        payload = dict(item)
        asset_id = payload.pop("assetId", None)
        for field in ("customImage", "version", "generation", "generationJobId", "modelUrl", "generationError"):
            payload.pop(field, None)
        payload["addedAt"] = now_ms()
        input_revision = db.execute("SELECT COALESCE(MAX(input_revision),0)+1 FROM jobs WHERE collection_id=?", (payload["id"],)).fetchone()[0]
        db.execute("INSERT INTO collections(id,payload,asset_id,input_revision) VALUES (?,?,?,?)", (payload["id"], encode(payload), asset_id, input_revision))
        if asset_id:
            store.enqueue(db, payload["id"], asset_id, input_revision)
        return True

    @app.post("/api/shares/{identifier}/collect")
    def collect_share(identifier: str):
        with store.transaction(True) as db:
            row = db.execute("SELECT payload FROM shares WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise HTTPException(404, "分享不存在。")
            changed = collect_item(db, json.loads(row[0])["item"])
            result = mutation_state(db) if changed else store.state(db)
        worker.wake()
        return result

    def require_community(db, identifier):
        row = db.execute("SELECT payload FROM community WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise HTTPException(404, "示例内容不存在。")
        return json.loads(row[0])

    @app.post("/api/community/{identifier}/view")
    def view_community(identifier: str):
        with store.transaction(True) as db:
            payload = require_community(db, identifier)
            payload["exposures"] += 1
            db.execute("UPDATE community SET payload=? WHERE id=?", (encode(payload), identifier))
            return mutation_state(db)

    @app.put("/api/community/{identifier}/like")
    def like_community(identifier: str, body: Like):
        with store.transaction(True) as db:
            payload = require_community(db, identifier)
            liked = bool(db.execute("SELECT 1 FROM likes WHERE id=?", (identifier,)).fetchone())
            if liked == body.liked:
                return store.state(db)
            if body.liked:
                db.execute("INSERT INTO likes VALUES (?)", (identifier,))
            else:
                db.execute("DELETE FROM likes WHERE id=?", (identifier,))
            payload["likes"] = max(0, payload["likes"] + (1 if body.liked else -1))
            db.execute("UPDATE community SET payload=? WHERE id=?", (encode(payload), identifier))
            return mutation_state(db)

    @app.post("/api/community/{identifier}/collect")
    def collect_community(identifier: str):
        with store.transaction(True) as db:
            payload = require_community(db, identifier)
            item = {"id": f"community-{identifier}", "title": payload["title"], "category": payload["category"], "art": payload["art"], "tags": [], "note": payload["description"][:500], "source": "friend", "sender": payload["author"], "date": Date.today().isoformat()}
            changed = collect_item(db, item)
            if changed:
                payload["saves"] += 1
                db.execute("UPDATE community SET payload=? WHERE id=?", (encode(payload), identifier))
            return mutation_state(db) if changed else store.state(db)

    @app.get("/api/backup")
    def backup():
        archive = store.root / "exports" / f"backup-{uuid.uuid4()}.zip"
        try:
            with store.transaction() as db:
                require_initialized(store, db)
                snapshot = store.state(db)
                data = snapshot["data"]
                items = data["collections"] + [share["item"] for share in data["shares"]]
                asset_ids = {item["assetId"] for item in items if item.get("assetId")}
                asset_paths = {}
                for identifier in asset_ids:
                    row = require_asset(db, identifier)
                    path = store.root / "media" / row["filename"]
                    if not path.is_file():
                        raise HTTPException(409, "原图文件缺失，无法导出完整备份。")
                    asset_paths[identifier] = path
                with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                    for identifier, path in asset_paths.items():
                        bundle.write(path, f"media/{path.name}")
                    for item in items:
                        if item.get("assetId"):
                            item["customImage"] = f"media/{asset_paths[item['assetId']].name}"
                        if item.get("modelUrl"):
                            job_id = item["generationJobId"]
                            folder = store.root / "models" / job_id
                            if not (folder / "model.glb").is_file():
                                raise HTTPException(409, "模型文件缺失，无法导出完整备份。原图仍保留。")
                            for path in folder.iterdir():
                                if path.is_file() and path.suffix in {".glb", ".json", ".npz", ".npy", ".png"}:
                                    bundle.write(path, f"models/{job_id}/{path.name}")
                            item["modelUrl"] = f"models/{job_id}/model.glb"
                    references = ROOT / "demo" / "public" / "assets" / "reference"
                    if references.is_dir() and any(not item.get("assetId") for item in items):
                        for path in references.glob("*.png"):
                            bundle.write(path, f"reference/{path.name}")
                    bundle.writestr("manifest.json", encode({"format": "memoverse-local-backup", "version": 1, "createdAt": now_ms(), **snapshot}))
                    bundle.writestr("README.txt", "MemoVerse 本地完整备份。manifest.json 保存元数据；media 为原图；models 为模型和深度产物；reference 为示例图片来源。当前尚无备份导入界面。\n")
        except BaseException:
            archive.unlink(missing_ok=True)
            raise
        return FileResponse(archive, media_type="application/zip", filename="memoverse-local-backup.zip", background=BackgroundTask(archive.unlink, missing_ok=True))

    return app

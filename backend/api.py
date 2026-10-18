"""HTTP adapters: identity, validated contracts and response transport."""
from __future__ import annotations

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .backups import create_account_backup
from .contracts import CreateCollection, Like, Profile, UpdateCollection, Version
from .media import MAX_IMAGE_BYTES, persist_image
from .services import collections, profiles, social
from .state import state_response
from .store import Store, StoreError


def current_user(request: Request):
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict) or not isinstance(user.get("id"), str):
        raise StoreError(401, "请先登录。")
    return user["id"]


def install_routes(app: FastAPI, store: Store, worker):
    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/state")
    def state(request: Request):
        with store.transaction() as db:
            return state_response(store, worker, db, current_user(request))

    @app.post("/api/media")
    async def upload(request: Request, file: UploadFile = File()):
        user_id = current_user(request)
        try:
            raw = await file.read(MAX_IMAGE_BYTES + 1)
            return await run_in_threadpool(persist_image, store, user_id, raw)
        finally:
            await file.close()

    def stream_object(key, content_type):
        reader = store.storage.open(key)
        return StreamingResponse(
            reader.chunks(), media_type=content_type,
            headers={"Content-Length": str(reader.size), "Cache-Control": "no-store, private"},
            background=BackgroundTask(reader.close),
        )

    @app.get("/api/media/{asset_id}")
    def media(request: Request, asset_id: str):
        with store.transaction() as db:
            asset = dict(store.asset(db, current_user(request), asset_id))
        return stream_object(asset["object_key"], asset["mime"])

    @app.get("/api/models/{identifier}/model.glb")
    def model(request: Request, identifier: str):
        with store.transaction() as db:
            record = dict(store.model(db, current_user(request), identifier))
        return stream_object(record["model_key"], "model/gltf-binary")

    @app.post("/api/collections")
    def create(request: Request, body: CreateCollection):
        return collections.create(store, worker, current_user(request), body)

    @app.patch("/api/collections/{identifier}")
    def update(request: Request, identifier: str, body: UpdateCollection):
        return collections.update(store, worker, current_user(request), identifier, body)

    @app.delete("/api/collections/{identifier}")
    def delete(request: Request, identifier: str, body: Version):
        return collections.delete(store, worker, current_user(request), identifier, body)

    @app.post("/api/collections/{identifier}/retry-generation")
    def retry(request: Request, identifier: str):
        return collections.retry_generation(store, worker, current_user(request), identifier)

    @app.get("/api/jobs/{identifier}")
    def job_detail(request: Request, identifier: str):
        return collections.job_detail(store, current_user(request), identifier)

    @app.patch("/api/profile")
    def profile(request: Request, body: Profile):
        return profiles.update(store, worker, current_user(request), body)

    @app.post("/api/shares/read-all")
    def read_all(request: Request):
        return social.read_all(store, worker, current_user(request))

    @app.post("/api/shares/{identifier}/read")
    def read_share(request: Request, identifier: str):
        return social.read_share(store, worker, current_user(request), identifier)

    @app.post("/api/shares/{identifier}/collect")
    def collect_share(request: Request, identifier: str):
        return social.collect_share(store, worker, current_user(request), identifier)

    @app.post("/api/community/{identifier}/view")
    def view_community(request: Request, identifier: str):
        return social.view_community(store, worker, current_user(request), identifier)

    @app.put("/api/community/{identifier}/like")
    def like_community(request: Request, identifier: str, body: Like):
        return social.like_community(store, worker, current_user(request), identifier, body)

    @app.post("/api/community/{identifier}/collect")
    def collect_community(request: Request, identifier: str):
        return social.collect_community(store, worker, current_user(request), identifier)

    @app.get("/api/backup")
    def backup(request: Request):
        archive = create_account_backup(store, current_user(request))
        return FileResponse(
            archive, media_type="application/zip", filename="memoverse-account-backup.zip",
            background=BackgroundTask(archive.unlink, missing_ok=True),
        )

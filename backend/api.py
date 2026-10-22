"""HTTP adapters for authenticated movie tickets and referenced private media."""
from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .backups import create_account_backup
from .contracts import CreateTicket, Identifier, Operation, UpdateTicket
from .media import MAX_IMAGE_BYTES, persist_image
from .services import tickets
from .state import account_state
from .store import StoreError


def current_user(request):
    user = getattr(request.state, "user", None)
    if not isinstance(user, dict) or not isinstance(user.get("id"), str):
        raise StoreError(401, "请先登录。")
    return user["id"]


def install_routes(app: FastAPI, store):
    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/state")
    def state(request: Request):
        with store.transaction() as db:
            return account_state(store, db, current_user(request))

    @app.get("/api/operations/{operationId}")
    def operation_status(request: Request, operationId: Identifier):
        user_id = current_user(request)
        with store.transaction() as db:
            store.require_user(db, user_id)
            applied = db.execute("SELECT 1 FROM operations WHERE user_id=? AND id=?",
                                 (user_id, operationId)).fetchone() is not None
        return {"operationId": operationId, "applied": applied}

    @app.post("/api/media")
    async def upload(request: Request, file: UploadFile = File()):
        try:
            raw = await file.read(MAX_IMAGE_BYTES + 1)
            return await run_in_threadpool(persist_image, store, current_user(request), raw)
        finally:
            await file.close()

    @app.get("/api/media/{asset_id}")
    def media(request: Request, asset_id: str):
        with store.transaction() as db:
            asset = dict(store.referenced_asset(db, current_user(request), asset_id))
        reader = store.storage.open(asset["object_key"])
        return StreamingResponse(reader.chunks(), media_type=asset["mime"],
                                 headers={"Content-Length": str(reader.size), "Cache-Control": "no-store, private"},
                                 background=BackgroundTask(reader.close))

    @app.post("/api/tickets")
    def create(request: Request, body: CreateTicket):
        return tickets.apply(store, current_user(request), body.id, "create", body)

    @app.patch("/api/tickets/{identifier}")
    def update(request: Request, identifier: str, body: UpdateTicket):
        return tickets.apply(store, current_user(request), identifier, "update", body)

    @app.delete("/api/tickets/{identifier}")
    def delete(request: Request, identifier: str, body: Operation):
        return tickets.apply(store, current_user(request), identifier, "delete", body)

    @app.get("/api/backup")
    def backup(request: Request):
        archive = create_account_backup(store, current_user(request))
        return FileResponse(archive, media_type="application/zip", filename="memoverse-account-backup.zip",
                            background=BackgroundTask(archive.unlink, missing_ok=True))

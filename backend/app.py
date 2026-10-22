"""Authenticated schema-3 application with optional recoverable ticket processing."""
import logging
import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import accounts
from .api import install_routes
from .backups import BackupError
from .media import MAX_IMAGE_BYTES, UploadRecovery
from .object_storage import ObjectMissing, StorageError, configured_store
from .store import Store, StoreError, require_schema3_root
from .shared_state import RedisSharedState
from .processing import ProcessingWorker

ROOT = Path(__file__).resolve().parents[1]
log = logging.getLogger(__name__)


def create_app(data_dir: Path | None = None, *, auth_provider=None, object_store=None, shared_state=None,
               run_recovery=True, run_processing=True, processing_factory=None):
    root = Path(data_dir or os.environ.get("MEMOVERSE_DATA_DIR", ROOT / "backend/data-v3"))
    require_schema3_root(root)
    storage = object_store if object_store is not None else configured_store(root)
    store = Store(root, storage=storage)
    shared_state = shared_state if shared_state is not None else RedisSharedState.from_env()
    recovery = UploadRecovery(store)
    processing = (processing_factory or ProcessingWorker)(store, shared_state)

    @asynccontextmanager
    async def lifespan(app):
        if run_recovery:
            recovery.start()
        if run_processing:
            processing.start()
        try:
            yield
        finally:
            processing.stopping.set()
            recovery.stopping.set()
            try:
                stops = [run_in_threadpool(processing.stop)]
                if run_recovery:
                    stops.append(run_in_threadpool(recovery.stop))
                await asyncio.gather(*stops)
            finally:
                await run_in_threadpool(shared_state.close)
                await run_in_threadpool(storage.close)

    app = FastAPI(title="MemoVerse Movie Ticket API", version="0.6.0", lifespan=lifespan)
    app.state.store = store
    app.state.shared_state, app.state.upload_recovery = shared_state, recovery
    app.state.processing_worker = processing

    @app.exception_handler(StoreError)
    async def business_error(request, error):
        return JSONResponse({"detail": error.message}, status_code=error.status_code)

    @app.exception_handler(StorageError)
    async def storage_error(request, error):
        log.warning("Object storage operation failed: %s", type(error).__name__)
        return JSONResponse({"detail": "媒体暂时无法读取，记录仍保留，请稍后重试。"},
                            status_code=404 if isinstance(error, ObjectMissing) else 503)

    @app.exception_handler(BackupError)
    async def backup_error(request, error):
        log.warning("Account backup validation failed")
        return JSONResponse({"detail": "备份暂时无法生成，记录仍保留，请稍后重试。"}, status_code=503)

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

    install_routes(app, store, shared_state)
    if auth_provider is None:
        from .auth import install_auth
        auth_provider = install_auth
    app.state.auth = auth_provider(app, root, on_authenticated=lambda user: accounts.ensure_account(
        store, user["id"], user["email"], user.get("name", "")))
    return app

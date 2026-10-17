"""Named authentication and tiny textured-model fixtures, only for tests."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import struct

import numpy as np
from PIL import Image
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from backend.template import canonical


USERS = {
    "alice": {"id": "user-alice", "email": "alice@example.test", "name": "Alice"},
    "bob": {"id": "user-bob", "email": "bob@example.test", "name": "Bob"},
}
ORIGIN = "https://testserver"


class FakeAuthFactory:
    """Explicit dependency injection, never imported by production code."""

    def __init__(self):
        self.sessions = {f"session-{name}": user.copy() for name, user in USERS.items()}

    def __call__(self, app, data_root, *, on_authenticated):
        @app.middleware("http")
        async def authenticate(request, call_next):
            if request.url.path == "/api/health":
                return await call_next(request)
            token = request.cookies.get("test_session")
            user = self.sessions.get(token)
            if not user:
                return JSONResponse({"detail": "请先登录。"}, status_code=401)
            if request.method in {"POST", "PATCH", "PUT", "DELETE"}:
                if request.headers.get("X-CSRF-Token") != "test-csrf" or request.headers.get("Origin") != ORIGIN:
                    return JSONResponse({"detail": "请求校验失败。"}, status_code=403)
            await run_in_threadpool(on_authenticated, user)
            request.state.user = user.copy()
            return await call_next(request)
        return self

    def revoke(self, name):
        self.sessions.pop(f"session-{name}", None)


def signed_in(client, name="alice"):
    client.cookies.set("test_session", f"session-{name}")
    return client


WRITE_HEADERS = {"X-CSRF-Token": "test-csrf", "X-MemoVerse-Client": "web", "Origin": ORIGIN}


def image_bytes(color="red", format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (9, 7), color).save(output, format=format)
    return output.getvalue()


def model_bytes():
    texture = image_bytes("blue")
    positions = struct.pack("<9f", -1, -1, 0, 1, -1, 0.2, 0, 1, 0.4)
    indices = struct.pack("<3H", 0, 1, 2) + b"\0\0"
    binary = positions + indices + texture
    binary += b"\0" * (-len(binary) % 4)
    document = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
                "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": 0}]}],
                "buffers": [{"byteLength": len(binary)}],
                "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(positions)},
                                {"buffer": 0, "byteOffset": len(positions), "byteLength": 6},
                                {"buffer": 0, "byteOffset": len(positions) + len(indices), "byteLength": len(texture)}],
                "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3", "min": [-1, -1, 0], "max": [1, 1, 0.4]},
                              {"bufferView": 1, "componentType": 5123, "count": 3, "type": "SCALAR"}],
                "images": [{"bufferView": 2, "mimeType": "image/png"}], "textures": [{"source": 0}],
                "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}]}
    encoded = canonical(document)
    encoded += b" " * (-len(encoded) % 4)
    return b"glTF" + struct.pack("<II", 2, 28 + len(encoded) + len(binary)) + struct.pack("<II", len(encoded), 0x4E4F534A) + encoded + struct.pack("<II", len(binary), 0x004E4942) + binary


def write_model(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    (output / "model.glb").write_bytes(model_bytes())
    np.save(output / "depth.npy", np.array([[1, 2], [2, 3]], dtype=np.float32), allow_pickle=False)
    geometry = {"camera": {"type": "orthographic", "position": [0, 0, 4], "half_height": 1.05}, "testOnly": True}
    (output / "geometry.json").write_bytes(canonical(geometry))
    return {"model_path": output / "model.glb", "depth_path": output / "depth.npy", **geometry}


def template_fixture(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    original = image_bytes("blue")
    asset_id = hashlib.sha256(original).hexdigest()
    asset_path = f"media/{asset_id}.png"
    (root / "media").mkdir()
    (root / asset_path).write_bytes(original)
    model_id = hashlib.sha256(b"test-template-model-v1").hexdigest()
    output = root / "models" / model_id
    result = write_model(output)
    files = [{"path": f"models/{model_id}/{name}", "size": (output / name).stat().st_size,
              "sha256": hashlib.sha256((output / name).read_bytes()).hexdigest()} for name in ("model.glb", "depth.npy", "geometry.json")]
    item = {"id": "initial-collection", "title": "初始收藏", "category": "daily", "tags": ["日常"], "note": "独立副本", "date": "2026-10-18",
            "addedAt": 1, "source": "mine", "art": "cafe", "version": 1, "assetId": asset_id, "modelId": model_id, "generation": "ready"}
    manifest = {"schemaVersion": 1, "templateId": "initial-template-v1",
                "source": {"databaseSha256": "a" * 64, "snapshotPath": "source.sqlite3", "snapshotSha256": "b" * 64},
                "pipeline": {"anyimageCommit": "a" * 40, "modelSha256": "c" * 64, "workerSha256": "d" * 64, "dependencies": {}},
                "data": {"collections": [item], "shares": [{"id": "share-1", "item": {**item, "id": "shared-collection", "source": "friend", "sender": "小林"}, "sender": "小林", "receivedAt": "2026-10-18", "unread": True}],
                         "community": [{"id": "community-1", "title": "示例社区", "category": "daily", "art": "cafe", "author": "小林", "description": "示例", "exposures": 10, "saves": 2, "likes": 3, "assetId": asset_id, "modelId": model_id, "generation": "ready"}],
                         "profile": {"name": "模板名字", "bio": "保留日子"}, "likedCommunityIds": []},
                "assets": [{"id": asset_id, "path": asset_path, "mimeType": "image/png", "size": len(original), "width": 9, "height": 7, "sha256": asset_id}],
                "models": [{"id": model_id, "assetId": asset_id, "path": f"models/{model_id}/model.glb", "depthPath": f"models/{model_id}/depth.npy", "geometryPath": f"models/{model_id}/geometry.json",
                            "camera": result["camera"], "provenance": {"kind": "generated"}, "files": files}]}
    manifest["version"] = hashlib.sha256(canonical(manifest)).hexdigest()
    (root / "manifest.json").write_bytes(canonical(manifest))
    return manifest

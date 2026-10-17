"""Validate one immutable, server-approved initialization template."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import struct
import threading

from PIL import Image

from .object_storage import ObjectStore, object_key, sha_file


class TemplateError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def portable(value):
    if isinstance(value, dict):
        return {key: portable(item) for key, item in value.items() if not key.lower().endswith("_path")}
    if isinstance(value, list):
        return [portable(item) for item in value]
    return value


def safe_file(root: Path, relative: str):
    if not isinstance(relative, str) or not relative or "\\" in relative or ":" in relative:
        raise TemplateError("Invalid template path")
    parts = PurePosixPath(relative).parts
    if relative.startswith("/") or any(part in ("", ".", "..") for part in relative.split("/")):
        raise TemplateError("Template path escapes its root")
    path = root
    for part in parts:
        path /= part
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise TemplateError("Template paths cannot contain links")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise TemplateError("Template file is missing or outside its root")
    return path


def validate_glb(path: Path):
    with path.open("rb") as stream:
        header = stream.read(12)
        if len(header) != 12:
            raise TemplateError("Incomplete GLB header")
        magic, version, length = struct.unpack("<4sII", header)
        if magic != b"glTF" or version != 2 or length != path.stat().st_size:
            raise TemplateError("Invalid GLB identity or length")
        chunk = stream.read(8)
        if len(chunk) != 8:
            raise TemplateError("GLB is missing its JSON chunk")
        size, kind = struct.unpack("<II", chunk)
        if kind != 0x4E4F534A or size > min(length - 20, 4 * 1024 * 1024):
            raise TemplateError("Invalid GLB JSON chunk")
        document = json.loads(stream.read(size).decode("utf-8"))
        if document.get("asset", {}).get("version") != "2.0" or not document.get("meshes") or not document.get("images"):
            raise TemplateError("GLB is missing textured geometry")
        if any("uri" in row for row in document.get("buffers", []) + document.get("images", [])):
            raise TemplateError("GLB dependencies must be embedded")


def validate_model_outputs(model_path: Path, depth_path: Path, geometry_path: Path):
    import numpy as np
    validate_glb(model_path)
    depth = np.load(depth_path, allow_pickle=False, mmap_mode="r")
    try:
        if depth.ndim != 2 or not depth.size or not np.isfinite(depth).any():
            raise TemplateError("Invalid depth output")
    finally:
        mmap = getattr(depth, "_mmap", None)
        if mmap is not None:
            mmap.close()
    geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
    if not isinstance(geometry, dict) or not isinstance(geometry.get("camera"), dict) or portable(geometry) != geometry:
        raise TemplateError("Geometry metadata must be portable and contain a camera")
    return geometry


class InitialTemplate:
    def __init__(self, root: Path):
        self.root = root.absolute()
        if self.root.is_symlink() or (hasattr(self.root, "is_junction") and self.root.is_junction()):
            raise TemplateError("Template root cannot be a link")
        self.manifest = json.loads(safe_file(self.root, "manifest.json").read_text(encoding="utf-8"))
        self._lock = threading.Lock()
        self._prepared = False
        self._validate()

    def _validate(self):
        manifest = self.manifest
        if manifest.get("schemaVersion") != 1 or manifest.get("templateId") != "initial-template-v1":
            raise TemplateError("Unsupported initialization template")
        version = hashlib.sha256(canonical({key: value for key, value in manifest.items() if key != "version"})).hexdigest()
        if manifest.get("version") != version:
            raise TemplateError("Template version checksum mismatch")
        self.version = version
        self.assets = self._index(manifest["assets"])
        self.models = self._index(manifest["models"])
        self.data = manifest["data"]
        if set(self.data) != {"collections", "shares", "community", "profile", "likedCommunityIds"}:
            raise TemplateError("Incomplete template data groups")
        for group in ("collections", "shares", "community"):
            self._index(self.data[group], content=True)
        if not isinstance(self.data["profile"].get("name"), str) or not isinstance(self.data["profile"].get("bio"), str):
            raise TemplateError("Invalid template profile")
        community_ids = {item["id"] for item in self.data["community"]}
        if any(identifier not in community_ids for identifier in self.data["likedCommunityIds"]):
            raise TemplateError("Template likes reference unknown content")
        for asset in self.assets.values():
            if asset["sha256"] != asset["id"] or asset["size"] <= 0 or min(asset["width"], asset["height"]) <= 0:
                raise TemplateError("Invalid template asset metadata")
            expected = f"media/{asset['id']}"
            if not asset["path"].startswith(expected) or PurePosixPath(asset["path"]).parent.as_posix() != "media":
                raise TemplateError("Asset must use a content-addressed media path")
            path = self._verify_file(asset)
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                mime = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}.get(image.format)
                if image.size != (asset["width"], asset["height"]) or mime != asset["mimeType"]:
                    raise TemplateError("Asset dimensions/type disagree with its manifest")
        for model in self.models.values():
            if model["assetId"] not in self.assets:
                raise TemplateError("Model references an unknown image")
            expected = {f"models/{model['id']}/{name}" for name in ("model.glb", "depth.npy", "geometry.json")}
            actual = {item["path"] for item in model["files"]}
            if len(model["files"]) != 3 or actual != expected or {model["path"], model["depthPath"], model["geometryPath"]} != expected:
                raise TemplateError("Model manifest must include exactly GLB/depth/geometry")
            for record in model["files"]:
                self._verify_file(record)
            geometry = validate_model_outputs(safe_file(self.root, model["path"]), safe_file(self.root, model["depthPath"]), safe_file(self.root, model["geometryPath"]))
            if model["camera"] != geometry["camera"]:
                raise TemplateError("Model camera disagrees with geometry metadata")
        asset_refs, model_refs = set(), set()
        records = [*self.data["collections"], *[share["item"] for share in self.data["shares"]], *self.data["community"]]
        for item in records:
            if item.get("generation") != "ready" or item.get("assetId") not in self.assets or item.get("modelId") not in self.models:
                raise TemplateError("Every initial item must have a verified original and ready model")
            if self.models[item["modelId"]]["assetId"] != item["assetId"]:
                raise TemplateError("Initial item model belongs to another original")
            if any(key in item for key in ("customImage", "modelUrl", "generationJobId")):
                raise TemplateError("Template must not contain legacy URLs/job references")
            asset_refs.add(item["assetId"])
            model_refs.add(item["modelId"])
        if asset_refs != set(self.assets) or model_refs != set(self.models):
            raise TemplateError("Template contains unreferenced assets/models")

    @staticmethod
    def _index(records, *, content=False):
        if not isinstance(records, list):
            raise TemplateError("Template records must be arrays")
        result = {}
        for item in records:
            identifier = item.get("id")
            expression = r"[A-Za-z0-9_-]{1,120}" if content else r"[a-f0-9]{64}"
            if not isinstance(identifier, str) or not re.fullmatch(expression, identifier) or identifier in result:
                raise TemplateError("Template contains invalid/duplicate IDs")
            result[identifier] = item
        return result

    def _verify_file(self, record):
        object_key(record["path"])
        path = safe_file(self.root, record["path"])
        if path.stat().st_size != record["size"] or sha_file(path) != record["sha256"]:
            raise TemplateError("Template file checksum mismatch")
        return path

    def prepare_objects(self, storage: ObjectStore):
        """Called outside SQL transactions; one verified upload per process."""
        with self._lock:
            if self._prepared:
                return
            for asset in self.assets.values():
                storage.put_file(asset["path"], safe_file(self.root, asset["path"]), asset["sha256"], asset["mimeType"])
            for model in self.models.values():
                for record in model["files"]:
                    mime = "model/gltf-binary" if record["path"].endswith(".glb") else "application/json" if record["path"].endswith(".json") else "application/octet-stream"
                    storage.put_file(record["path"], safe_file(self.root, record["path"]), record["sha256"], mime)
            self._prepared = True

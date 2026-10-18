"""Prepare immutable model objects without opening business transactions."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import threading

from ..object_storage import ObjectStore, sha_file
from ..template import canonical, portable, safe_file, validate_model_outputs


def prepare_model(storage: ObjectStore, temporary_root: Path, asset, stop_event: threading.Event):
    from ..relief_engine import generate_relief
    with tempfile.TemporaryDirectory(prefix="generation-", dir=temporary_root) as temporary:
        folder = Path(temporary)
        source = folder / ("original" + Path(asset["object_key"]).suffix)
        storage.download(asset["object_key"], source, asset["id"])
        if stop_event.is_set():
            raise RuntimeError("服务停止，生成任务将在下次启动时恢复。")
        output = folder / "output"
        result = generate_relief(source, output, cancel_event=stop_event)
        model_path = safe_file(output, "model.glb")
        if Path(result["model_path"]).resolve() != model_path.resolve():
            raise RuntimeError("生成程序返回了非当前任务的模型文件。")
        depth_path = safe_file(output, "depth.npy")
        geometry_path = safe_file(output, "geometry.json")
        geometry = portable(json.loads(geometry_path.read_text(encoding="utf-8")))
        geometry_path.write_bytes(canonical(geometry) + b"\n")
        validate_model_outputs(model_path, depth_path, geometry_path)
        records = [{"name": path.name, "size": path.stat().st_size, "sha256": sha_file(path)} for path in (model_path, depth_path, geometry_path)]
        model_id = hashlib.sha256(canonical({"assetId": asset["id"], "files": records, "camera": geometry["camera"]})).hexdigest()
        files = []
        for record in records:
            if stop_event.is_set():
                raise RuntimeError("服务停止，生成任务将在下次启动时恢复。")
            key = f"models/{model_id}/{record['name']}"
            mime = "model/gltf-binary" if record["name"].endswith(".glb") else "application/json" if record["name"].endswith(".json") else "application/octet-stream"
            storage.put_file(key, safe_file(output, record["name"]), record["sha256"], mime)
            files.append({"path": key, "size": record["size"], "sha256": record["sha256"]})
        return {"id": model_id, "assetId": asset["id"], "path": f"models/{model_id}/model.glb",
                "depthPath": f"models/{model_id}/depth.npy", "geometryPath": f"models/{model_id}/geometry.json",
                "files": files, "camera": geometry["camera"], "provenance": {"kind": "generated"},
                "geometry": geometry}

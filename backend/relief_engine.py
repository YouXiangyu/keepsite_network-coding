"""Local subprocess boundary for the pinned AnyImage CPU inference adapter."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def engine_status() -> dict:
    required = [ROOT / '.cache/anyimage/src/anyimage/server/models/onnx_moge2.py', ROOT / '.cache/models/moge-2-vits-normal-onnx/model.onnx', ROOT / '.cache/anyimage-deps/onnxruntime', ROOT / '.cache/anyimage-deps/trimesh']
    missing = [str(path.relative_to(ROOT)) for path in required if not path.exists()]
    for module in ('numpy', 'PIL'):
        if importlib.util.find_spec(module) is None:
            missing.append('Python dependency: ' + module)
    model = ROOT / '.cache/models/moge-2-vits-normal-onnx/model.onnx'
    if model.exists() and model.stat().st_size != 140852051:
        missing.append('Geometry model size mismatch; rerun setup to verify SHA256')
    return {'available': not missing, 'reason': 'Missing: ' + ', '.join(missing) if missing else 'Local AnyImage MoGe2 CPU inference ready', 'pipeline': 'AnyImage official MoGe2 inference + project relief mesh/GLB export'}


def generate_relief(input_path: Path, output_dir: Path, *, cancel_event=None) -> dict:
    status = engine_status()
    if not status['available']:
        raise RuntimeError(status['reason'])
    env = os.environ.copy()
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['HF_HUB_OFFLINE'] = '1'
    process = subprocess.Popen([os.environ.get('ANYIMAGE_PYTHON', sys.executable), '-B', str(ROOT / 'backend/anyimage_worker.py'), str(input_path.resolve()), str(output_dir.resolve())], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    deadline = time.monotonic() + 600
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError('本机服务停止，生成任务将在下次启动时恢复。')
            if time.monotonic() >= deadline:
                raise RuntimeError('生成超过十分钟，原图已保留，可以稍后重试。')
            try:
                stdout, stderr = process.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                continue
        if process.returncode:
            raise RuntimeError('AnyImage inference failed: ' + stderr[-3000:])
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
    metadata = json.loads(stdout.strip().splitlines()[-1])
    metadata['model_path'] = Path(metadata['model_path'])
    metadata['depth_path'] = Path(metadata['depth_path'])
    return metadata

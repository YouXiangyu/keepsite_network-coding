"""Loopback-only API and subprocess supervisor for the local relief laboratory."""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import struct
import subprocess
import sys
import threading
from typing import Callable
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image

from backend.media import MAX_IMAGE_BYTES, image_info
from .lab_process import start_managed_process


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_IMAGE = ROOT / '.cache/initial-template-v1/media/3a4b33dd86f71205b1116cf9cb2715c179d39ed6fdccd8a6223df3c2f32becbb.jpg'
FILE_NAMES = {
    'model': 'model.glb', 'original': 'source.png', 'overlay': 'overlay.png',
    'mask': 'foreground-mask.png', 'report': 'report.json', 'parameters': 'parameters.json',
}
MIME_TYPES = {'.glb': 'model/gltf-binary', '.png': 'image/png', '.json': 'application/json'}
IMAGE_ID = re.compile(r'(?:[0-9a-f]{32}|[0-9a-f]{64})\Z')
RUN_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z')
TERMINAL_STATUSES = {'ready', 'failed', 'cancelled', 'interrupted'}


def _now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def _atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path):
    def invalid_constant(value):
        raise ValueError('non-finite JSON value')

    value = json.loads(path.read_text(encoding='utf-8'), parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError('JSON object required')
    return value


def _safe_file(root, filename):
    candidate = (root / filename).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        raise HTTPException(404, '文件不存在。')
    return candidate


def _display_name(value, fallback):
    if not isinstance(value, str):
        raise HTTPException(422, '名称必须是文本。')
    value = value.replace('\\', '/').rsplit('/', 1)[-1]
    return ''.join(char for char in value if char.isprintable()).strip()[:120] or fallback


def _public_value(value):
    """Reports may include local model metadata; never publish local paths/logs."""
    private_keys = {
        'source_path', 'mask_path', 'model_path', 'depth_path', 'model_dir', 'job_dir',
        'cache_root', 'output_dir', 'data_dir', 'path', 'file_path', 'directory',
        'traceback', 'stack', 'stderr', 'stdout', 'log',
    }
    if isinstance(value, dict):
        return {key: _public_value(item) for key, item in value.items() if key not in private_keys}
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if isinstance(value, str) and (
        re.search(r'(?i)(?:^|\s)[a-z]:[\\/]', value) or value.startswith(('\\\\', '/'))
    ):
        return '[本机路径已省略]'
    return value


def _default_command(job_dir, stage):
    return [sys.executable, '-u', '-B', '-m', 'experiments.subject_relief.lab_worker',
            '--job-dir', str(job_dir), '--stage', stage]


@dataclass
class _ActiveRun:
    id: str
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    process: subprocess.Popen | None = None
    thread: threading.Thread | None = None
    interrupted: bool = False


class _WorkerFailure(RuntimeError):
    pass


class LabRuntime:
    def __init__(self, data_dir, ui_dir, worker_command_factory, seed_default):
        self.root = Path(data_dir).resolve()
        self.ui_root = Path(ui_dir).resolve()
        self.images_dir = self.root / 'images'
        self.runs_dir = self.root / 'runs'
        self.cache_root = self.root / 'cache'
        for path in (self.images_dir, self.runs_dir, self.cache_root, self.root / 'tmp'):
            path.mkdir(parents=True, exist_ok=True)
        self.command_factory = worker_command_factory or _default_command
        self.lock = threading.RLock()
        self.token = secrets.token_urlsafe(32)
        self.images = {}
        self.runs = {}
        self.active: _ActiveRun | None = None
        self.stopping = False
        self.default_image_id = None
        self._load_history()
        if seed_default is not False:
            source = DEFAULT_IMAGE if seed_default is True else Path(seed_default)
            if source.is_file():
                raw = source.read_bytes()
                image = self.add_image(raw, 'image', '酒店房间（默认）', stable_id=hashlib.sha256(raw).hexdigest())
                self.default_image_id = image['id']
                if seed_default is True:
                    stored = self.images[image['id']]
                    stored['initial_boxes'] = [[1400 / 4096, 1100 / 3072, 1945 / 4096, 2050 / 3072]]
                    _atomic_json(self.images_dir / (image['id'] + '.json'), stored)

    def _load_history(self):
        for path in self.images_dir.glob('*.json'):
            if not IMAGE_ID.fullmatch(path.stem):
                continue
            try:
                record = _read_json(path)
                if record.get('id') != path.stem or record.get('kind') not in ('image', 'mask'):
                    continue
                if record.get('extension') not in ('.jpg', '.png', '.webp'):
                    continue
                _safe_file(self.images_dir, record['id'] + record['extension'])
                self.images[record['id']] = record
            except (OSError, ValueError, HTTPException):
                continue
        for folder in self.runs_dir.iterdir():
            if (not folder.is_dir() or not RUN_ID.fullmatch(folder.name)
                    or folder.resolve().parent != self.runs_dir.resolve()):
                continue
            try:
                record = _read_json(_safe_file(folder, 'job.json'))
                if record.get('id') != folder.name:
                    continue
                if record.get('status') in ('running', 'cancelling'):
                    record.update(status='interrupted', finished_at=_now(),
                                  error='上次服务退出时任务尚未完成，请重新生成。')
                    _atomic_json(folder / 'job.json', record)
                if record.get('status') not in TERMINAL_STATUSES:
                    continue
                self.runs[folder.name] = record
            except (OSError, ValueError, HTTPException):
                continue

    def _persist(self, record):
        _atomic_json(self.runs_dir / record['id'] / 'job.json', record)

    def image_public(self, record):
        result = {key: record[key] for key in ('id', 'name', 'kind', 'width', 'height')}
        result['preview_url'] = f"/api/images/{record['id']}/preview"
        if 'initial_boxes' in record:
            result['initial_boxes'] = record['initial_boxes']
        return result

    def add_image(self, raw, kind, name, *, stable_id=None):
        if kind not in ('image', 'mask'):
            raise HTTPException(422, 'kind 必须为 image 或 mask。')
        name = _display_name(name, '上传图片')
        info = image_info(raw)
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size[::-1] if image.getexif().get(274) in (5, 6, 7, 8) else image.size
        identifier = stable_id or uuid.uuid4().hex
        with self.lock:
            if identifier in self.images:
                return self.image_public(self.images[identifier])
            extension = Path(info['filename']).suffix
            path = self.images_dir / (identifier + extension)
            if stable_id and path.is_file():
                # A previous shutdown can leave the copied seed before its metadata.
                if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != info['sha256']:
                    raise ValueError('Existing seed image does not match its content identity.')
            else:
                with path.open('xb') as stream:
                    stream.write(raw)
            record = {
                'id': identifier, 'name': name, 'kind': kind,
                'width': width, 'height': height, 'extension': extension,
                'mime_type': info['mimeType'], 'sha256': info['sha256'], 'created_at': _now(),
            }
            _atomic_json(self.images_dir / (identifier + '.json'), record)
            self.images[identifier] = record
            return self.image_public(record)

    def image_path(self, identifier, expected_kind=None):
        if not isinstance(identifier, str) or not IMAGE_ID.fullmatch(identifier):
            raise HTTPException(404, '图片不存在。')
        record = self.images.get(identifier)
        if record is None or (expected_kind and record['kind'] != expected_kind):
            raise HTTPException(404, '图片不存在或类型不符。')
        return _safe_file(self.images_dir, identifier + record['extension'])

    def run_public(self, record, *, detail=False):
        keys = ('id', 'title', 'status', 'image_id', 'image_name', 'mask_image_id', 'parameters',
                'boxes', 'created_at', 'started_at', 'finished_at', 'progress', 'error')
        result = {key: record.get(key) for key in keys}
        result['elapsed_seconds'] = None
        if record.get('started_at'):
            try:
                started = datetime.fromisoformat(record['started_at'].replace('Z', '+00:00'))
                finished = datetime.fromisoformat((record.get('finished_at') or _now()).replace('Z', '+00:00'))
                result['elapsed_seconds'] = round(max(0, (finished - started).total_seconds()), 3)
            except (TypeError, ValueError):
                pass
        result['files'] = {}
        if record['status'] == 'ready':
            folder = self.runs_dir / record['id']
            result['files'] = {
                key: f"/api/runs/{record['id']}/files/{filename}"
                for key, filename in FILE_NAMES.items() if (folder / filename).is_file()
            }
            if detail:
                try:
                    result['report'] = _public_value(_read_json(_safe_file(folder, 'report.json')))
                except (OSError, ValueError, HTTPException):
                    result['report'] = None
        return result

    def list_runs(self):
        with self.lock:
            return {
                'runs': [self.run_public(record) for record in sorted(
                    self.runs.values(), key=lambda item: item.get('created_at', ''), reverse=True
                )],
                'active_run_id': self.active.id if self.active else None,
            }

    def get_run(self, identifier):
        if not RUN_ID.fullmatch(identifier):
            raise HTTPException(404, '实验不存在。')
        with self.lock:
            if identifier not in self.runs:
                raise HTTPException(404, '实验不存在。')
            return self.run_public(self.runs[identifier], detail=True)

    def start_run(self, payload):
        from .lab_config import validate_boxes, validate_parameters

        if not isinstance(payload, dict):
            raise HTTPException(422, '请求必须是 JSON 对象。')
        allowed = {'image_id', 'mask_image_id', 'parameters', 'boxes', 'title'}
        if set(payload) - allowed:
            raise HTTPException(422, '请求含有不支持的字段。')
        try:
            parameters = validate_parameters(payload.get('parameters', {}))
            boxes = validate_boxes(payload.get('boxes', []))
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        image_id = payload.get('image_id')
        mask_id = payload.get('mask_image_id')
        if parameters['mask_mode'] == 'provided' and parameters['background_flatten'] > 0 and mask_id is None:
            raise HTTPException(422, '使用上传蒙版时，请先选择一张黑白蒙版。')
        with self.lock:
            if self.stopping:
                raise HTTPException(503, '实验服务正在停止。')
            if self.active is not None:
                raise HTTPException(409, '已有实验正在运行，请等待或先取消。')
            source = self.image_path(image_id, 'image')
            mask = self.image_path(mask_id, 'mask') if mask_id is not None else None
            image = self.images[image_id]
            if mask_id is not None:
                mask_image = self.images[mask_id]
                if mask_image['width'] * image['height'] != image['width'] * mask_image['height']:
                    raise HTTPException(422, '蒙版宽高比必须与转正后的原图一致。')
            title = _display_name(payload.get('title', image['name'] + ' · 实验'), '新实验')
            identifier = str(uuid.uuid4())
            folder = self.runs_dir / identifier
            folder.mkdir()
            now = _now()
            record = {
                'id': identifier, 'title': title, 'status': 'running', 'image_id': image_id,
                'image_name': image['name'], 'mask_image_id': mask_id, 'parameters': parameters,
                'boxes': boxes, 'created_at': now, 'started_at': now, 'finished_at': None,
                'progress': {'stage': 'starting', 'message': '准备实验', 'current': 0, 'total': 0},
                'error': None,
            }
            _atomic_json(folder / 'request.json', {
                'id': identifier, 'image_id': image_id, 'source_path': str(source),
                'source_name': image['name'], 'mask_path': str(mask) if mask else None,
                'parameters': parameters, 'boxes': boxes, 'cache_root': str(self.cache_root),
            })
            _atomic_json(folder / 'parameters.json', parameters)
            self._persist(record)
            self.runs[identifier] = record
            active = _ActiveRun(identifier)
            active.thread = threading.Thread(target=self._run, args=(active,), daemon=True,
                                             name='relief-lab-' + identifier[:8])
            self.active = active
            active.thread.start()
            return self.run_public(record)

    @staticmethod
    def _stop_process(process):
        if process is None:
            return
        if process.poll() is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def _progress(self, active, value):
        if not isinstance(value, dict) or value.get('event') != 'progress':
            return
        current, total = value.get('current', 0), value.get('total', 0)
        if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in (current, total)):
            return
        progress = {
            'stage': str(_public_value(str(value.get('stage', 'working'))))[:64],
            'message': str(_public_value(str(value.get('message', '正在计算'))))[:240],
            'current': current, 'total': total,
        }
        if isinstance(value.get('cache_hit'), bool):
            progress['cache_hit'] = value['cache_hit']
        with self.lock:
            if self.runs[active.id]['status'] == 'running':
                self.runs[active.id]['progress'] = progress
                self._persist(self.runs[active.id])

    def _execute_stage(self, active, stage, log):
        folder = self.runs_dir / active.id
        command = self.command_factory(folder, stage)
        if not isinstance(command, (list, tuple)) or not command or any(not isinstance(part, str) for part in command):
            raise _WorkerFailure('实验进程命令无效。')
        env = os.environ.copy()
        env.update(PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
                   HF_HOME=str(self.cache_root / 'huggingface'), TORCH_HOME=str(self.cache_root / 'torch'),
                   TEMP=str(self.root / 'tmp'), TMP=str(self.root / 'tmp'))
        with self.lock:
            if active.cancel.is_set():
                return
            process, process_job = start_managed_process(
                command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            active.process = process
        try:
            logged = 0
            while True:
                line = process.stdout.readline(65536)
                if not line:
                    break
                if logged < 4 * 1024 * 1024:
                    log.write(line)
                    log.flush()
                    logged += len(line)
                if len(line) <= 8192:
                    try:
                        self._progress(active, json.loads(line))
                    except (ValueError, TypeError):
                        pass
            process.wait()
            if process.returncode and not active.cancel.is_set():
                raise _WorkerFailure('分割阶段失败，详细信息已保留在本机日志。' if stage == 'mask'
                                     else '生成阶段失败，详细信息已保留在本机日志。')
        finally:
            try:
                self._stop_process(process)
                process.stdout.close()
            finally:
                # Closing also reclaims any remaining processes if a worker
                # exits unexpectedly. Parent death closes the OS handle too.
                process_job.close()
                with self.lock:
                    if active.process is process:
                        active.process = None

    def _validate_outputs(self, folder, stage):
        try:
            if stage == 'mask':
                for name in ('foreground-mask.png', 'source.png'):
                    with Image.open(_safe_file(folder, name)) as image:
                        if image.format != 'PNG':
                            raise ValueError('PNG artifact required')
                        image.verify()
                _read_json(_safe_file(folder, 'mask-stage.json'))
            else:
                _read_json(_safe_file(folder, 'report.json'))
                path = _safe_file(folder, 'model.glb')
                with path.open('rb') as stream:
                    header = stream.read(20)
                    if len(header) != 20:
                        raise ValueError('truncated GLB')
                    magic, version, length, chunk_length, chunk_type = struct.unpack('<4sIIII', header)
                    if magic != b'glTF' or version != 2 or length != path.stat().st_size:
                        raise ValueError('invalid GLB header')
                    if chunk_type != 0x4E4F534A or not 0 < chunk_length <= min(length - 20, 4 * 1024 * 1024):
                        raise ValueError('invalid GLB metadata')
                    document = json.loads(stream.read(chunk_length).rstrip(b' \x00'))
                    if document.get('asset', {}).get('version') != '2.0' or not document.get('meshes'):
                        raise ValueError('GLB has no mesh')
        except (OSError, ValueError, HTTPException, struct.error, AttributeError) as exc:
            raise _WorkerFailure('实验产物校验失败，未发布模型。') from exc

    def _run(self, active):
        error = None
        succeeded = False
        folder = self.runs_dir / active.id
        try:
            with (folder / 'jobrun.log').open('a', encoding='utf-8') as log:
                for stage in ('mask', 'generate'):
                    if active.cancel.is_set():
                        break
                    self._execute_stage(active, stage, log)
                    if active.cancel.is_set():
                        break
                    self._validate_outputs(folder, stage)
                succeeded = not active.cancel.is_set()
        except _WorkerFailure as exc:
            error = str(exc)
        except Exception:
            error = '本机实验未能完成，请检查任务日志后重试。'
        finally:
            self._stop_process(active.process)
            with self.lock:
                record = self.runs[active.id]
                if active.cancel.is_set():
                    status = 'interrupted' if active.interrupted else 'cancelled'
                else:
                    status = 'ready' if succeeded and error is None else 'failed'
                record.update(status=status, finished_at=_now(), error=error if status == 'failed' else None)
                if status == 'ready':
                    record['progress'] = {'stage': 'ready', 'message': '实验完成', 'current': 1, 'total': 1}
                try:
                    self._persist(record)
                except OSError:
                    record.update(status='failed', error='无法保存实验历史，请检查本机磁盘后重试。')
                finally:
                    if self.active is active:
                        self.active = None
                    active.done.set()

    def cancel_run(self, identifier):
        with self.lock:
            self.get_run(identifier)
            record = self.runs[identifier]
            if record['status'] in ('cancelled', 'interrupted'):
                return self.run_public(record)
            if not self.active or self.active.id != identifier or record['status'] in TERMINAL_STATUSES:
                raise HTTPException(409, '任务已经结束，无需取消。')
            active = self.active
            active.cancel.set()
            record['status'] = 'cancelling'
            try:
                self._persist(record)
            except OSError:
                record['error'] = '取消记录暂时无法写入本机磁盘。'
            process = active.process
        self._stop_process(process)
        return self.get_run(identifier)

    def shutdown(self):
        with self.lock:
            self.stopping = True
            active = self.active
            if active:
                active.interrupted = True
                active.cancel.set()
                self.runs[active.id]['status'] = 'cancelling'
                try:
                    self._persist(self.runs[active.id])
                except OSError:
                    pass  # Process reclamation must still happen if the disk is full.
        if active:
            self._stop_process(active.process)
            active.thread.join(timeout=8)
            if active.thread.is_alive():
                raise RuntimeError('Local experiment worker did not stop cleanly.')


async def _bounded_body(request, maximum):
    declared = request.headers.get('content-length')
    if declared:
        try:
            if int(declared) > maximum:
                raise HTTPException(413, '请求内容过大。')
        except ValueError as exc:
            raise HTTPException(400, 'Content-Length 无效。') from exc
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise HTTPException(413, '请求内容过大。')
        chunks.append(chunk)
    return b''.join(chunks)


def create_app(*, data_dir=None, ui_dir=None, worker_command_factory: Callable | None = None,
               seed_default=True, port=4183):
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('port must be an integer in 1..65535')
    runtime = LabRuntime(data_dir or ROOT / '.cache/subject-relief/lab',
                         ui_dir or ROOT / '.cache/subject-relief/lab-ui', worker_command_factory, seed_default)

    @asynccontextmanager
    async def lifespan(app):
        try:
            yield
        finally:
            runtime.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.lab_runtime = runtime
    origins = {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}

    @app.middleware('http')
    async def local_boundary(request, call_next):
        host = request.headers.get('host', '').lower()
        match = re.fullmatch(r'(127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?', host)
        if not match or (match.group(2) is not None and int(match.group(2)) != port):
            response = JSONResponse({'detail': '仅允许本机实验地址。'}, status_code=400)
        elif request.method not in ('GET', 'HEAD', 'OPTIONS') and (
            request.headers.get('origin') not in origins
            or not secrets.compare_digest(request.headers.get('x-lab-token', '').encode('utf-8'), runtime.token.encode('ascii'))
        ):
            response = JSONResponse({'detail': '请从本机实验页面发起操作。'}, status_code=403)
        else:
            response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cross-Origin-Resource-Policy'] = 'same-origin'
        response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/bootstrap')
    def bootstrap():
        from .lab_config import config_payload

        with runtime.lock:
            return {
                'token': runtime.token, 'config': config_payload(),
                'images': [runtime.image_public(record) for record in runtime.images.values()],
                'default_image_id': runtime.default_image_id,
                **runtime.list_runs(),
            }

    @app.get('/api/runs')
    def runs():
        return runtime.list_runs()

    @app.get('/api/runs/{identifier}')
    def run(identifier: str):
        return runtime.get_run(identifier)

    @app.post('/api/images', status_code=201)
    async def upload(request: Request, kind: str = 'image', name: str = '上传图片'):
        raw = await _bounded_body(request, MAX_IMAGE_BYTES)
        return runtime.add_image(raw, kind, name)

    @app.post('/api/runs', status_code=201)
    async def start(request: Request):
        raw = await _bounded_body(request, 65536)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise HTTPException(422, '请求必须为有效 JSON。') from exc
        return runtime.start_run(payload)

    @app.post('/api/runs/{identifier}/cancel')
    def cancel(identifier: str):
        return runtime.cancel_run(identifier)

    @app.get('/api/images/{identifier}/preview')
    def preview(identifier: str):
        with runtime.lock:
            path = runtime.image_path(identifier)
            mime = runtime.images[identifier]['mime_type']
        return FileResponse(path, media_type=mime)

    @app.get('/api/runs/{identifier}/files/{filename}')
    def artifact(identifier: str, filename: str):
        if filename not in FILE_NAMES.values():
            raise HTTPException(404, '文件不存在。')
        run = runtime.get_run(identifier)
        if run['status'] != 'ready':
            raise HTTPException(409, '实验尚未完成。')
        path = _safe_file(runtime.runs_dir / identifier, filename)
        if filename == 'report.json':
            return JSONResponse(_public_value(_read_json(path)))
        return FileResponse(path, media_type=MIME_TYPES[Path(filename).suffix],
                            filename=f'{identifier}.glb' if filename == 'model.glb' else None)

    @app.get('/')
    @app.get('/index.html')
    def index():
        return FileResponse(_safe_file(runtime.ui_root, 'index.html'), media_type='text/html')

    @app.get('/lab.bundle.js')
    def javascript():
        return FileResponse(_safe_file(runtime.ui_root, 'lab.bundle.js'), media_type='text/javascript')

    @app.get('/styles.css')
    def stylesheet():
        return FileResponse(_safe_file(runtime.ui_root, 'styles.css'), media_type='text/css')

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=4183)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run(create_app(port=args.port), host='127.0.0.1', port=args.port, workers=1)


if __name__ == '__main__':
    main()

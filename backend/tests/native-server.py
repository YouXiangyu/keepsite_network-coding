"""Owned Android/Web loopback fixture; no cloud credentials or provider traffic."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import random
import re
import secrets
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
import fakeredis
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
import jwt
from PIL import Image
import uvicorn

from backend.app import create_app
from backend.auth import AuthConfig, AuthError, equal_secret, install_auth
from backend.contracts import CreateTicket
from backend.media import persist_image
from backend.object_storage import LocalObjectStore
from backend.services import tickets
from backend.shared_state import RedisSharedState

ORIGIN = 'http://127.0.0.1:4180'
FIXTURE_HEADER = 'native-browser-fixture'
PASSWORD = 'TestTicket123!'
USERS = {'alice@example.test': 'native-fixture-alice', 'bob@example.test': 'native-fixture-bob'}
MAX_MEDIA_REQUESTS = 200


def source_hashes():
    """Freeze fixture/backend inputs at startup so an old process cannot attest new files."""
    files = [Path(__file__)]
    for directory, subdirs, names in os.walk(ROOT / 'backend'):
        subdirs[:] = [name for name in subdirs if not name.startswith('.') and name not in
                      {'__pycache__', 'tests', 'data', 'data-v2', 'data-v3'}]
        files.extend(Path(directory) / name for name in names if name.endswith('.py'))
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_text(encoding='utf-8').replace('\r\n', '\n').encode()).hexdigest()
            for path in sorted(files) if path.is_relative_to(ROOT)}


def safe_directory(path):
    path = Path(path).absolute()
    if os.name == 'nt' and path.drive.upper() != 'D:':
        raise ValueError('Fixture files must use D:')
    for item in (path, *path.parents):
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            raise ValueError('Fixture paths cannot traverse links')
    return path.resolve()


class LocalProvider:
    """Real JWT signatures with isolated in-memory refresh credentials per session."""
    def __init__(self, config):
        self.config = config
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.refreshes, self.lock = {}, threading.Lock()

    def jwks(self):
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {'keys': [{**key, 'kid': 'native-fixture', 'alg': 'RS256', 'use': 'sig'}]}

    def tokens(self, email, refresh):
        stamp = time.time()
        common = {'iss': self.config.issuer, 'sub': USERS[email], 'iat': stamp, 'exp': stamp + 900}
        identity = {**common, 'aud': self.config.client_id, 'token_use': 'id',
                    'email': email, 'email_verified': True, 'name': email.split('@')[0]}
        access = {**common, 'client_id': self.config.client_id, 'token_use': 'access'}
        return {'id_token': jwt.encode(identity, self.key, algorithm='RS256', headers={'kid': 'native-fixture'}),
                'access_token': jwt.encode(access, self.key, algorithm='RS256', headers={'kid': 'native-fixture'}),
                'refresh_token': refresh}

    def login(self, email, password):
        if email not in USERS or not equal_secret(password, PASSWORD):
            raise AuthError(401, '测试账号或口令不正确。', 'authentication_failed')
        with self.lock:
            if len(self.refreshes) >= 64:
                raise AuthError(429, '测试会话数量已达上限。', 'rate_limited')
            refresh = secrets.token_urlsafe(32)
            self.refreshes[refresh] = email
        return self.tokens(email, refresh)

    def refresh(self, refresh_token):
        with self.lock:
            email = self.refreshes.get(refresh_token)
        if email is None:
            raise AuthError(401, '测试会话已失效。', 'authentication_failed')
        return self.tokens(email, refresh_token)

    def revoke(self, refresh_token):
        with self.lock:
            self.refreshes.pop(refresh_token, None)

    @staticmethod
    def unsupported(*_args):
        raise AuthError(503, '本地测试服务仅提供预设邮箱登录。', 'provider_disabled')

    register = verify = resend = forgot = reset = exchange = unsupported


def application(directory, run_token, *, dist=None):
    directory = safe_directory(directory)
    if not re.fullmatch(r'[a-f0-9]{32}', run_token) or (directory / 'data').exists() or (directory / 'objects').exists():
        raise ValueError('Fixture requires an owned new data directory and run token')
    config = AuthConfig('ap-northeast-1', 'ap-northeast-1_nativefixture', 'nativefixture',
                        'https://fixture.invalid', ORIGIN, Fernet.generate_key().decode(), google_enabled=False)
    provider = LocalProvider(config)
    def authentication(app, root, *, on_authenticated):
        return install_auth(app, root, on_authenticated=on_authenticated, config=config, provider=provider)
    client = fakeredis.FakeRedis()
    storage = LocalObjectStore(directory / 'objects')
    try:
        app = create_app(directory / 'data', auth_provider=authentication, object_store=storage,
                         shared_state=RedisSharedState(client=client), run_recovery=False, run_processing=False)
    except BaseException:
        client.close()
        storage.close()
        raise
    app.state.fixture_provider = provider
    startup_sources = source_hashes()
    seeded, seed_lock = {}, threading.Lock()
    progress, media_requests, media_gates = {}, [], {}
    media_count, media_overflow = 0, False

    @app.exception_handler(AuthError)
    async def fixture_auth_error(_request, error):
        return JSONResponse({'detail': error.detail, 'code': error.code}, status_code=error.status,
                            headers={'Cache-Control': 'no-store'})

    @app.middleware('http')
    async def fixture_boundary(request, call_next):
        nonlocal media_count, media_overflow
        if request.url.path.startswith('/__test/'):
            if (request.client.host != '127.0.0.1'
                    or request.headers.get('X-MemoVerse-Fixture') != FIXTURE_HEADER
                    or not equal_secret(request.headers.get('X-MemoVerse-Fixture-Run'), run_token)
                    or request.headers.get('Origin', ORIGIN) != ORIGIN):
                return JSONResponse({'detail': 'Fixture access denied'}, status_code=403)
        if request.url.path == '/api/health':
            return JSONResponse({'ok': True, 'fixture': 'native-loopback', 'cloud': False, 'port': 4180})
        match = re.fullmatch(r'/api/media/([a-f0-9]{64})', request.url.path)
        row, gate = None, None
        if match and request.method == 'GET':
            with seed_lock:
                media_count += 1
                if len(media_requests) < MAX_MEDIA_REQUESTS:
                    row = {'sequence': media_count, 'client': 'android' if request.headers.get('X-MemoVerse-Client') == 'android' else 'web',
                           'assetId': match.group(1), 'status': None, 'finished': False}
                    media_requests.append(row)
                else:
                    media_overflow = True
                gate = media_gates.get(match.group(1))
                if gate is not None:
                    gate['started'] = True
        response = None
        try:
            if gate is not None:
                try:
                    await asyncio.wait_for(gate['event'].wait(), 30)
                except TimeoutError:
                    response = JSONResponse({'detail': 'Owned test media gate expired'}, status_code=503)
                    return response
            response = await call_next(request)
            return response
        finally:
            if row is not None:
                with seed_lock:
                    row.update(status=response.status_code if response else None, finished=True)

    def current(request):
        session = app.state.auth.session(request.cookies.get(app.state.auth.cookie_name))
        if session is None:
            raise HTTPException(401, 'Fixture account login required')
        return session

    @app.post('/__test/bootstrap')
    def bootstrap(request: Request, body: dict):
        if set(body) != {'count'} or isinstance(body['count'], bool) or not isinstance(body['count'], int) or not 1 <= body['count'] <= 300:
            raise HTTPException(400, 'Count must be an integer from 1 to 300')
        session = current(request)
        app.state.auth.check_write(request, session)
        user_id = session['user']['id']
        with seed_lock:
            if user_id in seeded:
                if seeded[user_id]['count'] != body['count']:
                    raise HTTPException(409, 'Account already seeded with another count')
                return seeded[user_id]
            generator = random.Random(20261022)
            media = []
            for index in range(min(2, body['count'])):
                with Image.frombytes('RGB', (512, 768), generator.randbytes(512 * 768 * 3)) as picture:
                    with io.BytesIO() as stream:
                        picture.save(stream, 'PNG')
                        raw = stream.getvalue()
                media.append(persist_image(app.state.store, user_id, f'native-upload-{index}', raw))
            items = []
            for index in range(body['count']):
                asset = media[index % len(media)]
                ticket_id = f'native-ticket-{index:03d}'
                tickets.apply(app.state.store, user_id, ticket_id, 'create', CreateTicket(
                    id=ticket_id, operationId=f'native-create-{index:03d}', originalAssetId=asset['assetId'],
                    fields={'title': f'原生联调合成电影票 {index + 1:03d}'}))
                items.append({'ticketId': ticket_id, 'media': asset})
            seeded[user_id] = {'userId': user_id, 'count': body['count'], 'seed': 20261022, 'items': items}
            return seeded[user_id]

    @app.get('/__test/manifest')
    def manifest(request: Request):
        user_id = current(request)['user']['id']
        with seed_lock:
            result = seeded.get(user_id)
            if result is None:
                raise HTTPException(404, 'No synthetic dataset for this account')
            with app.state.store.transaction() as db:
                for item in result['items']:
                    app.state.store.referenced_asset(db, user_id, item['media']['assetId'])
            return result

    @app.post('/__test/shutdown')
    def shutdown():
        app.state.fixture_shutdown()
        return {'ok': True, 'runId': directory.name}

    @app.post('/__test/reset-auth-limits')
    def reset_auth_limits():
        # Explicit run-bound test preparation only; production limits are unchanged.
        limiter = app.state.auth.limiter
        with limiter.lock:
            limiter.entries.clear()
        return {'ok': True}

    @app.get('/__test/progress')
    def progress_status():
        with seed_lock:
            return {'phases': dict(progress), 'mediaRequests': [dict(row) for row in media_requests],
                    'mediaRequestCount': media_count, 'mediaRequestsOverflow': media_overflow,
                    'sourceSha256AtStartup': startup_sources,
                    'gateStarted': [key for key, gate in media_gates.items() if gate['started']]}

    @app.post('/__test/progress')
    def progress_write(body: dict):
        if (set(body) != {'phase', 'data'} or not isinstance(body['phase'], str)
                or not re.fullmatch(r'[a-z_]{1,40}', body['phase']) or not isinstance(body['data'], dict)
                or len(json.dumps(body)) > 4096):
            raise HTTPException(400, 'Invalid test progress')
        with seed_lock:
            if len(progress) >= 32 and body['phase'] not in progress:
                raise HTTPException(400, 'Test progress capacity exceeded')
            progress[body['phase']] = body['data']
        return {'ok': True}

    @app.post('/__test/media-gate')
    async def media_gate(body: dict):
        if (set(body) != {'assetId', 'action'} or not isinstance(body['assetId'], str)
                or not re.fullmatch(r'[a-f0-9]{64}', body['assetId']) or body['action'] not in ('hold', 'release')):
            raise HTTPException(400, 'Invalid media gate')
        key = body['assetId']
        with seed_lock:
            if body['action'] == 'release':
                gate = media_gates.pop(key, None)
                if gate:
                    gate['event'].set()
            else:
                if media_gates:
                    raise HTTPException(409, 'Only one owned media gate may be active')
                media_gates[key] = {'event': asyncio.Event(), 'started': False}
        return {'ok': True}

    @app.get('/{path:path}')
    def static(path: str):
        if path.startswith(('api/', '__test/')):
            raise HTTPException(404)
        directory = safe_directory(dist or ROOT / 'demo/dist/client')
        target = safe_directory(directory / path)
        if not target.is_relative_to(directory):
            raise HTTPException(404)
        return FileResponse(target if target.is_file() else directory / 'index.html')
    return app, client


def run_fixture():
    dist = safe_directory(ROOT / 'demo/dist/client')
    if not (dist / 'index.html').is_file():
        raise SystemExit('Build the Web client before starting the native fixture')
    cache = safe_directory(ROOT / '.cache')
    cache.mkdir(exist_ok=True)
    run_token = os.environ.get('MEMOVERSE_NATIVE_RUN_TOKEN', '')
    if not re.fullmatch(r'[a-f0-9]{32}', run_token):
        raise SystemExit('Launch with a random MEMOVERSE_NATIVE_RUN_TOKEN ownership key')
    os.environ['AWS_EC2_METADATA_DISABLED'] = 'true'
    manifest = cache / f'native-run-{run_token}.json'
    owns_manifest, directory = False, None
    try:
        with tempfile.TemporaryDirectory(prefix='native-browser-', dir=cache) as temporary:
            directory = safe_directory(temporary)
            if directory.parent != cache or not re.fullmatch(r'native-browser-[a-z0-9_]{8}', directory.name):
                raise SystemExit('Unexpected fixture directory ownership')
            with manifest.open('x', encoding='utf-8') as output:
                json.dump({'runId': directory.name, 'port': 4180}, output)
            owns_manifest = True
            app, client = application(directory, run_token, dist=dist)
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=4180,
                ws='websockets-sansio', ws_max_size=65536, ws_ping_interval=15, ws_ping_timeout=15,
                ws_per_message_deflate=False, log_level='warning', access_log=False, timeout_graceful_shutdown=5))
            app.state.fixture_shutdown = lambda: setattr(server, 'should_exit', True)
            try:
                server.run()
            finally:
                client.close()
    finally:
        if owns_manifest and directory is not None and not directory.exists():
            manifest.unlink(missing_ok=True)


if __name__ == '__main__':
    run_fixture()

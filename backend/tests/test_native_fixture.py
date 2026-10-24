"""Native loopback fixture exercises actual BFF sessions and never opens a port."""
import hashlib
import importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import time

from fastapi.testclient import TestClient
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('native_fixture', ROOT / 'backend/tests/native-server.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
TOKEN = '1' * 32
OWNED = {'X-MemoVerse-Fixture': fixture.FIXTURE_HEADER, 'X-MemoVerse-Fixture-Run': TOKEN}
ANDROID = {'Origin': fixture.ORIGIN, 'X-MemoVerse-Client': 'android'}


def login(client, who='alice', platform='android'):
    response = client.post('/api/auth/login', headers={**ANDROID, 'X-MemoVerse-Client': platform},
                           json={'email': f'{who}@example.test', 'password': fixture.PASSWORD})
    assert response.status_code == 200, response.text
    assert 'id_token' not in response.text and 'refresh_token' not in response.text
    assert 'HttpOnly' in response.headers['set-cookie']
    return response.json()


def test_native_fixture_real_bff_isolation_and_explicit_synthetic_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr('backend.auth.boto3.client', lambda *a, **k: pytest.fail('Fixture must never connect to AWS'))
    app, redis = fixture.application(tmp_path / 'run', TOKEN)
    with TestClient(app, base_url=fixture.ORIGIN, client=('127.0.0.1', 12345)) as client:
        assert client.get('/api/health').json()['fixture'] == 'native-loopback'
        assert client.get('/api/sync/snapshot').status_code == 401
        assert client.get('/__test/manifest').status_code == 403
        assert client.get('/__test/manifest', headers=OWNED).status_code == 401
        assert client.post('/api/auth/login', headers=ANDROID, json={'email': 'alice@example.test', 'password': 'WrongTicket123!'}).status_code == 401
        state = login(client)
        assert state['providers'] == {'email': True, 'google': False}
        assert client.get('/api/sync/snapshot').json()['total'] == 0
        sid = client.cookies.get(app.state.auth.cookie_name)
        assert app.state.auth.websocket_session(sid, fixture.ORIGIN)['user']['id'] == state['user']['id']
        assert client.post('/__test/bootstrap', headers=OWNED, json={'count': 3}).status_code == 403
        write = {**ANDROID, **OWNED, 'X-CSRF-Token': state['csrfToken']}
        response = client.post('/__test/bootstrap', headers=write, json={'count': 3})
        assert response.status_code == 200, response.text
        dataset = response.json()
        assert dataset['count'] == 3 and len(dataset['items']) == 3
        assert len({item['media']['assetId'] for item in dataset['items']}) == 2
        assert client.get('/__test/manifest', headers=OWNED).json() == dataset
        assert client.post('/__test/bootstrap', headers=write, json={'count': 3}).json() == dataset
        assert client.post('/__test/bootstrap', headers=write, json={'count': 4}).status_code == 409
        media = dataset['items'][0]['media']
        raw = client.get(media['url'])
        assert raw.status_code == 200 and len(raw.content) == media['size'] > 1024 * 1024
        assert hashlib.sha256(raw.content).hexdigest() == media['sha256']
        alice_id = state['user']['id']
        assert client.post('/api/auth/logout', headers={**ANDROID, 'X-CSRF-Token': state['csrfToken']}).status_code == 200
        assert not app.state.fixture_provider.refreshes
        with pytest.raises(fixture.AuthError):
            app.state.auth.websocket_session(sid, fixture.ORIGIN)
        state = login(client, 'alice', platform='web')
        assert state['user']['id'] == alice_id
        assert client.get('/api/sync/snapshot').json()['total'] == 3
        state = login(client, 'bob')
        assert state['user']['id'] != alice_id
        assert client.get('/api/sync/snapshot').json()['total'] == 0
        assert client.get(media['url']).status_code == 404
        assert client.get('/__test/manifest', headers=OWNED).status_code == 404
    redis.close()


def test_native_fixture_lifecycle_does_not_adopt_foreign_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(fixture, 'ROOT', tmp_path)
    monkeypatch.setenv('MEMOVERSE_NATIVE_RUN_TOKEN', TOKEN)
    dist = tmp_path / 'demo/dist/client'
    dist.mkdir(parents=True)
    (dist / 'index.html').write_text('synthetic client', encoding='utf-8')
    cache = tmp_path / '.cache'
    cache.mkdir()
    sentinel = cache / 'retained.txt'
    sentinel.write_text('retained', encoding='utf-8')
    class Server:
        def __init__(self, config):
            self.app = config.app
            self.should_exit = False
        def run(self):
            assert (cache / f'native-run-{TOKEN}.json').exists()
            with TestClient(self.app, base_url=fixture.ORIGIN, client=('127.0.0.1', 12345)) as client:
                bad = {**OWNED, 'X-MemoVerse-Fixture-Run': '0' * 32}
                assert client.post('/__test/shutdown', headers=bad).status_code == 403
                assert not self.should_exit
                assert client.post('/__test/shutdown', headers=OWNED).status_code == 200
                assert self.should_exit
    monkeypatch.setattr(fixture.uvicorn, 'Server', Server)
    fixture.run_fixture()
    assert list(cache.iterdir()) == [sentinel]
    manifest = cache / f'native-run-{TOKEN}.json'
    manifest.write_text('foreign-owner', encoding='utf-8')
    with pytest.raises(FileExistsError):
        fixture.run_fixture()
    assert manifest.read_text(encoding='utf-8') == 'foreign-owner'
    assert sorted(path.name for path in cache.iterdir()) == sorted([sentinel.name, manifest.name])


def test_owned_progress_and_media_gate_record_requests_before_completion_and_expose_overflow(tmp_path, monkeypatch):
    monkeypatch.setattr(fixture, 'MAX_MEDIA_REQUESTS', 2)
    app, redis = fixture.application(tmp_path / 'run', TOKEN)
    with TestClient(app, base_url=fixture.ORIGIN, client=('127.0.0.1', 12345)) as client:
        state = login(client)
        write = {**ANDROID, **OWNED, 'X-CSRF-Token': state['csrfToken']}
        seeded = client.post('/__test/bootstrap', headers=write, json={'count': 1}).json()
        media = seeded['items'][0]['media']
        gate = {'assetId': media['assetId'], 'action': 'hold'}
        assert client.post('/__test/media-gate', json=gate).status_code == 403
        assert client.get('/__test/progress', headers={**OWNED, 'Origin': 'https://foreign.test'}).status_code == 403
        assert client.post('/__test/progress', headers=OWNED, json={'phase': 'web_ready', 'data': {'assetId': media['assetId']}}).status_code == 200
        assert client.post('/__test/media-gate', headers=OWNED, json=gate).status_code == 200
        assert client.post('/__test/media-gate', headers=OWNED, json=gate).status_code == 409
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(client.get, media['url'])
            try:
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    progress = client.get('/__test/progress', headers=OWNED).json()
                    if media['assetId'] in progress['gateStarted']:
                        break
                    time.sleep(0.01)
                assert media['assetId'] in progress['gateStarted']
                assert not pending.done()
                assert progress['mediaRequestCount'] == 1 and not progress['mediaRequestsOverflow']
                assert progress['mediaRequests'][0]['status'] is None and not progress['mediaRequests'][0]['finished']
            finally:
                assert client.post('/__test/media-gate', headers=OWNED, json={**gate, 'action': 'release'}).status_code == 200
            assert hashlib.sha256(pending.result(timeout=3).content).hexdigest() == media['sha256']
        assert client.get('/api/media/' + '0' * 64).status_code == 404
        assert client.get(media['url']).status_code == 200
        progress = client.get('/__test/progress', headers=OWNED).json()
        assert progress['mediaRequestCount'] == 3 and progress['mediaRequestsOverflow']
        assert len(progress['mediaRequests']) == 2
        assert [row['sequence'] for row in progress['mediaRequests']] == [1, 2]
        assert [row['status'] for row in progress['mediaRequests']] == [200, 404]
        assert all(row['finished'] for row in progress['mediaRequests']) and progress['gateStarted'] == []
        assert progress['sourceSha256AtStartup'] == fixture.source_hashes()
        assert 'backend/tests/native-server.py' in progress['sourceSha256AtStartup']
        assert TOKEN not in str(progress) and 'cookie' not in str(progress).lower()
        assert client.post('/__test/progress', headers=OWNED, json={'phase': 'bad-phase', 'data': {}}).status_code == 400
        assert client.post('/__test/progress', headers=OWNED, json={'phase': 'huge', 'data': {'value': 'x' * 4096}}).status_code == 400
    redis.close()


def test_owned_auth_limit_reset_preserves_real_login_validation(tmp_path):
    app, redis = fixture.application(tmp_path / 'run', TOKEN)
    with TestClient(app, base_url=fixture.ORIGIN, client=('127.0.0.1', 12345)) as client:
        for _ in range(5):
            login(client)
        body = {'email': 'alice@example.test', 'password': fixture.PASSWORD}
        assert client.post('/api/auth/login', headers=ANDROID, json=body).status_code == 429
        bad = {**OWNED, 'X-MemoVerse-Fixture-Run': '0' * 32}
        assert client.post('/__test/reset-auth-limits', headers=bad).status_code == 403
        assert client.post('/api/auth/login', headers=ANDROID, json=body).status_code == 429
        assert client.post('/__test/reset-auth-limits', headers=OWNED).json() == {'ok': True}
        session = login(client)
        assert client.get('/api/auth/session').json()['user'] == session['user']
        sid = client.cookies.get(app.state.auth.cookie_name)
        assert app.state.auth.websocket_session(sid, fixture.ORIGIN)['user'] == session['user']
    redis.close()

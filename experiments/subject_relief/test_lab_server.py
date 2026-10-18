"""Local laboratory API and real subprocess lifecycle tests; never run AI."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import time
import uuid

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from experiments.subject_relief.lab_server import create_app


ORIGIN = 'http://127.0.0.1:4183'
WORKER = r'''
import json, struct, sys, time
from pathlib import Path
from PIL import Image
folder, stage, mode = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
print(json.dumps({'event':'progress','stage':stage,'message':'test child started','current':1,'total':2,'cache_hit':False}), flush=True)
if mode == 'slow':
    time.sleep(60)
if mode == 'fail':
    print('Traceback: D:\private\secret.py', file=sys.stderr, flush=True)
    raise SystemExit(7)
if stage == 'mask':
    Image.new('RGBA',(16,12),(220,210,200,255)).save(folder/'source.png')
    Image.new('L',(16,12),255).save(folder/'foreground-mask.png')
    (folder/'mask-stage.json').write_text('{"ok":true}')
else:
    Image.new('RGBA',(16,12),(150,210,180,255)).save(folder/'overlay.png')
    (folder/'report.json').write_text(json.dumps({'ok':True,'private':{'model_dir':'D:\private\model'},'note':'D:\private\secret'}))
    if mode == 'bad-glb':
        (folder/'model.glb').write_bytes(b'not a model')
    else:
        binary=struct.pack('<9f3H',0,0,0,1,0,0,0,1,0,0,1,2)+b'\x00\x00'
        document={'asset':{'version':'2.0'},'scene':0,'scenes':[{'nodes':[0]}],
          'nodes':[{'mesh':0}],'meshes':[{'primitives':[{'attributes':{'POSITION':0},'indices':1}]}],
          'buffers':[{'byteLength':len(binary)}],
          'bufferViews':[{'buffer':0,'byteOffset':0,'byteLength':36},{'buffer':0,'byteOffset':36,'byteLength':6}],
          'accessors':[{'bufferView':0,'componentType':5126,'count':3,'type':'VEC3','min':[0,0,0],'max':[1,1,0]},
                       {'bufferView':1,'componentType':5123,'count':3,'type':'SCALAR'}]}
        encoded=json.dumps(document,separators=(',',':')).encode()
        encoded+=b' '*((-len(encoded))%4)
        content=struct.pack('<II',len(encoded),0x4e4f534a)+encoded+struct.pack('<II',len(binary),0x004e4942)+binary
        (folder/'model.glb').write_bytes(struct.pack('<4sII',b'glTF',2,12+len(content))+content)
'''


def image_bytes(size=(16, 12)):
    output = io.BytesIO()
    Image.new('RGB', size, (220, 210, 200)).save(output, format='PNG')
    return output.getvalue()


def command_factory(mode='success'):
    return lambda folder, stage: [sys.executable, '-u', '-B', '-c', WORKER, str(folder), stage, mode]


def app_for(tmp_path, *, factory=None, seed=False):
    ui = tmp_path / 'ui'
    ui.mkdir(exist_ok=True)
    (ui / 'index.html').write_text('<!doctype html><title>Lab</title>', encoding='utf-8')
    (ui / 'lab.bundle.js').write_text('void 0;', encoding='utf-8')
    (ui / 'styles.css').write_text('body { color: black; }', encoding='utf-8')
    return create_app(data_dir=tmp_path / 'data', ui_dir=ui,
                      worker_command_factory=factory or command_factory(), seed_default=seed)


def auth(client):
    bootstrap = client.get('/api/bootstrap')
    assert bootstrap.status_code == 200, bootstrap.text
    return {'Origin': ORIGIN, 'X-Lab-Token': bootstrap.json()['token']}


def upload(client, headers, *, kind='image', size=(16, 12), name='test.png'):
    response = client.post('/api/images', params={'kind': kind, 'name': name},
                           content=image_bytes(size), headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


def start(client, headers, image, **kwargs):
    return client.post('/api/runs', json={'image_id': image['id'], 'parameters': {}, 'boxes': [], **kwargs}, headers=headers)


def wait_for(client, identifier, statuses, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get('/api/runs/' + identifier)
        assert response.status_code == 200, response.text
        record = response.json()
        if record['status'] in statuses:
            return record
        time.sleep(0.03)
    pytest.fail('local fake worker did not reach expected status')


def wait_started(app):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with app.state.lab_runtime.lock:
            active = app.state.lab_runtime.active
            if active and active.process is not None:
                return active.process
        time.sleep(0.02)
    pytest.fail('local fake child did not start')


def test_loopback_host_origin_and_token_boundary(tmp_path):
    with TestClient(app_for(tmp_path), base_url=ORIGIN) as client:
        headers = auth(client)
        assert client.get('/api/bootstrap', headers={'Host': 'evil.example:4183'}).status_code == 400
        assert client.get('/api/bootstrap', headers={'Host': '127.0.0.1:9999'}).status_code == 400
        assert client.post('/api/images', content=image_bytes()).status_code == 403
        assert client.post('/api/images', content=image_bytes(), headers={'Origin': ORIGIN}).status_code == 403
        assert client.post('/api/images', content=image_bytes(), headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
        response = client.post('/api/images', content=image_bytes(), headers=headers)
        assert response.status_code == 201
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['cross-origin-resource-policy'] == 'same-origin'
        assert 'access-control-allow-origin' not in response.headers


def test_upload_limits_validation_and_restricted_static_paths(tmp_path):
    with TestClient(app_for(tmp_path), base_url=ORIGIN) as client:
        headers = auth(client)
        response = client.post('/api/images', content=b'x', headers={**headers, 'Content-Length': str(12 * 1024 * 1024 + 1)})
        assert response.status_code == 413
        assert client.post('/api/images', content=b'not an image', headers=headers).status_code == 422
        assert client.post('/api/images?kind=arbitrary', content=image_bytes(), headers=headers).status_code == 422
        image = upload(client, headers, name='../../outside.png')
        assert image['name'] == 'outside.png'
        assert client.get(image['preview_url']).status_code == 200
        assert client.get('/').status_code == 200
        for path in ('/api/images/../preview', '/api/references/unknown/model', '/styles.css/../secret',
                     '/api/images/%2e%2e%2fsecret/preview', '/api/references/%2e%2e%2fsecret/model', '/jobrun.log'):
            assert client.get(path).status_code == 404


def test_success_readback_download_and_private_report_fields(tmp_path):
    app = app_for(tmp_path)
    with TestClient(app, base_url=ORIGIN) as client:
        headers = auth(client)
        image = upload(client, headers)
        response = start(client, headers, image, title='Test run')
        assert response.status_code == 201, response.text
        identifier = response.json()['id']
        record = wait_for(client, identifier, {'ready', 'failed'})
        assert record['status'] == 'ready', record
        assert record['title'] == 'Test run'
        assert record['created_at'].endswith('Z') and record['finished_at'].endswith('Z')
        assert record['elapsed_seconds'] >= 0
        assert record['parameters']
        assert set(record['files']) == {'model', 'original', 'overlay', 'mask', 'report', 'parameters'}
        assert record['report']['ok'] is True
        assert 'D:\\private' not in json.dumps(record)
        assert record['report']['private'] == {}
        model = client.get(record['files']['model'])
        assert model.status_code == 200 and model.content[:4] == b'glTF'
        assert 'attachment' in model.headers['content-disposition']
        assert client.get(record['files']['parameters']).json() == record['parameters']
        assert 'model_dir' not in client.get(record['files']['report']).text
        assert client.get(f'/api/runs/{identifier}/files/request.json').status_code == 404
        assert client.get(f'/api/runs/{identifier}/files/jobrun.log').status_code == 404
        assert client.get('/api/runs').json()['active_run_id'] is None
    with TestClient(app_for(tmp_path), base_url=ORIGIN) as client:
        restored = client.get('/api/runs/' + identifier).json()
        assert restored['status'] == 'ready'
        assert restored['parameters'] == record['parameters']


def test_busy_cancel_reaps_child_and_releases_slot(tmp_path):
    first_folder = []

    def factory(folder, stage):
        if not first_folder:
            first_folder.append(folder)
        return command_factory('slow' if folder == first_folder[0] else 'success')(folder, stage)

    app = app_for(tmp_path, factory=factory)
    with TestClient(app, base_url=ORIGIN) as client:
        headers = auth(client)
        image = upload(client, headers)
        response = start(client, headers, image)
        assert response.status_code == 201
        identifier = response.json()['id']
        process = wait_started(app)
        assert start(client, headers, image).status_code == 409
        assert client.get(f'/api/runs/{identifier}/files/model.glb').status_code == 409
        response = client.post(f'/api/runs/{identifier}/cancel', headers=headers)
        assert response.status_code == 200, response.text
        record = wait_for(client, identifier, {'cancelled'})
        assert process.poll() is not None
        assert record['files'] == {}
        assert client.get('/api/runs').json()['active_run_id'] is None
        assert client.post(f'/api/runs/{identifier}/cancel', headers=headers).status_code == 200
        next_run = start(client, headers, image)
        assert next_run.status_code == 201
        assert wait_for(client, next_run.json()['id'], {'ready', 'failed'})['status'] == 'ready'


def test_shutdown_reaps_process_and_persists_interrupted(tmp_path):
    app = app_for(tmp_path, factory=command_factory('slow'))
    with TestClient(app, base_url=ORIGIN) as client:
        headers = auth(client)
        identifier = start(client, headers, upload(client, headers)).json()['id']
        process = wait_started(app)
    assert process.poll() is not None
    record = json.loads((tmp_path / 'data/runs' / identifier / 'job.json').read_text(encoding='utf-8'))
    assert record['status'] == 'interrupted'


@pytest.mark.parametrize('previous_status', ['running', 'cancelling'])
def test_restart_marks_unfinished_history_interrupted_without_launching(tmp_path, previous_status):
    identifier = str(uuid.uuid4())
    folder = tmp_path / 'data/runs' / identifier
    folder.mkdir(parents=True)
    (folder / 'job.json').write_text(json.dumps({'id': identifier, 'status': previous_status,
                                              'created_at': '2026-10-19T00:00:00Z'}), encoding='utf-8')

    def unexpected(*args):
        pytest.fail('recovery must not restart inference')

    with TestClient(app_for(tmp_path, factory=unexpected), base_url=ORIGIN) as client:
        record = client.get('/api/runs/' + identifier).json()
        assert record['status'] == 'interrupted'
        assert record['finished_at'].endswith('Z')
        assert client.get('/api/runs').json()['active_run_id'] is None


@pytest.mark.parametrize('mode', ['fail', 'bad-glb'])
def test_failed_or_invalid_outputs_are_not_published(tmp_path, mode):
    with TestClient(app_for(tmp_path, factory=command_factory(mode)), base_url=ORIGIN) as client:
        headers = auth(client)
        identifier = start(client, headers, upload(client, headers)).json()['id']
        record = wait_for(client, identifier, {'ready', 'failed'})
        assert record['status'] == 'failed'
        assert record['files'] == {}
        assert 'Traceback' not in record['error'] and 'private' not in record['error']
        assert client.get(f'/api/runs/{identifier}/files/model.glb').status_code == 409


def test_run_payload_cannot_supply_paths_or_mismatched_masks(tmp_path):
    with TestClient(app_for(tmp_path), base_url=ORIGIN) as client:
        headers = auth(client)
        image = upload(client, headers)
        mask = upload(client, headers, kind='mask', size=(8, 8))
        assert start(client, headers, image, source_path='D:/anything').status_code == 422
        assert start(client, headers, image, mask_image_id=mask['id']).status_code == 422
        assert start(client, headers, image, boxes=[[0, 0, 2, 1]]).status_code == 422
        assert start(client, headers, image, parameters={'not_a_parameter': 1}).status_code == 422
        assert client.post('/api/runs', json={'image_id': '../outside'}, headers=headers).status_code == 404


def test_default_seed_is_copied_without_changing_original(tmp_path):
    source = tmp_path / 'original.png'
    original = image_bytes()
    source.write_bytes(original)
    with TestClient(app_for(tmp_path, seed=source), base_url=ORIGIN) as client:
        bootstrap = client.get('/api/bootstrap').json()
        identifier = bootstrap['default_image_id']
        assert identifier and len(identifier) == 64
        assert client.get(f'/api/images/{identifier}/preview').content == original
        assert source.read_bytes() == original


def test_provided_mask_requirement_and_lower_resolution_mask(tmp_path):
    with TestClient(app_for(tmp_path), base_url=ORIGIN) as client:
        headers = auth(client)
        image = upload(client, headers)
        assert start(client, headers, image, parameters={'mask_mode': 'provided'}).status_code == 422
        response = start(client, headers, image, parameters={'mask_mode': 'provided', 'background_flatten': 0})
        assert response.status_code == 201
        assert wait_for(client, response.json()['id'], {'ready', 'failed'})['status'] == 'ready'
        mask = upload(client, headers, kind='mask', size=(8, 6))
        response = start(client, headers, image, parameters={'mask_mode': 'provided'}, mask_image_id=mask['id'])
        assert response.status_code == 201
        assert wait_for(client, response.json()['id'], {'ready', 'failed'})['status'] == 'ready'

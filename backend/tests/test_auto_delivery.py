"""Deployment state-machine contracts with synthetic S3 and no live credentials."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'deploy/ubuntu'))
from release_manifest import LATEST_KEY, decode, encode, manifest


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


worker = module('pull_release', 'deploy/ubuntu/pull-release.py')
publisher = module('publish_release', 'scripts/publish-release.py')
A, B = 'a' * 40, 'b' * 40
RAW = b'synthetic verified source archive'
CANDIDATE = manifest(B, hashlib.sha256(RAW).hexdigest(), len(RAW))


class Body(io.BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


class Missing(Exception):
    pass


class S3:
    exceptions = SimpleNamespace(NoSuchKey=Missing)

    def __init__(self, objects):
        self.objects = dict(objects)
        self.calls, self.puts, self.bodies = [], [], []
        self.metadata = {}

    def get_object(self, **kwargs):
        self.calls.append(kwargs['Key'])
        if kwargs['Key'] not in self.objects:
            raise Missing()
        body = Body(self.objects[kwargs['Key']])
        self.bodies.append(body)
        return {'Body': body, 'ContentLength': len(body.getvalue())}

    def list_objects_v2(self, **kwargs):
        return {'Contents': [{'Key': key} for key in sorted(self.objects) if key.startswith(kwargs['Prefix'])][:2]}

    def put_object(self, **kwargs):
        raw = kwargs['Body']
        self.objects[kwargs['Key']] = raw.read() if hasattr(raw, 'read') else raw
        self.metadata[kwargs['Key']] = kwargs.get('Metadata', {})
        self.puts.append(kwargs['Key'])

    def head_object(self, **kwargs):
        return {'ContentLength': len(self.objects[kwargs['Key']]), 'Metadata': self.metadata.get(kwargs['Key'], {})}


@pytest.fixture
def host(tmp_path, monkeypatch):
    base = tmp_path / 'opt'
    state = tmp_path / 'state'
    (base / 'activation-backups').mkdir(parents=True)
    (base / 'releases').mkdir()
    state.mkdir()
    monkeypatch.setattr(worker, 'BASE', base)
    monkeypatch.setattr(worker, 'RELEASES', base / 'releases')
    monkeypatch.setattr(worker, 'STATE', state)
    monkeypatch.setattr(worker, 'ADMIN', base / 'admin')
    monkeypatch.setattr(worker, 'trusted', lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, 'remaining', lambda: 1800)
    monkeypatch.setattr(worker, 'space', lambda _candidate: True)
    monkeypatch.setattr(worker, 'healthy', lambda: True)
    monkeypatch.setattr(worker, 'enabled', lambda: True)
    active = {'commit': A}
    monkeypatch.setattr(worker, 'current', lambda: active['commit'])
    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None))
    calls = []
    def stage(command):
        calls.append(command)
        if command[0] == '/bin/bash':
            active['commit'] = B
        return 0, ''
    monkeypatch.setattr(worker, 'stage', stage)
    return SimpleNamespace(base=base, state=state, active=active, stages=calls,
                           s3=S3({LATEST_KEY: encode(CANDIDATE), CANDIDATE['key']: RAW}))


def test_same_version_is_a_noop_without_downloading_or_restarting(host):
    host.active['commit'] = B
    assert worker.pull(host.s3) == {'result': 'current', 'commit': B}
    assert host.s3.calls == [LATEST_KEY] and host.stages == []
    assert all(body.closed for body in host.s3.bodies)


def test_boot_without_candidate_and_near_shutdown_do_not_change_live_code(host, monkeypatch):
    assert worker.pull(S3({}))['result'] == 'no_candidate'
    monkeypatch.setattr(worker, 'remaining', lambda: 599)
    assert worker.pull(host.s3)['result'] == 'shutdown_window'
    assert host.s3.calls == [] and host.active['commit'] == A and host.stages == []


def test_candidate_is_verified_prepared_and_activated_with_exact_identity(host):
    assert worker.pull(host.s3)['result'] == 'deployed'
    assert host.stages[0][3:5] == [B, CANDIDATE['sha256']]
    assert host.stages[1] == ['/bin/bash', str(host.base / 'releases' / B / 'deploy/ubuntu/activate.sh'), str(host.base / 'releases' / B)]
    value = json.loads((host.state / 'state.json').read_text())
    assert value['observedBefore'] == A and value['blocked'] is False
    assert not list(host.state.glob('transfer-*'))


def test_integrity_failure_closes_body_and_never_runs_candidate_code(host):
    host.s3.objects[CANDIDATE['key']] = b'x' * len(RAW)
    with pytest.raises(ValueError, match='integrity'):
        worker.pull(host.s3)
    assert host.active['commit'] == A and host.stages == []
    assert all(body.closed for body in host.s3.bodies)
    assert not list(host.state.glob('transfer-*'))


def test_no_disk_or_manual_deployment_lock_does_not_consume_attempts(host, monkeypatch):
    monkeypatch.setattr(worker, 'space', lambda _: False)
    assert worker.pull(host.s3)['result'] == 'insufficient_disk'
    assert host.stages == []
    monkeypatch.setattr(sys.modules['fcntl'], 'flock', lambda *args: (_ for _ in ()).throw(BlockingIOError()))
    assert worker.pull(host.s3)['result'] == 'deployment_busy'
    assert host.stages == []


def test_failed_candidate_has_two_attempts_and_preserves_current(host, monkeypatch):
    monkeypatch.setattr(worker, 'stage', lambda command: (1, 'Dependency preparation failed'))
    assert worker.pull(host.s3)['result'] == 'prepare_failed'
    assert worker.pull(host.s3)['result'] == 'prepare_failed'
    assert worker.pull(host.s3)['result'] == 'candidate_failed_twice'
    assert host.active['commit'] == A
    assert json.loads((host.state / 'state.json').read_text())['attempts'] == 2


def test_activation_failure_verifies_real_current_before_claiming_rollback(host, monkeypatch):
    monkeypatch.setattr(worker, 'stage', lambda command: (0, '') if command[0] != '/bin/bash' else (1, 'rolled back'))
    assert worker.pull(host.s3)['result'] == 'activation_failed_rolled_back'
    assert host.active['commit'] == A
    assert json.loads((host.state / 'state.json').read_text())['blocked'] is False


def test_unknown_activation_interruption_blocks_further_automatic_deployments(host, monkeypatch):
    def interrupted(command):
        if command[0] == '/bin/bash':
            raise InterruptedError()
        return 0, ''
    monkeypatch.setattr(worker, 'stage', interrupted)
    with pytest.raises(InterruptedError):
        worker.pull(host.s3)
    assert json.loads((host.state / 'state.json').read_text())['blocked'] is True
    host.s3.calls.clear()
    assert worker.pull(host.s3)['result'] == 'manual_recovery_required'
    assert host.s3.calls == []


@pytest.mark.parametrize('change', [{'commit': '../escape'}, {'key': 'media/private'}, {'size': True}, {'version': True}, {'unexpected': 1}])
def test_manifest_rejects_path_escape_wrong_prefix_or_schema(change):
    with pytest.raises(ValueError):
        decode(json.dumps({**CANDIDATE, **change}).encode())


def test_manifest_rejects_duplicate_keys():
    with pytest.raises(ValueError):
        decode(encode(CANDIDATE)[:-1] + b',"commit":"' + B.encode() + b'"}')


def test_publisher_reuses_same_commit_identity_and_never_publishes_unchecked_head(tmp_path, monkeypatch):
    archive = tmp_path / 'release.tar'
    archive.write_bytes(RAW)
    s3 = S3({})
    monkeypatch.setattr(publisher, 'ready', lambda *args: True)
    assert publisher.publish(s3, worker.BUCKET, B, archive, 'synthetic') ['published'] is True
    archive.write_bytes(b'another build must not replace this commit')
    assert publisher.publish(s3, worker.BUCKET, B, archive, 'synthetic')['sha256'] == CANDIDATE['sha256']
    assert s3.puts.count(CANDIDATE['key']) == 1
    monkeypatch.setattr(publisher, 'ready', lambda *args: False)
    before = s3.puts.count(LATEST_KEY)
    assert publisher.publish(s3, worker.BUCKET, B, archive, 'synthetic')['published'] is False
    assert s3.puts.count(LATEST_KEY) == before


def test_checks_require_exact_main_commit_and_latest_successful_check_attempt(monkeypatch):
    monkeypatch.setenv('GITHUB_RUN_ID', '10')
    checks = [{'id': index, 'head_sha': B, 'name': name, 'status': 'completed', 'conclusion': 'success',
               'app': {'slug': 'github-actions'}, 'check_suite': {'id': 200 if name == 'deployment-recovery' else 100}}
              for index, name in enumerate(sorted(publisher.REQUIRED), 1)]
    def github(path, token):
        if path.endswith('/main'):
            return {'object': {'sha': B}}
        if '/actions/runs?' in path:
            return {'total_count': 2, 'workflow_runs': [
                {'id': 10, 'path': '.github/workflows/application-ci.yml', 'head_sha': B, 'head_branch': 'main', 'event': 'push', 'check_suite_id': 100},
                {'id': 20, 'path': '.github/workflows/deployment-recovery.yml', 'head_sha': B, 'head_branch': 'main', 'event': 'push', 'check_suite_id': 200, 'status': 'completed', 'conclusion': 'success'},
            ]}
        return {'total_count': len(checks), 'check_runs': checks}
    monkeypatch.setattr(publisher, 'github', github)
    assert publisher.ready(B, 'synthetic') is True
    checks[0]['app']['slug'] = 'untrusted-app'
    assert publisher.ready(B, 'synthetic') is False
    checks[0]['app']['slug'] = 'github-actions'
    checks.append({**checks[0], 'id': 100, 'conclusion': 'failure'})
    assert publisher.ready(B, 'synthetic') is False
    assert publisher.ready(A, 'synthetic') is False


def test_prepare_crossing_shutdown_window_defers_activation_without_consuming_attempt(host, monkeypatch):
    times = iter([1800, 1800, 599])
    monkeypatch.setattr(worker, 'remaining', lambda: next(times))
    assert worker.pull(host.s3)['result'] == 'shutdown_window'
    assert len(host.stages) == 1 and host.active['commit'] == A
    assert json.loads((host.state / 'state.json').read_text())['attempts'] == 0


def test_enable_failure_is_retried_when_new_release_is_auth_healthy(host, monkeypatch):
    monkeypatch.setattr(worker, 'enabled', lambda: False)
    def enable_fails(command):
        if command[0] == '/bin/bash':
            host.active['commit'] = B
            return 1, 'New release is healthy but enable-on-boot failed; retry activation.'
        return 0, ''
    monkeypatch.setattr(worker, 'stage', enable_fails)
    assert worker.pull(host.s3)['result'] == 'enable_retry_required'
    assert json.loads((host.state / 'state.json').read_text())['blocked'] is False


def test_service_runs_root_owned_admin_runtime_and_keeps_shutdown_policy_independent():
    service = (ROOT / 'deploy/ubuntu/keepsite-autodeploy.service').read_text()
    timer = (ROOT / 'deploy/ubuntu/keepsite-autodeploy.timer').read_text()
    assert '/opt/keepsite/admin/autodeploy-venv/bin/python' in service
    assert 'OnBootSec=2min' in timer and 'OnUnitInactiveSec=5min' in timer
    assert '/opt/keepsite/current' not in service and '/etc/keepsite.env' not in service
    assert 'keepsite-auto-stop' not in service + timer


def test_publisher_closes_sdk_client_without_requiring_context_manager(tmp_path, monkeypatch, capsys):
    class Client:
        closed = False
        def close(self):
            self.closed = True
    client = Client()
    monkeypatch.setenv('GITHUB_REPOSITORY', publisher.REPOSITORY)
    monkeypatch.setenv('GITHUB_TOKEN', 'synthetic')
    monkeypatch.setattr(sys, 'argv', ['publish', '--commit', B, '--bucket', 'synthetic'])
    monkeypatch.setattr(publisher, 'ready', lambda *args: True)
    monkeypatch.setattr(publisher, 'package', lambda commit, output: output.write_bytes(RAW))
    monkeypatch.setattr(publisher.boto3, 'client', lambda *args, **kwargs: client)
    monkeypatch.setattr(publisher, 'publish', lambda *args: {'published': True})
    publisher.main()
    assert client.closed is True
    assert 'published' in capsys.readouterr().out

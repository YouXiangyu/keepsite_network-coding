#!/usr/bin/env python3
"""Root-only pull deployment. A failed or unavailable candidate preserves current."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import urlopen

import boto3
from botocore.config import Config

from release_manifest import LATEST_KEY, MAX_MANIFEST, SHA, decode

BASE = Path('/opt/keepsite')
RELEASES = BASE / 'releases'
ADMIN = BASE / 'admin'
STATE = Path('/var/lib/keepsite-autodeploy')
BUCKET = 'memoverse-media-125647512944-apne1'
ACCOUNT = '125647512944'
STOP_AFTER = 58 * 60
MIN_REMAINING = 10 * 60
MIN_FREE = 384 * 1024 * 1024


def trusted(path, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
            or directory and not stat.S_ISDIR(info.st_mode)
            or not directory and not stat.S_ISREG(info.st_mode)):
        raise ValueError('Deployment management path is not root-controlled')


def current():
    link = BASE / 'current'
    if not link.exists() and not link.is_symlink():
        return None
    value = link.resolve(strict=True)
    if value.parent != RELEASES or SHA.fullmatch(value.name) is None:
        raise ValueError('Unexpected active release path')
    return value.name


def healthy():
    try:
        with urlopen('http://127.0.0.1:8000/api/health', timeout=5) as response:
            if json.loads(response.read(4096)).get('status') != 'ok':
                return False
        with urlopen('http://127.0.0.1:8000/api/auth/session', timeout=5) as response:
            if json.loads(response.read(4096)).get('configured') is not True:
                return False
        try:
            with urlopen('http://127.0.0.1:8000/api/sync/snapshot', timeout=5):
                return False
        except HTTPError as error:
            with closing(error):
                if error.code != 401:
                    return False
        return True
    except HTTPError as error:
        error.close()
        return False
    except (OSError, ValueError):
        return False


def state_read():
    path = STATE / 'state.json'
    if not path.exists():
        return {'attempts': 0}
    trusted(path)
    if path.stat().st_size > 64 * 1024:
        raise ValueError('Deployment state too large')
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or type(value.get('attempts')) is not int or not 0 <= value['attempts'] <= 2:
        raise ValueError('Invalid deployment state')
    return value


def save(value):
    descriptor, name = tempfile.mkstemp(prefix='.state-', dir=STATE)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, STATE / 'state.json')
    finally:
        Path(name).unlink(missing_ok=True)


def remaining():
    return STOP_AFTER - float(Path('/proc/uptime').read_text().split()[0])


def space(candidate):
    # Source, archive, a new venv, wheels and temporary installation all coexist.
    return shutil.disk_usage(BASE).free >= max(MIN_FREE, candidate['size'] * 4 + MIN_FREE)


def enabled():
    return subprocess.run(['systemctl', 'is-enabled', '--quiet', 'keepsite.service']).returncode == 0


def stage(command):
    # Keep child output private, and relay termination so activation may rollback.
    with tempfile.TemporaryFile() as output:
        child = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        prior = {}
        def terminate(signum, _frame):
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            raise InterruptedError('Deployment interrupted')
        try:
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                prior[sig] = signal.signal(sig, terminate)
            result = child.wait(timeout=420)
        finally:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
            for sig, handler in prior.items():
                signal.signal(sig, handler)
        output.seek(0)
        return result, output.read(1024 * 1024).decode(errors='replace')


def pull(s3):
    value = state_read()
    if value.get('blocked'):
        return {'result': 'manual_recovery_required'}
    if remaining() < MIN_REMAINING:
        return {'result': 'shutdown_window'}
    listed = s3.list_objects_v2(Bucket=BUCKET, Prefix=LATEST_KEY, MaxKeys=2, ExpectedBucketOwner=ACCOUNT)
    if not any(item['Key'] == LATEST_KEY for item in listed.get('Contents', [])):
        return {'result': 'no_candidate'}
    response = s3.get_object(Bucket=BUCKET, Key=LATEST_KEY, ExpectedBucketOwner=ACCOUNT)
    with closing(response['Body']) as body:
        if response['ContentLength'] > MAX_MANIFEST:
            raise ValueError('Manifest too large')
        candidate = decode(body.read(MAX_MANIFEST + 1))
    sha = candidate['commit']
    active = current()
    if sha == active and healthy() and enabled():
        value.update(commit=sha, attempts=0, result='current')
        save(value)
        return {'result': 'current', 'commit': sha}
    if value.get('commit') != sha:
        value.update(commit=sha, attempts=0)
    if value.get('attempts', 0) >= 2:
        return {'result': 'candidate_failed_twice', 'commit': sha}
    # Probe the shared lock, then release it before child helpers acquire it.
    import fcntl
    with (BASE / 'activation-backups/.lock').open('a') as deployment_lock:
        try:
            fcntl.flock(deployment_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'result': 'deployment_busy'}
    if not space(candidate):
        return {'result': 'insufficient_disk', 'commit': sha}
    with tempfile.TemporaryDirectory(prefix='transfer-', dir=STATE) as temporary:
        archive = Path(temporary) / 'release.tar'
        response = s3.get_object(Bucket=BUCKET, Key=candidate['key'], ExpectedBucketOwner=ACCOUNT)
        digest = hashlib.sha256()
        size = 0
        with closing(response['Body']) as body, archive.open('xb') as output:
            if response['ContentLength'] != candidate['size']:
                raise ValueError('Archive length mismatch')
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                size += len(chunk)
                if size > candidate['size']:
                    raise ValueError('Archive exceeds declared size')
                digest.update(chunk)
                output.write(chunk)
        if size != candidate['size'] or digest.hexdigest() != candidate['sha256']:
            raise ValueError('Archive integrity failure')
        if remaining() < MIN_REMAINING or not space(candidate):
            return {'result': 'resource_window_changed', 'commit': sha}
        release = RELEASES / sha
        value.update(attempts=value.get('attempts', 0) + 1, result='preparing')
        save(value)
        code, message = stage(['/usr/bin/python3', str(ADMIN / 'prepare-release.py'), str(archive), sha,
                               candidate['sha256'], 'keepsite.space'])
        if code != 0:
            if 'Another preparation or activation is running' in message:
                value['attempts'] -= 1
            value['result'] = 'prepare_failed'
            save(value)
            return {'result': value['result'], 'commit': sha}
        if remaining() < MIN_REMAINING:
            value.update(result='shutdown_window', attempts=value['attempts'] - 1)
            save(value)
            return {'result': 'shutdown_window', 'commit': sha}
        value.update(result='activating', observedBefore=active, blocked=True)
        save(value)
        code, message = stage(['/bin/bash', str(release / 'deploy/ubuntu/activate.sh'), str(release)])
        actual = current()
        if 'Another preparation or activation is running' in message:
            value.update(result='deployment_busy', blocked=False, attempts=value['attempts'] - 1)
        elif code == 0 and actual == sha and healthy() and enabled():
            value.update(result='deployed', attempts=0, blocked=False)
        elif actual == active and healthy() and 'ROLLBACK FAILED' not in message:
            value.update(result='activation_failed_rolled_back', blocked=False)
        elif actual == sha and healthy() and 'enable-on-boot failed' in message:
            value.update(result='enable_retry_required', blocked=False)
        else:
            value.update(result='manual_recovery_required', blocked=True)
        save(value)
        return {'result': value['result'], 'commit': sha, 'actual': actual}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({'plan': 'poll private main artifact, verify, prepare and activate', 'intervalSeconds': 300}))
        return
    if os.geteuid() != 0:
        raise SystemExit('Root required')
    for path in (BASE, RELEASES, ADMIN, STATE):
        trusted(path, directory=True)
    trusted(ADMIN / 'autodeploy-venv', directory=True)
    for name in ('pull-release.py', 'release_manifest.py', 'prepare-release.py'):
        trusted(ADMIN / name)
    def interrupted(_signum, _frame):
        raise InterruptedError('Deployment interrupted')
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, interrupted)
    import fcntl
    with (STATE / 'worker.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({'result': 'worker_busy'}))
            return
        try:
            with boto3.client('s3', region_name='ap-northeast-1', config=Config(
                    connect_timeout=5, read_timeout=15, retries={'total_max_attempts': 2, 'mode': 'standard'})) as s3:
                print(json.dumps(pull(s3)))
        except Exception as error:
            print(json.dumps({'result': 'error', 'errorType': type(error).__name__}))
            raise SystemExit(1)


if __name__ == '__main__':
    main()

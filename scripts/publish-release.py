"""Publish one verified main artifact; no deployment credentials are stored in Git."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.request import Request, urlopen

import boto3
from botocore.config import Config

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'deploy/ubuntu'))
from release_manifest import LATEST_KEY, MAX_ARCHIVE, PREFIX, REPOSITORY, SHA, decode, encode, manifest

REQUIRED = {'Backend tests', 'Frontend tests', 'Android tests', 'deployment-recovery'}


def github(path, token):
    request = Request('https://api.github.com/repos/' + REPOSITORY + path,
                      headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                               'X-GitHub-Api-Version': '2026-03-10'})
    with urlopen(request, timeout=15) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError('GitHub response too large')
    return json.loads(raw)


def ready(commit, token):
    if github('/git/ref/heads/main', token)['object']['sha'] != commit:
        return False
    runs = github(f'/actions/runs?head_sha={commit}&event=push&branch=main&per_page=100', token)
    if runs['total_count'] != len(runs['workflow_runs']):
        return False
    expected = {}
    for run in runs['workflow_runs']:
        if run['head_sha'] == commit and run['head_branch'] == 'main' and run['event'] == 'push':
            path = run['path']
            if path in ('.github/workflows/application-ci.yml', '.github/workflows/deployment-recovery.yml'):
                if run['id'] > expected.get(path, {}).get('id', 0):
                    expected[path] = run
    application = expected.get('.github/workflows/application-ci.yml')
    recovery = expected.get('.github/workflows/deployment-recovery.yml')
    if (not application or not recovery or str(application['id']) != os.environ.get('GITHUB_RUN_ID')
            or recovery['status'] != 'completed' or recovery['conclusion'] != 'success'):
        return False
    response = github(f'/commits/{commit}/check-runs?per_page=100', token)
    checks = response['check_runs']
    if response['total_count'] != len(checks):
        return False
    latest = {}
    for check in checks:
        suite = recovery['check_suite_id'] if check['name'] == 'deployment-recovery' else application['check_suite_id']
        if (check['head_sha'] == commit and check['name'] in REQUIRED
                and check.get('app', {}).get('slug') == 'github-actions'
                and check.get('check_suite', {}).get('id') == suite):
            if check['id'] > latest.get(check['name'], {}).get('id', 0):
                latest[check['name']] = check
    return set(latest) == REQUIRED and all(c['status'] == 'completed' and c['conclusion'] == 'success' for c in latest.values())


def package(commit, output):
    if subprocess.check_output(['git', 'rev-parse', 'HEAD']).decode().strip() != commit:
        raise ValueError('Checkout does not match publishing commit')
    if subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=normal']):
        raise ValueError('Publishing checkout must be clean')
    static = Path('demo/dist/client')
    if not (static / 'index.html').is_file():
        raise ValueError('Same-origin production build missing')
    subprocess.run(['git', 'archive', '--format=tar', '--output=' + str(output), commit], check=True)
    with tarfile.open(output, 'a') as archive:
        for path in sorted(static.rglob('*')):
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError('Unexpected static asset')
            archive.add(path, arcname=path.as_posix(), recursive=False)
    if output.stat().st_size > MAX_ARCHIVE:
        raise ValueError('Deployment artifact too large')


def publish(s3, bucket, commit, archive, token):
    key = PREFIX + 'commits/' + commit + '.json'
    listed = s3.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=2)
    if any(item['Key'] == key for item in listed.get('Contents', [])):
        response = s3.get_object(Bucket=bucket, Key=key)
        with closing(response['Body']) as body:
            candidate = decode(body.read(4097))
        if candidate['commit'] != commit:
            raise ValueError('Commit manifest identity conflict')
    else:
        candidate = None
    if candidate is None:
        with archive.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        candidate = manifest(commit, digest, archive.stat().st_size)
        with archive.open('rb') as stream:
            s3.put_object(Bucket=bucket, Key=candidate['key'], Body=stream,
                          ContentLength=candidate['size'], ContentType='application/x-tar',
                          Metadata={'sha256': digest}, ServerSideEncryption='AES256', IfNoneMatch='*')
        s3.put_object(Bucket=bucket, Key=key, Body=encode(candidate),
                      ContentType='application/json', ServerSideEncryption='AES256', IfNoneMatch='*')
    stored = s3.head_object(Bucket=bucket, Key=candidate['key'])
    if stored['ContentLength'] != candidate['size'] or stored.get('Metadata', {}).get('sha256') != candidate['sha256']:
        raise ValueError('Stored deployment artifact identity conflict')
    if not ready(commit, token):
        return {'published': False, 'reason': 'main_or_checks_changed', 'commit': commit}
    s3.put_object(Bucket=bucket, Key=LATEST_KEY, Body=encode(candidate),
                  ContentType='application/json', ServerSideEncryption='AES256')
    return {'published': True, 'commit': commit, 'sha256': candidate['sha256'], 'size': candidate['size']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--commit', required=True)
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--region', default='ap-northeast-1')
    parser.add_argument('--wait-seconds', type=int, default=0)
    args = parser.parse_args()
    if SHA.fullmatch(args.commit) is None or os.environ.get('GITHUB_REPOSITORY') != REPOSITORY:
        raise SystemExit('Trusted repository and exact commit required')
    token = os.environ['GITHUB_TOKEN']
    deadline = time.monotonic() + min(max(args.wait_seconds, 0), 600)
    while not ready(args.commit, token):
        if time.monotonic() >= deadline:
            print(json.dumps({'published': False, 'reason': 'main_or_checks_not_ready'}))
            return
        time.sleep(10)
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / 'release.tar'
        package(args.commit, archive)
        with closing(boto3.client('s3', region_name=args.region, config=Config(
                connect_timeout=5, read_timeout=30, retries={'total_max_attempts': 2, 'mode': 'standard'}))) as s3:
            print(json.dumps(publish(s3, args.bucket, args.commit, archive, token)))


if __name__ == '__main__':
    main()

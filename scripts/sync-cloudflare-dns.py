#!/usr/bin/env python3
"""Publish this EC2 instance's IPv4; withdraw only a verified owned A record.

No DNS or local-state mutation occurs without --execute. IMDSv2 is mandatory.
The private config fields are api_token, zone_id and record_name.
Cloudflare has no conditional DELETE by content: operators must not run two
publishers for this name concurrently. A forced shutdown may skip ExecStop.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


CF_BASE = 'https://api.cloudflare.com/client/v4'
IMDS_BASE = 'http://169.254.169.254'
DEFAULT_CONFIG = Path('/etc/memoverse/cloudflare.json')
DEFAULT_STATE = Path('/var/lib/memoverse-ddns/state.json')


class DDNSError(Exception):
    """Only fixed, non-sensitive error codes may be logged."""


def identifier(value, kind):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-fA-F]{32}', value) is None:
        raise DDNSError('invalid_' + kind)
    return value.lower()


def domain_name(value):
    if not isinstance(value, str):
        raise DDNSError('invalid_record_name')
    value = value.lower().removesuffix('.')
    labels = value.split('.')
    if len(value) > 253 or len(labels) < 2 or any(
        re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) is None
        for label in labels
    ):
        raise DDNSError('invalid_record_name')
    return value


def public_ipv4(value):
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError):
        raise DDNSError('invalid_public_ipv4') from None
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise DDNSError('invalid_public_ipv4')
    return str(address)


def private_json(path, *, missing_ok=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise DDNSError('private_file_missing') from None
    if not stat.S_ISREG(info.st_mode) or info.st_size > 16384:
        raise DDNSError('invalid_private_file')
    if os.name == 'posix' and (stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid()):
        raise DDNSError('private_file_requires_owner_and_mode_0600')
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, UnicodeError):
        raise DDNSError('invalid_private_json') from None
    if not isinstance(value, dict):
        raise DDNSError('invalid_private_json')
    return value


def load_config(path):
    raw = private_json(path)
    token = raw.get('api_token')
    if not isinstance(token, str) or re.fullmatch(r'[A-Za-z0-9._~+/=-]{20,512}', token) is None:
        raise DDNSError('invalid_api_token')
    return {'token': token, 'zone_id': identifier(raw.get('zone_id'), 'zone_id'),
            'record_name': domain_name(raw.get('record_name'))}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


class HTTP:
    """Fixed hosts, no proxy inheritance, bounded reads/retries and no redirects."""

    def __init__(self, *, opener=None, clock=time.monotonic, sleep=time.sleep, timeout=4):
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())
        self.clock, self.sleep, self.timeout = clock, sleep, timeout
        self.deadline = clock() + 60

    def request(self, method, url, *, headers=None, body=None, retries=3):
        parsed = urlsplit(url)
        valid_cf = parsed.scheme == 'https' and parsed.netloc == 'api.cloudflare.com' and parsed.path.startswith('/client/v4/')
        valid_imds = parsed.scheme == 'http' and parsed.netloc == '169.254.169.254' and parsed.path in {
            '/latest/api/token', '/latest/meta-data/instance-id', '/latest/meta-data/public-ipv4',
        } and not parsed.query
        if not (valid_cf or valid_imds) or parsed.fragment:
            raise DDNSError('unapproved_http_target')
        for attempt in range(retries):
            remaining = self.deadline - self.clock()
            if remaining <= 0:
                raise DDNSError('request_deadline_exceeded')
            request = Request(url, data=body, headers=headers or {}, method=method)
            try:
                with self.opener.open(request, timeout=min(self.timeout, remaining)) as response:
                    payload = response.read(512 * 1024 + 1)
                    if len(payload) > 512 * 1024:
                        raise DDNSError('http_response_too_large')
                    return payload
            except HTTPError as error:
                status = error.code
                error.close()
                if status not in {429, 500, 502, 503, 504} or attempt == retries - 1:
                    raise DDNSError('http_status_' + str(status)) from None
            except (URLError, OSError, TimeoutError):
                if attempt == retries - 1:
                    raise DDNSError('http_transport_failed') from None
            self.sleep(min(2 ** attempt, max(0, self.deadline - self.clock())))
        raise DDNSError('http_transport_failed')


def instance_identity(http):
    token = http.request('PUT', IMDS_BASE + '/latest/api/token',
                         headers={'X-aws-ec2-metadata-token-ttl-seconds': '60'}).decode('ascii').strip()
    if not token or len(token) > 4096 or any(character.isspace() for character in token):
        raise DDNSError('invalid_imds_token')
    headers = {'X-aws-ec2-metadata-token': token}
    instance_id = http.request('GET', IMDS_BASE + '/latest/meta-data/instance-id', headers=headers).decode('ascii').strip()
    address = http.request('GET', IMDS_BASE + '/latest/meta-data/public-ipv4', headers=headers).decode('ascii').strip()
    if re.fullmatch(r'i-(?:[0-9a-f]{8}|[0-9a-f]{17})', instance_id) is None:
        raise DDNSError('invalid_instance_id')
    return {'instance_id': instance_id, 'public_ipv4': public_ipv4(address)}


class Cloudflare:
    def __init__(self, http, config):
        self.http, self.config = http, config
        self.base = CF_BASE + '/zones/' + config['zone_id'] + '/dns_records'

    def call(self, method, suffix='', body=None):
        payload = self.http.request(method, self.base + suffix,
            headers={'Authorization': 'Bearer ' + self.config['token'], 'Content-Type': 'application/json'},
            body=None if body is None else json.dumps(body, separators=(',', ':')).encode('utf-8'),
            retries=3 if method == 'GET' else 1)
        try:
            response = json.loads(payload)
        except (ValueError, UnicodeError):
            raise DDNSError('invalid_cloudflare_json') from None
        if not isinstance(response, dict) or response.get('success') is not True:
            raise DDNSError('cloudflare_request_rejected')
        return response

    def record(self, value):
        if not isinstance(value, dict) or value.get('type') != 'A' or domain_name(value.get('name')) != self.config['record_name']:
            raise DDNSError('unexpected_dns_record')
        identifier(value.get('id'), 'record_id')
        try:
            ipaddress.IPv4Address(value['content'])
        except (KeyError, ValueError, TypeError):
            raise DDNSError('invalid_record_content') from None
        if not isinstance(value.get('proxied'), bool) or type(value.get('ttl')) is not int:
            raise DDNSError('invalid_record_settings')
        if not isinstance(value.get('modified_on'), str) or not value['modified_on']:
            raise DDNSError('missing_record_revision')
        return value

    def single(self):
        query = urlencode({'type': 'A', 'name.exact': self.config['record_name'],
                           'match': 'all', 'page': 1, 'per_page': 2})
        response = self.call('GET', '?' + query)
        records = response.get('result')
        info = response.get('result_info')
        if not isinstance(records, list) or not isinstance(info, dict):
            raise DDNSError('invalid_dns_list')
        count = info.get('total_count')
        if type(count) is not int or count != len(records):
            raise DDNSError('ambiguous_dns_records')
        if len(records) > 1:
            raise DDNSError('multiple_a_records_conflict')
        return self.record(records[0]) if records else None

    def get(self, record_id):
        record_id = identifier(record_id, 'record_id')
        record = self.record(self.call('GET', '/' + record_id)['result'])
        if record['id'] != record_id:
            raise DDNSError('unexpected_record_id')
        return record


def atomic_state(path, value):
    parent = path.parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if parent.is_symlink() or (os.name == 'posix' and
            (parent.stat().st_uid != os.geteuid() or stat.S_IMODE(parent.stat().st_mode) & 0o077)):
        raise DDNSError('state_directory_requires_private_owner')
    if path.exists() or path.is_symlink():
        private_json(path)
    descriptor, temporary = tempfile.mkstemp(prefix='.state-', dir=parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, sort_keys=True, separators=(',', ':'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def execution_lock(path, execute):
    if not execute:
        yield
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise DDNSError('invalid_state_directory')
    if os.name == 'posix':
        import fcntl
        if path.parent.stat().st_uid != os.geteuid() or stat.S_IMODE(path.parent.stat().st_mode) & 0o077:
            raise DDNSError('state_directory_requires_private_owner')
        descriptor = os.open(path.parent / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DDNSError('another_ddns_operation_is_running') from None
            yield
        finally:
            os.close(descriptor)
    else:
        # The deployed service is Linux. Windows is used only for offline tests.
        yield


def sync(action, config, state_path, http, *, execute=False):
    result = {'action': action, 'domain': config['record_name']}
    state = private_json(state_path, missing_ok=True)
    if action == 'withdraw' and (not state or state.get('status') != 'published'):
        return {**result, 'result': 'skipped_no_managed_record'}
    identity = instance_identity(http)
    result['ip'] = identity['public_ipv4']
    api = Cloudflare(http, config)
    with execution_lock(state_path, execute):
        if action == 'publish':
            record = api.single()
            operation = 'create' if record is None else ('unchanged' if record['content'] == identity['public_ipv4'] else 'update')
            if not execute:
                return {**result, 'result': 'plan_' + operation}
            if record is None:
                record = api.record(api.call('POST', body={'type': 'A', 'name': config['record_name'],
                    'content': identity['public_ipv4'], 'proxied': False, 'ttl': 60})['result'])
                expected_proxy, expected_ttl = False, 60
            else:
                # Only content is patched. Existing TTL, proxy, comments and tags remain untouched.
                expected_proxy, expected_ttl = record['proxied'], record['ttl']
                if operation == 'update':
                    api.call('PATCH', '/' + identifier(record['id'], 'record_id'), {'content': identity['public_ipv4']})
            confirmed = api.get(record['id'])
            if confirmed['content'] != identity['public_ipv4'] or confirmed['proxied'] != expected_proxy or confirmed['ttl'] != expected_ttl:
                raise DDNSError('dns_publish_verification_failed')
            single = api.single()
            if single is None or any(single[key] != confirmed[key]
                                     for key in ('id', 'content', 'modified_on', 'proxied', 'ttl')):
                raise DDNSError('dns_publish_changed_during_verification')
            atomic_state(state_path, {'version': 1, 'status': 'published', **identity,
                'zone_id': config['zone_id'], 'record_name': config['record_name'], 'record_id': confirmed['id'],
                'modified_on': confirmed['modified_on'], 'confirmed_at': datetime.now(timezone.utc).isoformat()})
            return {**result, 'result': 'published_' + operation}

        if action != 'withdraw':
            raise DDNSError('invalid_action')
        state = private_json(state_path, missing_ok=True)
        if not state or state.get('status') != 'published':
            return {**result, 'result': 'skipped_no_managed_record'}
        if state.get('version') != 1 or any(state.get(key) != value for key, value in {
            **identity, 'zone_id': config['zone_id'], 'record_name': config['record_name'],
        }.items()):
            return {**result, 'result': 'skipped_ownership_mismatch'}
        record_id = identifier(state.get('record_id'), 'record_id')
        record = api.single()
        if record is None:
            if execute:
                atomic_state(state_path, {**state, 'status': 'withdrawn'})
            return {**result, 'result': 'already_absent' if execute else 'plan_already_absent'}
        if record['id'] != record_id:
            return {**result, 'result': 'skipped_record_replaced'}
        # Read the exact ID immediately before deleting, not just the list snapshot.
        current = api.get(record_id)
        if current['content'] != identity['public_ipv4'] or current['modified_on'] != state.get('modified_on'):
            return {**result, 'result': 'skipped_record_changed'}
        if not execute:
            return {**result, 'result': 'plan_withdraw'}
        api.call('DELETE', '/' + record_id)
        remaining = api.single()
        if remaining is not None:
            raise DDNSError('dns_withdraw_verification_failed')
        atomic_state(state_path, {**state, 'status': 'withdrawn'})
        return {**result, 'result': 'withdrawn'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('publish', 'withdraw'))
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--state', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--execute', action='store_true', help='Apply DNS changes and persist ownership state')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        result = sync(args.action, config, args.state, HTTP(), execute=args.execute)
    except DDNSError as error:
        print(json.dumps({'action': args.action, 'result': 'error', 'code': str(error)}))
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        # Never print request headers, config values or raw API error bodies.
        print(json.dumps({'action': args.action, 'result': 'error', 'code': 'invalid_io_or_response'}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())

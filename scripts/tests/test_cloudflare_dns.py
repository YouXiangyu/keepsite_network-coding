"""Offline DDNS lifecycle tests. All HTTP/IMDS replies are fixtures."""
from __future__ import annotations

import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('sync_cloudflare_dns', ROOT / 'scripts/sync-cloudflare-dns.py')
ddns = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ddns)

TOKEN = 'unit_test_token_not_a_real_credential'
CONFIG = {'token': TOKEN, 'zone_id': 'a' * 32, 'record_name': 'keepsite.space'}
CONFIG_FILE = {'api_token': TOKEN, 'zone_id': CONFIG['zone_id'], 'record_name': CONFIG['record_name']}
INSTANCE = 'i-0123456789abcdef0'
CURRENT_IP = '52.196.70.26'
OTHER_IP = '54.168.1.2'


def record(address=OTHER_IP, **overrides):
    return {'id': 'b' * 32, 'type': 'A', 'name': 'keepsite.space', 'content': address,
            'ttl': 3600, 'proxied': False, 'modified_on': 'revision-0',
            'comment': 'operator comment', 'tags': ['project:keepsite'], **overrides}


class FakeHTTP:
    def __init__(self, records=()):
        self.records = copy.deepcopy(list(records))
        self.calls = []
        self.identity = INSTANCE
        self.address = CURRENT_IP
        self.revision = 0
        self.before_get = None
        self.fail_public_ip = False
        self.mutation_error = None

    @property
    def mutations(self):
        return [call for call in self.calls if call['url'].startswith(ddns.CF_BASE) and call['method'] != 'GET']

    def request(self, method, url, *, headers=None, body=None, retries=3):
        self.calls.append({'method': method, 'url': url, 'headers': headers or {},
                           'body': None if body is None else json.loads(body), 'retries': retries})
        if url.startswith(ddns.IMDS_BASE):
            if url.endswith('/api/token'):
                assert method == 'PUT' and headers['X-aws-ec2-metadata-token-ttl-seconds'] == '60'
                assert 'Authorization' not in headers
                return b'fixture-imds-token'
            assert headers == {'X-aws-ec2-metadata-token': 'fixture-imds-token'}
            if url.endswith('/instance-id'):
                return self.identity.encode()
            if self.fail_public_ip:
                raise ddns.DDNSError('http_status_404')
            return self.address.encode()
        assert url.startswith(ddns.CF_BASE + '/zones/' + CONFIG['zone_id'] + '/dns_records')
        assert headers['Authorization'] == 'Bearer ' + TOKEN
        parsed = urlsplit(url)
        if method == 'GET' and parsed.query:
            query = parse_qs(parsed.query)
            assert query['name.exact'] == ['keepsite.space'] and query['type'] == ['A']
            return json.dumps({'success': True, 'result': self.records[:2],
                               'result_info': {'total_count': len(self.records)}}).encode()
        if method == 'GET':
            if self.before_get:
                self.before_get(self)
            identifier = parsed.path.rsplit('/', 1)[-1]
            found = next((item for item in self.records if item['id'] == identifier), None)
            if found is None:
                raise ddns.DDNSError('http_status_404')
            return json.dumps({'success': True, 'result': found}).encode()
        assert retries == 1  # Never blindly replay a possibly successful create/delete.
        if self.mutation_error:
            raise self.mutation_error
        self.revision += 1
        value = json.loads(body) if body is not None else None
        if method == 'POST':
            item = {**value, 'id': 'c' * 32, 'modified_on': f'revision-{self.revision}'}
            self.records.append(item)
        elif method == 'PATCH':
            identifier = parsed.path.rsplit('/', 1)[-1]
            item = next(item for item in self.records if item['id'] == identifier)
            item.update(value, modified_on=f'revision-{self.revision}')
        elif method == 'DELETE':
            identifier = parsed.path.rsplit('/', 1)[-1]
            self.records = [item for item in self.records if item['id'] != identifier]
            item = {'id': identifier}
        else:
            raise AssertionError('Unexpected HTTP method')
        return json.dumps({'success': True, 'result': item}).encode()


class DDNSLifecycleTests(unittest.TestCase):
    def setUp(self):
        base = ROOT / '.cache/ddns-unit-tests'
        base.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.state_path = self.folder / 'state' / 'state.json'

    def run_sync(self, http, action='publish', execute=True):
        return ddns.sync(action, CONFIG, self.state_path, http, execute=execute)

    def test_default_plan_does_not_create_dns_or_state_directories(self):
        http = FakeHTTP()
        self.assertEqual(self.run_sync(http, execute=False)['result'], 'plan_create')
        self.assertEqual(http.mutations, [])
        self.assertFalse(self.state_path.parent.exists())

    def test_new_record_is_dns_only_ttl_60_and_state_contains_no_token(self):
        http = FakeHTTP()
        self.assertEqual(self.run_sync(http)['result'], 'published_create')
        self.assertEqual(http.mutations[0]['body'], {'type': 'A', 'name': 'keepsite.space',
                          'content': CURRENT_IP, 'proxied': False, 'ttl': 60})
        state = ddns.private_json(self.state_path)
        self.assertEqual(state['instance_id'], INSTANCE)
        self.assertEqual(state['record_id'], 'c' * 32)
        self.assertNotIn(TOKEN, self.state_path.read_text())
        self.assertNotIn(TOKEN, json.dumps(self.run_sync(http)))
        self.assertEqual(list(self.state_path.parent.glob('.state-*')), [])

    def test_existing_proxy_ttl_comment_and_tags_are_preserved(self):
        for proxied, ttl in ((False, 3600), (True, 1)):
            with self.subTest(proxied=proxied):
                http = FakeHTTP([record(proxied=proxied, ttl=ttl)])
                self.assertEqual(self.run_sync(http)['result'], 'published_update')
                self.assertEqual(http.mutations[0]['body'], {'content': CURRENT_IP})
                self.assertEqual(http.records[0]['proxied'], proxied)
                self.assertEqual(http.records[0]['ttl'], ttl)
                self.assertEqual(http.records[0]['comment'], 'operator comment')
                self.assertEqual(http.records[0]['tags'], ['project:keepsite'])

    def test_repeated_publish_is_noop_and_manual_withdraw_then_republish_recreates(self):
        http = FakeHTTP()
        self.run_sync(http)
        original = ddns.private_json(self.state_path)
        self.assertEqual(self.run_sync(http)['result'], 'published_unchanged')
        self.assertEqual(len(http.mutations), 1)
        self.assertEqual(ddns.private_json(self.state_path)['record_id'], original['record_id'])
        self.assertEqual(self.run_sync(http, 'withdraw')['result'], 'withdrawn')
        self.assertEqual(http.records, [])
        self.assertEqual(self.run_sync(http, 'withdraw')['result'], 'skipped_no_managed_record')
        http.address = OTHER_IP
        self.assertEqual(self.run_sync(http)['result'], 'published_create')
        self.assertEqual(http.records[0]['content'], OTHER_IP)

    def test_multiple_a_records_stop_without_mutation(self):
        for count in (2, 3):
            with self.subTest(count=count):
                http = FakeHTTP([record(id=f'{index:032x}') for index in range(count)])
                with self.assertRaises(ddns.DDNSError):
                    self.run_sync(http)
                self.assertFalse(http.mutations)

    def test_response_for_other_name_or_type_is_rejected(self):
        for overrides in ({'name': 'unrelated.example'}, {'type': 'AAAA'}):
            with self.subTest(overrides=overrides):
                http = FakeHTTP([record(**overrides)])
                with self.assertRaises(ddns.DDNSError):
                    self.run_sync(http)
                self.assertFalse(http.mutations)

    def test_bad_metadata_addresses_never_reach_cloudflare(self):
        for address in ('127.0.0.1', '10.0.0.1', '169.254.1.1', '0.0.0.0', '192.0.2.1',
                        '224.0.0.1', '::1', 'not-an-ip', '52.196.70.26\n10.0.0.1'):
            with self.subTest(address=address):
                http = FakeHTTP()
                http.address = address
                with self.assertRaises(ddns.DDNSError):
                    self.run_sync(http)
                self.assertTrue(all(call['url'].startswith(ddns.IMDS_BASE) for call in http.calls))

    def test_missing_imds_public_ip_fails_without_api_write_or_state(self):
        http = FakeHTTP()
        http.fail_public_ip = True
        with self.assertRaises(ddns.DDNSError):
            self.run_sync(http)
        self.assertFalse(http.mutations)
        self.assertFalse(self.state_path.exists())

    def test_bad_instance_id_is_rejected(self):
        http = FakeHTTP()
        http.identity = 'i-not-a-real-instance'
        with self.assertRaises(ddns.DDNSError):
            self.run_sync(http)
        self.assertFalse(http.mutations)

    def test_publish_readback_mismatch_does_not_record_ownership(self):
        http = FakeHTTP()
        http.before_get = lambda api: api.records[0].update(content=OTHER_IP)
        with self.assertRaisesRegex(ddns.DDNSError, 'verification_failed'):
            self.run_sync(http)
        self.assertFalse(self.state_path.exists())

    def test_competing_record_appearing_after_create_is_not_claimed(self):
        http = FakeHTTP()
        http.before_get = lambda api: api.records.append(record(id='d' * 32))
        with self.assertRaisesRegex(ddns.DDNSError, 'multiple_a_records_conflict'):
            self.run_sync(http)
        self.assertFalse(self.state_path.exists())
        self.assertTrue(all(call['method'] != 'DELETE' for call in http.mutations))

    def test_ambiguous_mutation_failure_is_not_replayed_or_claimed(self):
        http = FakeHTTP()
        http.mutation_error = ddns.DDNSError('http_transport_failed')
        with self.assertRaises(ddns.DDNSError):
            self.run_sync(http)
        self.assertEqual(len(http.mutations), 1)
        self.assertFalse(self.state_path.exists())

    def test_plan_withdraw_does_not_delete_or_change_state(self):
        http = FakeHTTP()
        self.run_sync(http)
        before = self.state_path.read_bytes()
        self.assertEqual(self.run_sync(http, 'withdraw', execute=False)['result'], 'plan_withdraw')
        self.assertEqual(len(http.mutations), 1)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_withdraw_without_state_does_not_even_contact_imds(self):
        http = FakeHTTP([record(CURRENT_IP)])
        self.assertEqual(self.run_sync(http, 'withdraw')['result'], 'skipped_no_managed_record')
        self.assertEqual(http.calls, [])

    def test_new_instance_publication_is_not_deleted_by_old_instance(self):
        http = FakeHTTP()
        self.run_sync(http)
        http.records[0].update(content=OTHER_IP, modified_on='new-instance-revision')
        self.assertEqual(self.run_sync(http, 'withdraw')['result'], 'skipped_record_changed')
        self.assertTrue(all(call['method'] != 'DELETE' for call in http.mutations))

    def test_change_between_list_and_final_get_prevents_delete(self):
        http = FakeHTTP()
        self.run_sync(http)
        http.before_get = lambda api: api.records[0].update(content=OTHER_IP, modified_on='later')
        self.assertEqual(self.run_sync(http, 'withdraw')['result'], 'skipped_record_changed')
        self.assertTrue(all(call['method'] != 'DELETE' for call in http.mutations))

    def test_same_ip_with_external_record_change_is_not_deleted(self):
        http = FakeHTTP()
        self.run_sync(http)
        http.records[0]['modified_on'] = 'operator-reconfigured-record'
        self.assertEqual(self.run_sync(http, 'withdraw')['result'], 'skipped_record_changed')
        self.assertEqual(len(http.mutations), 1)

    def test_copied_state_or_changed_imds_ip_does_not_authorize_withdraw(self):
        for key, value in (('identity', 'i-fedcba98765432100'), ('address', OTHER_IP)):
            with self.subTest(key=key):
                http = FakeHTTP()
                self.run_sync(http)
                setattr(http, key, value)
                self.assertEqual(self.run_sync(http, 'withdraw')['result'], 'skipped_ownership_mismatch')
                self.assertEqual(len(http.mutations), 1)

    def test_replaced_record_id_is_not_deleted(self):
        http = FakeHTTP()
        self.run_sync(http)
        http.records[0]['id'] = 'd' * 32
        self.assertEqual(self.run_sync(http, 'withdraw')['result'], 'skipped_record_replaced')
        self.assertEqual(len(http.mutations), 1)

    def test_config_validation_and_safe_cli_error_logs(self):
        config_path = self.folder / 'config.json'
        for changes in ({'zone_id': '../bad'}, {'zone_id': 'a' * 31}, {'record_name': 'bad/path'},
                        {'record_name': '*.keepsite.space'}, {'api_token': TOKEN + '\r\nInjected: value'}):
            with self.subTest(changes=changes):
                config_path.write_text(json.dumps({**CONFIG_FILE, **changes}))
                config_path.chmod(0o600)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(ddns.main(['publish', '--config', str(config_path), '--state', str(self.state_path)]), 1)
                self.assertNotIn(TOKEN, output.getvalue())
                self.assertFalse(self.state_path.exists())

    def test_api_rejection_does_not_log_response_body(self):
        config_path = self.folder / 'config.json'
        config_path.write_text(json.dumps(CONFIG_FILE))
        config_path.chmod(0o600)
        http = FakeHTTP()
        original = http.request

        def rejected(method, url, **kwargs):
            if url.startswith(ddns.CF_BASE):
                return json.dumps({'success': False, 'errors': [{'message': TOKEN}]}).encode()
            return original(method, url, **kwargs)

        http.request = rejected
        output = io.StringIO()
        with patch.object(ddns, 'HTTP', return_value=http), contextlib.redirect_stdout(output):
            self.assertEqual(ddns.main(['publish', '--config', str(config_path), '--state', str(self.state_path)]), 1)
        self.assertNotIn(TOKEN, output.getvalue())
        self.assertIn('cloudflare_request_rejected', output.getvalue())

    def test_config_requires_api_token_and_does_not_accept_legacy_field(self):
        config_path = self.folder / 'config.json'
        config_path.write_text(json.dumps(CONFIG_FILE))
        config_path.chmod(0o600)
        self.assertEqual(ddns.load_config(config_path), CONFIG)
        config_path.write_text(json.dumps(CONFIG))
        with self.assertRaisesRegex(ddns.DDNSError, 'invalid_api_token'):
            ddns.load_config(config_path)

    def test_atomic_state_failure_leaves_previous_state_unchanged(self):
        ddns.atomic_state(self.state_path, {'status': 'previous'})
        original = self.state_path.read_bytes()
        with patch.object(ddns.os, 'replace', side_effect=OSError('fixture failure')):
            with self.assertRaises(OSError):
                ddns.atomic_state(self.state_path, {'status': 'next'})
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(list(self.state_path.parent.glob('.state-*')), [])


class Response:
    def __init__(self, data):
        self.data = data
        self.closed = False

    def read(self, limit):
        return self.data[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class Opener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class HTTPTests(unittest.TestCase):
    def test_transient_read_retry_is_bounded_and_responses_close(self):
        response = Response(b'ok')
        opener = Opener([URLError(TOKEN), HTTPError('fixture', 503, TOKEN, {}, io.BytesIO(TOKEN.encode())), response])
        sleeps = []
        http = ddns.HTTP(opener=opener, sleep=sleeps.append)
        self.assertEqual(http.request('GET', ddns.CF_BASE + '/zones'), b'ok')
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(sleeps, [1, 2])
        self.assertTrue(response.closed)

    def test_redirect_and_auth_errors_are_not_retried_or_exposed(self):
        for status in (301, 302, 401, 403):
            with self.subTest(status=status):
                opener = Opener([HTTPError('fixture', status, TOKEN, {}, io.BytesIO(TOKEN.encode()))])
                with self.assertRaises(ddns.DDNSError) as error:
                    ddns.HTTP(opener=opener).request('GET', ddns.CF_BASE + '/zones')
                self.assertNotIn(TOKEN, str(error.exception))
                self.assertEqual(len(opener.calls), 1)
        self.assertIsNone(ddns.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://untrusted.example'))

    def test_transport_failure_stops_after_three_attempts(self):
        opener = Opener([URLError(TOKEN)] * 3)
        with self.assertRaises(ddns.DDNSError) as error:
            ddns.HTTP(opener=opener, sleep=lambda _seconds: None).request('GET', ddns.CF_BASE + '/zones')
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(str(error.exception), 'http_transport_failed')

    def test_unapproved_hosts_http_cloudflare_and_metadata_paths_are_rejected(self):
        for target in ('https://untrusted.example/', 'http://api.cloudflare.com/client/v4/zones',
                       'https://api.cloudflare.com.evil.example/client/v4/zones',
                       'http://169.254.169.254/latest/meta-data/iam/security-credentials/'):
            with self.subTest(target=target):
                opener = Opener([])
                with self.assertRaises(ddns.DDNSError):
                    ddns.HTTP(opener=opener).request('GET', target)
                self.assertEqual(opener.calls, [])

    def test_service_publishes_and_retains_fixed_ip_record_with_root_private_state(self):
        unit = (ROOT / 'deploy/ubuntu/keepsite-ddns.service').read_text()
        for expected in ('Wants=network-online.target', 'After=network-online.target', 'Type=oneshot',
                         'RemainAfterExit=yes', 'User=root', 'StateDirectory=keepsite-ddns',
                         'StateDirectoryMode=0700', 'publish --execute',
                         'TimeoutStartSec=90', 'TimeoutStopSec=90'):
            self.assertIn(expected, unit)
        self.assertNotIn(TOKEN, unit)
        self.assertNotIn('ExecStop=', unit)
        self.assertNotIn('withdraw --execute', unit)


if __name__ == '__main__':
    unittest.main()

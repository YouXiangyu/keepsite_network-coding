"""BFF tests use real RSA signatures and injectable provider/clock; no AWS calls."""
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import io
import json
import logging
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.error import HTTPError

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import jwt
import pytest

from backend.auth import AuthConfig, AuthError, AuthService, CognitoProvider, NoRedirect, RateLimiter, install_auth

PUBLIC = 'https://memoverse.test'
HEADERS = {'Origin': PUBLIC, 'X-MemoVerse-Client': 'web'}
PASSWORD = 'StrongPass!123'


@pytest.fixture(scope='module')
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeProvider:
    def __init__(self, config, clock, key):
        self.config, self.clock, self.key = config, clock, key
        self.id_changes, self.access_changes = {}, {}
        self.subject, self.email = 'subject-a', 'person@example.com'
        self.refresh_calls, self.exchange_calls, self.jwks_calls = 0, 0, 0
        self.revoke_calls, self.actions = [], []
        self.refresh_error = self.action_error = None
        self.nonce = None
        self.last_tokens = None

    def tokens(self):
        claims = {'iss': self.config.issuer, 'sub': self.subject, 'iat': self.clock(), 'exp': self.clock() + 900}
        identity = {**claims, 'aud': self.config.client_id, 'token_use': 'id', 'email': self.email, 'email_verified': True, 'name': 'Test User'}
        if self.nonce is not None:
            identity['nonce'] = self.nonce
        identity.update(self.id_changes)
        access = {**claims, 'client_id': self.config.client_id, 'token_use': 'access', **self.access_changes}
        self.last_tokens = {'id_token': jwt.encode(identity, self.key, algorithm='RS256', headers={'kid': 'test-key'}),
                            'access_token': jwt.encode(access, self.key, algorithm='RS256', headers={'kid': 'test-key'}),
                            'refresh_token': f'provider-private-refresh-{self.refresh_calls}'}
        return self.last_tokens.copy()

    def jwks(self):
        self.jwks_calls += 1
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {'keys': [{**key, 'kid': 'test-key', 'alg': 'RS256', 'use': 'sig'}]}

    def login(self, email, password):
        self.actions.append(('login', email, password))
        if self.action_error:
            raise self.action_error
        return self.tokens()

    def refresh(self, refresh_token):
        self.refresh_calls += 1
        if self.refresh_error:
            raise self.refresh_error
        return self.tokens()

    def revoke(self, refresh_token):
        self.revoke_calls.append(refresh_token)
        if self.action_error:
            raise self.action_error

    def exchange(self, code, verifier):
        self.exchange_calls += 1
        self.exchanged = (code, verifier)
        return self.tokens()

    def action(self, name, *arguments):
        self.actions.append((name, *arguments))
        if self.action_error:
            raise self.action_error

    def register(self, *args): self.action('register', *args)
    def verify(self, *args): self.action('verify', *args)
    def resend(self, *args): self.action('resend', *args)
    def forgot(self, *args): self.action('forgot', *args)
    def reset(self, *args): self.action('reset', *args)


@pytest.fixture
def auth(tmp_path, signing_key):
    now = [1800000000.0]
    clock = lambda: now[0]
    config = AuthConfig('ap-northeast-1', 'ap-northeast-1_testpool', 'testclient123',
                        'https://fixture.auth.ap-northeast-1.amazoncognito.com', PUBLIC,
                        Fernet.generate_key().decode(), google_enabled=True)
    provider = FakeProvider(config, clock, signing_key)
    app = FastAPI()
    initialized = []
    callback_threads = []
    def on_authenticated(user):
        callback_threads.append(threading.current_thread().name)
        # A callback may use another database; auth must hold no write transaction.
        with service.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.rollback()
        initialized.append(user.copy())
    service = install_auth(app, tmp_path, on_authenticated=on_authenticated, config=config, provider=provider, clock=clock)

    @app.get('/api/health')
    def health(): return {'ok': True}

    @app.api_route('/api/protected', methods=['GET', 'POST'])
    def protected(request: Request): return {'user': request.state.user}

    with TestClient(app, base_url=PUBLIC) as client:
        yield SimpleNamespace(app=app, client=client, service=service, config=config, provider=provider,
                              now=now, initialized=initialized, directory=tmp_path, callback_threads=callback_threads)


def login(auth, **body):
    return auth.client.post('/api/auth/login', headers=HEADERS, json={'email': 'person@example.com', 'password': PASSWORD, **body})


def test_unconfigured_is_closed_and_does_not_create_provider(tmp_path, monkeypatch):
    for key in ('REGION', 'USER_POOL_ID', 'CLIENT_ID', 'DOMAIN'):
        monkeypatch.delenv('MEMOVERSE_COGNITO_' + key, raising=False)
    monkeypatch.delenv('MEMOVERSE_SESSION_KEY', raising=False)
    monkeypatch.setattr('backend.auth.boto3.client', lambda *args, **kwargs: pytest.fail('Unconfigured auth must not connect'))
    app = FastAPI()
    service = install_auth(app, tmp_path)
    @app.get('/api/protected')
    def protected(): return {'secret': 'never returned'}
    with TestClient(app) as client:
        result = client.get('/api/auth/session').json()
        assert result == {'authenticated': False, 'configured': False, 'providers': {'email': False, 'google': False}}
        assert client.get('/api/protected').status_code == 401
        assert client.post('/api/auth/login', json={}).status_code == 503
        assert service._provider is None


def test_anonymous_business_is_rejected_health_is_public(auth):
    assert auth.client.get('/api/health').status_code == 200
    assert auth.client.get('/api/protected').status_code == 401
    assert not auth.initialized


def test_login_cookie_tokens_encryption_csrf_and_callback(auth):
    result = login(auth, email=' Person@Example.COM ', password=' StrongPass!123 ')
    assert result.status_code == 200
    body = result.json()
    assert body['authenticated'] and body['user']['email'] == 'person@example.com'
    assert auth.provider.actions[-1][-1] == ' StrongPass!123 '
    cookie = result.headers['set-cookie']
    assert '__Host-memoverse_session=' in cookie and 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie and 'Domain=' not in cookie
    sid = auth.client.cookies.get(auth.service.cookie_name)
    assert len(sid) == 43 and '.' not in sid
    with auth.service.db() as db:
        row = db.execute('SELECT * FROM sessions').fetchone()
        assert row['sid_hash'] == hashlib.sha256(sid.encode()).hexdigest()
        assert sid.encode() not in row['tokens']
        for token in auth.provider.last_tokens.values():
            assert token not in result.text and token.encode() not in row['tokens']
    assert auth.client.get('/api/protected').json()['user'] == body['user']
    assert auth.client.post('/api/protected', headers=HEADERS).status_code == 403
    assert auth.client.post('/api/protected', headers={**HEADERS, 'X-CSRF-Token': 'wrong'}).status_code == 403
    assert auth.client.post('/api/protected', headers={**HEADERS, 'X-CSRF-Token': body['csrfToken']}).status_code == 200
    assert auth.initialized and auth.provider.jwks_calls == 1
    assert all(name == 'AnyIO worker thread' for name in auth.callback_threads)


@pytest.mark.parametrize('changes,target,status', [
    ({'iss': 'https://attacker.test/pool'}, 'id', 401),
    ({'aud': 'another-client'}, 'id', 401),
    ({'client_id': 'another-client'}, 'access', 401),
    ({'token_use': 'access'}, 'id', 401),
    ({'token_use': 'id'}, 'access', 401),
    ({'exp': 1799999999}, 'id', 401),
    ({'iat': 1800001000}, 'access', 401),
    ({'sub': 'another-person'}, 'access', 401),
    ({'email_verified': False}, 'id', 403),
    ({'email_verified': 'true'}, 'id', 403),
    ({'email': 'invalid'}, 'id', 401),
])
def test_signed_but_invalid_claims_never_create_session(auth, changes, target, status):
    (auth.provider.id_changes if target == 'id' else auth.provider.access_changes).update(changes)
    result = login(auth)
    assert result.status_code == status
    if status == 403:
        assert result.json()['code'] == 'email_unverified'
    with auth.service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0


def test_bad_signature_is_rejected(auth):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    original = auth.provider.jwks()
    auth.provider.jwks = lambda: original
    auth.provider.key = other
    assert login(auth).status_code == 401


def test_unknown_signing_key_and_disallowed_algorithm_are_rejected(auth):
    tokens = auth.provider.tokens()
    claims = jwt.decode(tokens['id_token'], options={'verify_signature': False})
    tokens['id_token'] = jwt.encode(claims, auth.provider.key, algorithm='RS256', headers={'kid': 'unknown'})
    with pytest.raises(AuthError) as error:
        auth.service.create_session(tokens)
    assert error.value.status == 401
    tokens['id_token'] = jwt.encode(claims, 'a' * 32, algorithm='HS256', headers={'kid': 'test-key'})
    with pytest.raises(AuthError):
        auth.service.create_session(tokens)


def test_jwks_outage_does_not_become_invalid_credentials(auth):
    auth.provider.jwks = lambda: {'keys': [{'kty': 'RSA', 'kid': 'test-key', 'n': 'invalid'}]}
    assert login(auth).status_code == 503


def test_refresh_rotates_once_under_concurrency(auth):
    assert login(auth).status_code == 200
    sid = auth.client.cookies.get(auth.service.cookie_name)
    auth.now[0] += 871
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: auth.service.session(sid), range(4)))
    assert all(result['user']['email'] == 'person@example.com' for result in results)
    assert auth.provider.refresh_calls == 1
    with auth.service.db() as db:
        row = db.execute('SELECT * FROM sessions').fetchone()
        assert row['version'] == 2
        assert auth.service.decrypt(row['tokens'])['refresh_token'] == 'provider-private-refresh-1'


def test_transient_refresh_does_not_clear_session_but_revocation_does(auth):
    login(auth)
    sid = auth.client.cookies.get(auth.service.cookie_name)
    auth.now[0] += 871
    auth.provider.refresh_error = AuthError(503, 'Temporary provider error')
    assert auth.client.get('/api/protected').status_code == 503
    with auth.service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1
    assert auth.client.cookies.get(auth.service.cookie_name) == sid
    auth.provider.refresh_error = AuthError(401, 'Refresh token revoked')
    assert auth.client.get('/api/protected').status_code == 401
    with auth.service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0


def test_refresh_cannot_switch_identity_and_session_has_absolute_expiry(auth):
    login(auth)
    auth.provider.subject = 'new-subject'
    auth.now[0] += 871
    assert auth.client.get('/api/protected').status_code == 401
    auth.provider.subject = 'subject-a'
    login(auth)
    auth.now[0] += 7 * 24 * 3600 + 1
    assert auth.client.get('/api/auth/session').json()['authenticated'] is False


def test_logout_invalidates_before_best_effort_revoke(auth):
    session = login(auth).json()
    assert auth.client.post('/api/auth/logout', headers=HEADERS).status_code == 403
    auth.provider.action_error = AuthError(503, 'Provider offline')
    response = auth.client.post('/api/auth/logout', headers={**HEADERS, 'X-CSRF-Token': session['csrfToken']})
    assert response.status_code == 200 and response.json()['ok']
    assert response.json()['logoutUrl'].startswith(auth.config.domain + '/logout?')
    assert auth.provider.revoke_calls
    assert auth.client.get('/api/protected').status_code == 401
    with auth.service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0


def test_delayed_logout_preserves_new_login_and_oauth_cookies(auth, monkeypatch):
    first = login(auth).json()
    old_sid = auth.client.cookies.get(auth.service.cookie_name)
    revoking = threading.Event()
    release = threading.Event()

    def delayed_revoke(token):
        auth.provider.revoke_calls.append(token)
        revoking.set()
        assert release.wait(10), 'Test did not release provider revocation'

    monkeypatch.setattr(auth.provider, 'revoke', delayed_revoke)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(auth.client.post, '/api/auth/logout',
                              headers={**HEADERS, 'X-CSRF-Token': first['csrfToken']})
        try:
            assert revoking.wait(5), 'Logout never reached provider revocation'
            # The exact old sid is already invalid before the network call ends.
            assert auth.client.get('/api/protected').status_code == 401
            auth.provider.subject = 'subject-b'
            auth.provider.email = 'second@example.com'
            second = login(auth).json()
            new_sid = auth.client.cookies.get(auth.service.cookie_name)
            assert new_sid and new_sid != old_sid
            assert second['user']['id'] != first['user']['id']
            start = auth.client.get('/api/auth/google', follow_redirects=False)
            assert start.status_code == 302
            binding = auth.client.cookies.get(auth.service.oauth_cookie)
            assert binding
        finally:
            release.set()
        response = pending.result(timeout=5)

    assert response.status_code == 200 and response.json()['ok']
    assert 'set-cookie' not in response.headers
    assert auth.client.cookies.get(auth.service.cookie_name) == new_sid
    assert auth.client.cookies.get(auth.service.oauth_cookie) == binding
    assert auth.client.get('/api/protected').json()['user'] == second['user']
    assert auth.client.get('/api/protected', headers={
        'Cookie': auth.service.cookie_name + '=' + old_sid}).status_code == 401
    assert len(auth.provider.revoke_calls) == 1


def test_google_uses_pkce_nonce_bound_once_and_fixed_redirect(auth):
    start = auth.client.get('/api/auth/google?returnUrl=https://evil.test', follow_redirects=False)
    assert start.status_code == 302
    query = parse_qs(urlsplit(start.headers['location']).query)
    assert query['identity_provider'] == ['Google'] and query['code_challenge_method'] == ['S256']
    assert query['redirect_uri'] == [PUBLIC + '/api/auth/callback']
    binding = auth.client.cookies.get(auth.service.oauth_cookie)
    auth.provider.nonce = query['nonce'][0]
    response = auth.client.get('/api/auth/callback?' + urlencode({'state': query['state'][0], 'code': 'single-use-code', 'returnUrl': 'https://evil.test'}), follow_redirects=False)
    assert response.status_code == 303 and response.headers['location'] == PUBLIC + '/'
    verifier = auth.provider.exchanged[1]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    assert query['code_challenge'] == [challenge]
    assert verifier not in start.headers['location']
    auth.client.cookies.set(auth.service.oauth_cookie, binding)
    replay = auth.client.get('/api/auth/callback?' + urlencode({'state': query['state'][0], 'code': 'single-use-code'}), follow_redirects=False)
    assert replay.status_code == 303 and auth.provider.exchange_calls == 1
    assert replay.headers['location'] == PUBLIC + '/?auth_error=google_login_failed'


def test_google_wrong_browser_binding_and_nonce_fail(auth):
    start = auth.client.get('/api/auth/google', follow_redirects=False)
    query = parse_qs(urlsplit(start.headers['location']).query)
    with pytest.raises(AuthError) as error:
        auth.service.callback(query['state'][0], 'x' * 43, 'code')
    assert error.value.code == 'invalid_state'
    auth.provider.nonce = 'wrong-nonce'
    result = auth.client.get('/api/auth/callback?' + urlencode({'state': query['state'][0], 'code': 'code'}), follow_redirects=False)
    assert result.status_code == 303 and result.headers['location'] == PUBLIC + '/?auth_error=google_login_failed'
    assert auth.client.get('/api/auth/session').json()['authenticated'] is False
    assert not auth.client.cookies.get(auth.service.oauth_cookie)


def test_expired_google_state_never_exchanges_code(auth):
    start = auth.client.get('/api/auth/google', follow_redirects=False)
    query = parse_qs(urlsplit(start.headers['location']).query)
    auth.now[0] += 601
    result = auth.client.get('/api/auth/callback?' + urlencode({'state': query['state'][0], 'code': 'code'}), follow_redirects=False)
    assert result.status_code == 303 and auth.provider.exchange_calls == 0
    assert result.headers['location'] == PUBLIC + '/?auth_error=google_login_failed'
    assert auth.client.get('/api/auth/session').json()['authenticated'] is False


def test_google_cancellation_uses_fixed_safe_redirect_without_session(auth):
    result = auth.client.get('/api/auth/callback?error=access_denied&error_description=private-provider-detail&returnUrl=https://evil.test', follow_redirects=False)
    assert result.status_code == 303
    assert result.headers['location'] == PUBLIC + '/?auth_error=google_login_failed'
    assert 'private-provider-detail' not in result.text
    assert auth.client.get('/api/auth/session').json()['authenticated'] is False
    assert auth.provider.exchange_calls == 0


def test_same_email_different_provider_subjects_are_not_merged(auth):
    first = login(auth).json()['user']['id']
    auth.provider.subject = 'Google_provider_subject'
    second = login(auth).json()['user']['id']
    assert first != second


def test_registration_verification_and_reset_do_not_create_sessions(auth):
    assert auth.client.post('/api/auth/register', headers=HEADERS, json={'email': 'New@Example.com', 'password': PASSWORD}).json() == {'verificationRequired': True, 'email': 'new@example.com'}
    assert auth.client.post('/api/auth/verify-email', headers=HEADERS, json={'email': 'new@example.com', 'code': '012345'}).json() == {'verified': True}
    assert auth.provider.actions[-1][-1] == '012345'
    assert auth.client.post('/api/auth/reset-password', headers=HEADERS, json={'email': 'new@example.com', 'code': '012345', 'password': PASSWORD}).status_code == 200
    assert auth.client.get('/api/protected').status_code == 401


def test_forgot_password_response_does_not_reveal_user_existence(auth):
    good = auth.client.post('/api/auth/forgot-password', headers=HEADERS, json={'email': 'person@example.com'})
    auth.provider.action_error = AuthError(401, 'No such user')
    missing = auth.client.post('/api/auth/forgot-password', headers=HEADERS, json={'email': 'unknown@example.com'})
    assert good.status_code == missing.status_code == 200 and good.json() == missing.json() == {'ok': True}


@pytest.mark.parametrize('email', ['not-an-email', 'a..b@example.com', '.a@example.com', 'a@-example.com', 'a@example..com'])
def test_invalid_email_does_not_call_provider(auth, email):
    assert login(auth, email=email).status_code == 422
    assert not auth.provider.actions


def test_write_origin_header_password_code_and_size_validation_are_safe(auth):
    secret = 'DoNotEchoSecret!'
    assert auth.client.post('/api/auth/login', json={'email': 'person@example.com', 'password': PASSWORD}).status_code == 403
    result = auth.client.post('/api/auth/login', headers={**HEADERS, 'Origin': 'https://attacker.test'}, json={'email': 'person@example.com', 'password': PASSWORD})
    assert result.status_code == 403
    result = login(auth, password=secret)
    assert result.status_code == 422 and secret not in result.text
    result = auth.client.post('/api/auth/verify-email', headers=HEADERS, json={'email': 'person@example.com', 'code': 123456})
    assert result.status_code == 422 and '123456' not in result.text
    result = auth.client.post('/api/auth/login', headers=HEADERS, content='x' * 5000)
    assert result.status_code == 422


def test_rate_limit_bounded_and_expiring(auth):
    limiter = RateLimiter(lambda: auth.now[0], maximum=2)
    for _ in range(5): limiter.check('one', 'login', 'person@example.com')
    with pytest.raises(AuthError) as error: limiter.check('one', 'login', 'person@example.com')
    assert error.value.status == 429
    with pytest.raises(AuthError): limiter.check('two', 'login', 'other@example.com')
    assert len(limiter.entries) == 2
    auth.now[0] += 601
    limiter.check('two', 'login', 'other@example.com')


def test_http_dev_is_only_allowed_for_explicit_localhost(auth):
    values = dict(auth.config.__dict__)
    with pytest.raises(ValueError): AuthConfig(**{**values, 'public_url': 'http://public.example.com'})
    local = AuthConfig(**{**values, 'public_url': 'http://127.0.0.1:4173'})
    assert not local.secure


def test_google_disabled_is_explicit(auth, tmp_path):
    config = AuthConfig(**{**auth.config.__dict__, 'google_enabled': False})
    service = AuthService(tmp_path / 'disabled', config=config, provider=auth.provider, clock=lambda: auth.now[0])
    assert service.public_state()['providers'] == {'email': True, 'google': False}
    with pytest.raises(AuthError) as error:
        service.start_google()
    assert error.value.status == 503 and auth.provider.exchange_calls == 0


def test_unsigned_sdk_no_write_retry_and_no_redirect(auth, monkeypatch):
    captured = {}
    def create(*args, **kwargs): captured.update(kwargs); return object()
    monkeypatch.setattr('backend.auth.boto3.client', create)
    CognitoProvider(auth.config)
    from botocore import UNSIGNED
    assert captured['config'].signature_version is UNSIGNED
    assert captured['config'].retries['total_max_attempts'] == 1
    assert 'aws_access_key_id' not in captured
    assert NoRedirect().redirect_request(None, None, 302, None, None, 'https://evil.test') is None


def test_access_logs_drop_oauth_code_query(auth):
    record = logging.LogRecord('uvicorn.access', logging.INFO, '', 1, '%s %s %s %s %s',
                               ('ip', 'GET', '/api/auth/callback?code=secret-code&state=secret-state', '1.1', 303), None)
    assert logging.getLogger('uvicorn.access').filter(record)
    assert 'secret-code' not in record.getMessage() and 'secret-state' not in record.getMessage()


@pytest.mark.parametrize('status,expected', [(400, 401), (403, 401), (429, 429), (503, 503), (302, 503)])
def test_provider_http_errors_close_response_without_leaking_body(auth, monkeypatch, status, expected):
    monkeypatch.setattr('backend.auth.boto3.client', lambda *args, **kwargs: object())
    provider = CognitoProvider(auth.config)
    body = io.BytesIO(b'private-code-and-token')
    error = HTTPError(auth.config.domain + '/oauth2/token', status, 'private error', {}, body)
    def fail(*args, **kwargs):
        raise error
    provider.http = SimpleNamespace(open=fail)
    with pytest.raises(AuthError) as raised:
        provider.http_json(auth.config.domain + '/oauth2/token', b'code=private')
    assert raised.value.status == expected
    assert body.closed
    assert 'private' not in raised.value.detail

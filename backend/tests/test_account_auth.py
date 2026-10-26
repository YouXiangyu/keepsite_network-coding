"""Real signed subject proofs for explicit account linking; no cloud operations."""
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import secrets
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

from backend.auth import AuthError, hashed
from test_auth import auth, signing_key, login, HEADERS, PASSWORD, PUBLIC
from test_auth_integration import integrated
from account_fixtures import account_snapshot, image_bytes


GOOGLE = [{'providerName': 'Google', 'providerType': 'Google', 'userId': 'google-proof'}]


def signed_in(auth, *, google=False):
    auth.provider.id_changes = {'identities': GOOGLE} if google else {}
    session = login(auth).json()
    assert session['authenticated']
    headers = {**HEADERS, 'X-CSRF-Token': session['csrfToken']}
    return session, headers


def google_proof(auth, params, *, subject='subject-google', email='person@example.com'):
    auth.provider.subject, auth.provider.email = subject, email
    auth.provider.id_changes = {'identities': GOOGLE}
    auth.provider.nonce = params['nonce'][0]


def start_web(auth, headers):
    result = auth.client.post('/api/auth/account/google/start', headers=headers, json={})
    assert result.status_code == 200, result.text
    launch = auth.client.get(result.json()['browserUrl'], follow_redirects=False)
    assert launch.status_code == 302, launch.text
    params = parse_qs(urlsplit(launch.headers['location']).query)
    assert params['prompt'] == ['select_account']
    return params


def web_complete(auth, params):
    return auth.client.get('/api/auth/callback', params={'state': params['state'][0], 'code': 'proof-code'}, follow_redirects=False)


def test_password_first_bind_google_keeps_account_and_session(auth):
    session, headers = signed_in(auth)
    sid = auth.client.cookies.get(auth.service.cookie_name)
    assert auth.client.get('/api/auth/account/methods').json() == {
        'password': {'linked': True, 'email': 'person@example.com'}, 'google': {'linked': False, 'email': None}}
    params = start_web(auth, headers)
    google_proof(auth, params)
    result = web_complete(auth, params)
    assert result.headers['location'] == PUBLIC + '/devices?account_link=success'
    assert auth.client.cookies.get(auth.service.cookie_name) == sid
    assert auth.client.get('/api/auth/session').json()['user'] == session['user']
    assert auth.client.get('/api/auth/account/methods').json()['google']['linked'] is True
    # The separate Google subject now opens exactly the same immutable account.
    auth.provider.nonce = None
    alternate = auth.service.create_session(auth.provider.tokens())
    assert alternate['user'] == session['user']
    auth.now[0] += 880
    assert auth.service.session(alternate['sid'])['user'] == session['user']
    assert auth.service.websocket_session(alternate['sid'], PUBLIC)['user'] == session['user']
    assert auth.service.websocket_session_key(hashed(alternate['sid']))['user'] == session['user']
    assert web_complete(auth, params).headers['location'] != PUBLIC + '/devices?account_link=success'


@pytest.mark.parametrize('password', ['abcdefgh', '12345678'])
def test_google_first_adds_email_password_only_after_code_and_password_proof(auth, password):
    session, headers = signed_in(auth, google=True)
    sid = auth.client.cookies.get(auth.service.cookie_name)
    result = auth.client.post('/api/auth/account/password/register', headers=headers,
                             json={'email': 'new@example.com', 'password': password})
    assert result.status_code == 200 and result.json()['verificationRequired']
    assert not auth.client.get('/api/auth/account/methods').json()['password']['linked']
    auth.provider.subject, auth.provider.email = 'native-credential', 'new@example.com'
    auth.provider.id_changes = {}
    result = auth.client.post('/api/auth/account/password/confirm', headers=headers,
                             json={'email': 'new@example.com', 'password': password, 'code': '012345'})
    assert result.status_code == 200, result.text
    assert result.json()['password'] == {'linked': True, 'email': 'new@example.com'}
    assert ('verify', 'new@example.com', '012345') in auth.provider.actions
    assert auth.client.cookies.get(auth.service.cookie_name) == sid
    password_login = login(auth, email='new@example.com', password=password).json()
    assert password_login['user'] == session['user']
    assert auth.service.session(auth.client.cookies.get(auth.service.cookie_name))['user'] == session['user']


def test_same_email_independent_accounts_never_auto_join_or_merge(auth):
    password_session, headers = signed_in(auth)
    params = start_web(auth, headers)
    google_proof(auth, params)
    # Independent sign-in before linking already owns an application account.
    independent = auth.service.create_session(auth.provider.tokens())
    assert independent['user']['id'] != password_session['user']['id']
    assert web_complete(auth, params).headers['location'] == PUBLIC + '/devices?account_link=failed'
    assert not auth.client.get('/api/auth/account/methods').json()['google']['linked']
    assert auth.service.session(independent['sid'])['user']['id'] == independent['user']['id']


@pytest.mark.parametrize('damage', ['logout', 'switch', 'binding', 'nonce', 'expired', 'provider', 'method'])
def test_google_proof_cannot_link_after_binding_or_session_damage(auth, damage):
    session, headers = signed_in(auth)
    sid = auth.client.cookies.get(auth.service.cookie_name)
    params = start_web(auth, headers)
    google_proof(auth, params)
    if damage == 'logout':
        auth.service.invalidate(sid)
    elif damage == 'switch':
        auth.client.cookies.set(auth.service.cookie_name, secrets.token_urlsafe(32))
    elif damage == 'binding':
        auth.client.cookies.set(auth.service.account_auth.cookie_name, secrets.token_urlsafe(32))
    elif damage == 'nonce':
        auth.provider.nonce = secrets.token_urlsafe(32)
    elif damage == 'expired':
        auth.now[0] += 601
    elif damage == 'method':
        auth.provider.id_changes = {}
    if damage == 'provider':
        result = auth.client.get('/api/auth/callback', params={'state': params['state'][0], 'error': 'access_denied'}, follow_redirects=False)
    else:
        result = web_complete(auth, params)
    assert result.headers['location'] == PUBLIC + '/devices?account_link=failed'
    assert not auth.service.account_auth.methods(session['user']['id'])['google']['linked']


def native_ticket(auth, headers, verifier, state):
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    result = auth.client.post('/api/auth/account/google/native/start', headers=headers,
                             json={'challenge': challenge, 'state': state})
    assert result.status_code == 200, result.text
    browser = TestClient(auth.app, base_url=PUBLIC)
    launch = browser.get(result.json()['browserUrl'], follow_redirects=False)
    assert launch.status_code == 302, launch.text
    params = parse_qs(urlsplit(launch.headers['location']).query)
    google_proof(auth, params)
    callback = browser.get('/api/auth/callback', params={'state': params['state'][0], 'code': 'proof'}, follow_redirects=False)
    assert auth.service.cookie_name not in browser.cookies
    browser.close()
    return parse_qs(urlsplit(callback.headers['location']).query)


def test_native_google_link_uses_original_app_session_pkce_and_one_use_ticket(auth):
    session, headers = signed_in(auth)
    headers = {**headers, 'X-Keepsite-Client': 'android'}
    verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    ticket = native_ticket(auth, headers, verifier, state)
    body = {'ticket': ticket['ticket'][0], 'verifier': verifier, 'state': state}
    wrong = auth.client.post('/api/auth/account/google/native/redeem', headers=headers,
                             json={**body, 'verifier': secrets.token_urlsafe(32)})
    assert wrong.status_code == 410
    assert auth.client.post('/api/auth/account/google/native/redeem', headers={**headers, 'X-CSRF-Token': 'bad'}, json=body).status_code == 403
    result = auth.client.post('/api/auth/account/google/native/redeem', headers=headers, json=body)
    assert result.status_code == 200 and result.json()['google']['linked']
    assert auth.client.get('/api/auth/session').json()['user'] == session['user']
    assert auth.client.post('/api/auth/account/google/native/redeem', headers=headers, json=body).status_code == 410


def test_native_link_ticket_cannot_be_redeemed_from_new_session_of_same_account(auth):
    _, headers = signed_in(auth)
    headers = {**headers, 'X-Keepsite-Client': 'android'}
    verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    ticket = native_ticket(auth, headers, verifier, state)
    auth.provider.subject, auth.provider.email, auth.provider.id_changes, auth.provider.nonce = 'subject-a', 'person@example.com', {}, None
    _, new_headers = signed_in(auth)
    result = auth.client.post('/api/auth/account/google/native/redeem', headers={**new_headers, 'X-Keepsite-Client': 'android'},
                             json={'ticket': ticket['ticket'][0], 'verifier': verifier, 'state': state})
    assert result.status_code == 410


def test_password_link_conflicts_and_validation_leave_existing_accounts_intact(auth):
    session, headers = signed_in(auth, google=True)
    auth.provider.subject, auth.provider.email, auth.provider.id_changes = 'other-native', 'other@example.com', {}
    other = auth.service.create_session(auth.provider.tokens())
    result = auth.client.post('/api/auth/account/password/link', headers=headers,
                             json={'email': 'other@example.com', 'password': PASSWORD})
    assert result.status_code == 409 and result.json()['code'] == 'identity_in_use'
    assert auth.service.session(other['sid'])['user']['id'] == other['user']['id']
    assert not auth.service.account_auth.methods(session['user']['id'])['password']['linked']
    assert auth.client.post('/api/auth/account/password/confirm', headers=headers,
        json={'email': 'other@example.com', 'password': PASSWORD, 'code': '012345'}).status_code == 410


def test_account_settings_auth_csrf_origin_recent_login_and_strict_input(auth):
    assert auth.client.get('/api/auth/account/methods').status_code == 401
    _, headers = signed_in(auth)
    assert auth.client.post('/api/auth/account/google/start', headers=HEADERS, json={}).status_code == 403
    assert auth.client.post('/api/auth/account/google/start', headers={**headers, 'Origin': 'https://evil.test'}, json={}).status_code == 403
    assert auth.client.post('/api/auth/account/google/start', headers=headers, json={'returnUrl': 'https://evil.test'}).status_code == 422
    assert auth.client.post('/api/auth/account/password/link', headers=headers,
        content='{"email":"a@example.com","email":"b@example.com","password":"StrongPass!123"}').status_code == 422
    auth.now[0] += 601
    result = auth.client.post('/api/auth/account/google/start', headers=headers, json={})
    assert result.status_code == 403 and result.json()['code'] == 'reauthentication_required'


def test_password_registration_can_resume_after_lost_response_and_wrong_code(auth, monkeypatch):
    _, headers = signed_in(auth, google=True)
    body = {'email': 'new@example.com', 'password': PASSWORD}
    original_register = auth.provider.register
    def response_lost(*args):
        original_register(*args)
        raise AuthError(503, 'provider timeout', 'provider_unavailable')
    monkeypatch.setattr(auth.provider, 'register', response_lost)
    assert auth.client.post('/api/auth/account/password/register', headers=headers, json=body).status_code == 503
    retry = auth.client.post('/api/auth/account/password/register', headers=headers, json=body)
    assert retry.status_code == 200 and ('resend', 'new@example.com') in auth.provider.actions
    auth.provider.action_error = AuthError(400, 'bad code', 'invalid_code')
    assert auth.client.post('/api/auth/account/password/confirm', headers=headers, json={**body, 'code': '999999'}).status_code == 400
    auth.provider.action_error = None
    auth.provider.subject, auth.provider.email, auth.provider.id_changes = 'new-password', 'new@example.com', {}
    assert auth.client.post('/api/auth/account/password/confirm', headers=headers, json={**body, 'code': '012345'}).status_code == 200


def test_cognito_same_email_conflict_is_actionable_and_never_forces_alias(auth, monkeypatch):
    _, headers = signed_in(auth, google=True)
    def conflict(*_args):
        raise AuthError(409, 'provider conflict', 'registration_unavailable')
    def no_resend(*_args):
        raise AuthError(400, 'already confirmed', 'invalid_request')
    monkeypatch.setattr(auth.provider, 'register', conflict)
    monkeypatch.setattr(auth.provider, 'resend', no_resend)
    response = auth.client.post('/api/auth/account/password/register', headers=headers,
                               json={'email': 'person@example.com', 'password': PASSWORD})
    assert response.status_code == 409 and response.json()['code'] == 'registration_unavailable'
    assert '另一邮箱' in response.json()['detail']
    assert not auth.client.get('/api/auth/account/methods').json()['password']['linked']


def test_confirmed_email_resumes_after_password_login_timeout(auth, monkeypatch):
    _, headers = signed_in(auth, google=True)
    body = {'email': 'new@example.com', 'password': PASSWORD}
    assert auth.client.post('/api/auth/account/password/register', headers=headers, json=body).status_code == 200
    actual_login = auth.provider.login
    def timeout(*_args):
        raise AuthError(503, 'response lost', 'provider_unavailable')
    monkeypatch.setattr(auth.provider, 'login', timeout)
    assert auth.client.post('/api/auth/account/password/confirm', headers=headers, json={**body, 'code': '012345'}).status_code == 503
    monkeypatch.setattr(auth.provider, 'login', actual_login)
    auth.provider.subject, auth.provider.email, auth.provider.id_changes = 'new-password', 'new@example.com', {}
    response = auth.client.post('/api/auth/account/password/confirm', headers=headers, json={**body, 'code': '012345'})
    assert response.status_code == 200
    assert [action for action in auth.provider.actions if action[0] == 'verify'] == [('verify', 'new@example.com', '012345')]


def test_already_confirmed_after_ambiguous_success_still_requires_valid_password(auth, monkeypatch):
    _, headers = signed_in(auth, google=True)
    body = {'email': 'new@example.com', 'password': PASSWORD}
    assert auth.client.post('/api/auth/account/password/register', headers=headers, json=body).status_code == 200
    def already_confirmed(*_args):
        raise AuthError(401, 'already confirmed', 'invalid_credentials')
    monkeypatch.setattr(auth.provider, 'verify', already_confirmed)
    auth.provider.action_error = AuthError(401, 'bad password', 'invalid_credentials')
    rejected = auth.client.post('/api/auth/account/password/confirm', headers=headers, json={**body, 'code': '012345'})
    assert rejected.status_code == 403 and rejected.json()['code'] == 'identity_proof_failed'
    assert auth.client.get('/api/auth/session').json()['authenticated']
    auth.provider.action_error = None
    auth.provider.subject, auth.provider.email, auth.provider.id_changes = 'new-password', 'new@example.com', {}
    assert auth.client.post('/api/auth/account/password/confirm', headers=headers, json={**body, 'code': '012345'}).status_code == 200


def test_wrong_candidate_password_never_expires_valid_target_session(auth):
    session, headers = signed_in(auth, google=True)
    auth.provider.action_error = AuthError(401, 'wrong password', 'invalid_credentials')
    rejected = auth.client.post('/api/auth/account/password/link', headers=headers,
                               json={'email': 'candidate@example.com', 'password': PASSWORD})
    assert rejected.status_code == 403 and rejected.json()['code'] == 'identity_proof_failed'
    assert auth.client.get('/api/auth/session').json()['user'] == session['user']
    assert auth.client.get('/api/protected').status_code == 200


def test_concurrent_link_claims_cannot_steal_one_subject(auth):
    first, _ = signed_in(auth)
    auth.provider.subject, auth.provider.email = 'second-owner', 'second@example.com'
    second = auth.service.create_session(auth.provider.tokens())
    auth.provider.subject, auth.provider.email, auth.provider.id_changes = 'google-candidate', 'candidate@example.com', {'identities': GOOGLE}
    tokens = auth.provider.tokens()
    candidate, _ = auth.service.validate_login_tokens(tokens)
    def attach(target):
        try:
            auth.service.account_auth.attach(hashed(target['sid']), target['user']['id'], candidate, tokens, 'google')
            return 200
        except AuthError as error:
            return error.status
    targets = [auth.service.session(auth.client.cookies.get(auth.service.cookie_name)), second]
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(attach, targets))
    assert sorted(statuses) == [200, 409]
    with auth.service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM account_identities WHERE principal_id=?', (candidate['id'],)).fetchone()[0] == 1


def test_linked_google_login_preserves_real_ticket_media_and_isolation(integrated):
    client, app, provider, config = integrated
    original = client.post('/api/auth/login', headers=HEADERS, json={'email': provider.email, 'password': PASSWORD}).json()
    headers = {**HEADERS, 'X-CSRF-Token': original['csrfToken']}
    upload = client.post('/api/media', headers=headers, data={'uploadId': 'linked-photo'},
                        files={'file': ('private.png', image_bytes(), 'image/png')}).json()
    payload = {'id': 'kept-ticket', 'operationId': 'create-kept', 'originalAssetId': upload['assetId'], 'fields': {'title': 'Preserved'}}
    assert client.post('/api/tickets', headers=headers, json=payload).status_code == 200
    before = account_snapshot(client)
    start = client.post('/api/auth/account/google/start', headers=headers, json={}).json()
    launch = client.get(start['browserUrl'], follow_redirects=False)
    params = parse_qs(urlsplit(launch.headers['location']).query)
    provider.subject, provider.id_changes, provider.nonce = 'alternate-google', {'identities': GOOGLE}, params['nonce'][0]
    assert client.get('/api/auth/callback', params={'state': params['state'][0], 'code': 'proof'}, follow_redirects=False).headers['location'].endswith('account_link=success')
    # Real standard Google login, with its normal callback/session creation path.
    start = client.get('/api/auth/google', follow_redirects=False)
    params = parse_qs(urlsplit(start.headers['location']).query)
    provider.nonce = params['nonce'][0]
    assert client.get('/api/auth/callback', params={'state': params['state'][0], 'code': 'login'}, follow_redirects=False).status_code == 303
    assert client.get('/api/auth/session').json()['user'] == original['user']
    assert account_snapshot(client) == before
    assert client.get(upload['url']).status_code == 200
    with app.state.store.transaction() as db:
        assert db.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 1
    provider.subject, provider.email, provider.id_changes, provider.nonce = 'unrelated', 'unrelated@example.com', {}, None
    assert client.post('/api/auth/login', headers=HEADERS, json={'email': provider.email, 'password': PASSWORD}).status_code == 200
    assert account_snapshot(client)['tickets'] == []
    assert client.get(upload['url']).status_code == 404

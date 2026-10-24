"""Native BFF handoff uses real signed tokens, isolated cookie jars and atomic SQL."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import secrets
import sqlite3
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi.testclient import TestClient
import pytest

from backend.auth import AuthError, hashed
from backend.native_auth import APP_RETURN_URI, MAX_FLOWS
from test_auth import auth, signing_key, login, PUBLIC, HEADERS, PASSWORD

ANDROID = {'Origin': PUBLIC, 'X-Keepsite-Client': 'android'}


def proof():
    verifier, state = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    return {'verifier': verifier, 'state': state, 'challenge': challenge}


def start(auth, values, *, headers=None):
    result = auth.client.post('/api/auth/native/google/start', headers=headers or ANDROID,
                              json={key: values[key] for key in ('challenge', 'state')})
    assert result.status_code == 200, result.text
    body = result.json()
    assert set(body) == {'browserUrl', 'expiresAt'} and body['expiresAt'] == int((auth.now[0] + 600) * 1000)
    assert body['browserUrl'].startswith(PUBLIC + '/api/auth/native/google/launch?flow=')
    return body['browserUrl']


def launch(browser, url):
    response = browser.get(url, follow_redirects=False)
    assert response.status_code == 302
    params = parse_qs(urlsplit(response.headers['location']).query)
    assert params['redirect_uri'] == [PUBLIC + '/api/auth/callback']
    assert params['code_challenge_method'] == ['S256'] and params['identity_provider'] == ['Google']
    return params


def callback(auth, browser, params, *, error=None):
    query = {'state': params['state'][0], **({'error': error} if error else {'code': 'one-use-provider-code'})}
    return browser.get('/api/auth/callback?' + urlencode(query), follow_redirects=False)


def completed(auth, browser, values):
    params = launch(browser, start(auth, values))
    auth.provider.nonce = params['nonce'][0]
    result = callback(auth, browser, params)
    assert result.status_code == 303
    uri = urlsplit(result.headers['location'])
    assert uri.scheme == 'io.github.youxiangyu.keepsite' and uri.netloc == '' and uri.path == '/oauth/callback'
    query = parse_qs(uri.query)
    assert set(query) == {'ticket', 'state'} and query['state'] == [values['state']]
    return query['ticket'][0], params


def redeem(auth, ticket, values, *, headers=None):
    return auth.client.post('/api/auth/native/google/redeem', headers=headers or ANDROID,
                            json={'ticket': ticket, 'verifier': values['verifier'], 'state': values['state']})


def test_android_email_and_business_requests_preserve_origin_and_csrf(auth):
    result = auth.client.post('/api/auth/login', headers=ANDROID,
                              json={'email': 'person@example.com', 'password': PASSWORD})
    assert result.status_code == 200
    session = result.json()
    assert auth.client.post('/api/protected', headers=ANDROID).status_code == 403
    assert auth.client.post('/api/protected', headers={**ANDROID, 'X-CSRF-Token': session['csrfToken']}).status_code == 200
    assert auth.client.post('/api/protected', headers={**ANDROID, 'Origin': 'https://foreign.example', 'X-CSRF-Token': session['csrfToken']}).status_code == 403
    sid = auth.client.cookies.get(auth.service.cookie_name)
    assert auth.service.websocket_session(sid, PUBLIC)['user'] == session['user']
    with pytest.raises(AuthError):
        auth.service.websocket_session(sid, 'https://foreign.example')


def test_google_handoff_never_overwrites_browser_web_session_or_pending_web_oauth(auth):
    values = proof()
    with TestClient(auth.app, base_url=PUBLIC) as browser:
        web = browser.post('/api/auth/login', headers=HEADERS,
                           json={'email': 'person@example.com', 'password': PASSWORD}).json()
        web_sid = browser.cookies.get(auth.service.cookie_name)
        web_start = browser.get('/api/auth/google', follow_redirects=False)
        web_binding = browser.cookies.get(auth.service.oauth_cookie)
        web_params = parse_qs(urlsplit(web_start.headers['location']).query)
        auth.provider.subject, auth.provider.email = 'native-subject', 'native@example.com'
        url = start(auth, values)
        params = launch(browser, url)
        assert params['code_challenge'] != [values['challenge']]
        assert params['state'] != [values['state']]
        assert browser.cookies.get(auth.service.oauth_cookie) == web_binding
        auth.provider.nonce = params['nonce'][0]
        result = callback(auth, browser, params)
        ticket = parse_qs(urlsplit(result.headers['location']).query)['ticket'][0]
        assert browser.cookies.get(auth.service.cookie_name) == web_sid
        assert browser.cookies.get(auth.service.oauth_cookie) == web_binding
        assert browser.get('/api/auth/session').json()['user'] == web['user']
        assert not browser.cookies.get(auth.app.state.native_auth.cookie_name)
        assert auth.service.cookie_name + '=' not in result.headers.get('set-cookie', '')
        with auth.service.db() as db:
            row = db.execute("SELECT * FROM native_oauth WHERE phase='complete'").fetchone()
            assert row['ticket_hash'] == hashed(ticket) and row['token_payload']
            assert auth.provider.last_tokens['refresh_token'].encode() not in row['token_payload']
            assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1
        accepted = redeem(auth, ticket, values)
        assert accepted.status_code == 200
        assert accepted.json()['user']['email'] == 'native@example.com'
        assert set(accepted.json()) == {'authenticated', 'configured', 'providers', 'user', 'csrfToken'}
        assert all(value in accepted.headers['set-cookie'] for value in ('HttpOnly', 'Secure', 'SameSite=lax'))
        assert browser.cookies.get(auth.service.cookie_name) == web_sid
        assert browser.get('/api/auth/session').json()['user'] == web['user']
        auth.provider.nonce = web_params['nonce'][0]
        response = browser.get('/api/auth/callback?' + urlencode({'state': web_params['state'][0], 'code': 'web-code'}), follow_redirects=False)
        assert response.status_code == 303 and response.headers['location'] == PUBLIC + '/'


def test_wrong_app_proofs_do_not_consume_ticket_and_success_replay_is_rejected(auth):
    values = proof()
    with TestClient(auth.app, base_url=PUBLIC) as browser:
        ticket, _ = completed(auth, browser, values)
        assert redeem(auth, ticket, {**values, 'verifier': secrets.token_urlsafe(32)}).status_code == 410
        assert redeem(auth, ticket, {**values, 'state': secrets.token_urlsafe(32)}).status_code == 410
        response = redeem(auth, ticket, values)
        assert response.status_code == 200
        csrf = response.json()['csrfToken']
        assert redeem(auth, ticket, values, headers={**ANDROID, 'X-CSRF-Token': csrf}).status_code == 410
        with auth.service.db() as db:
            assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1
            row = db.execute('SELECT * FROM native_oauth').fetchone()
            assert row['phase'] == 'consumed' and row['ticket_hash'] is None and row['token_payload'] is None


def test_redeem_is_atomic_across_concurrent_callers(auth):
    values = proof()
    with TestClient(auth.app, base_url=PUBLIC) as browser:
        ticket, _ = completed(auth, browser, values)
        native = auth.app.state.native_auth
        def consume():
            try:
                return native.redeem(ticket, values['verifier'], values['state'])
            except AuthError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: consume(), range(2)))
        assert sum(isinstance(item, dict) for item in results) == 1 and results.count(410) == 1
        with auth.service.db() as db:
            assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 1


def test_session_insert_failure_rolls_back_ticket_consumption(auth, monkeypatch):
    values = proof()
    with TestClient(auth.app, base_url=PUBLIC) as browser:
        ticket, _ = completed(auth, browser, values)
        original = auth.service.insert_session
        monkeypatch.setattr(auth.service, 'insert_session', lambda *args: (_ for _ in ()).throw(sqlite3.OperationalError('injected')))
        assert redeem(auth, ticket, values).status_code == 503
        with auth.service.db() as db:
            assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
            assert db.execute('SELECT phase FROM native_oauth').fetchone()[0] == 'complete'
        monkeypatch.setattr(auth.service, 'insert_session', original)
        assert redeem(auth, ticket, values).status_code == 200


def test_callback_replay_cannot_cancel_first_inflight_exchange(auth, monkeypatch):
    values = proof()
    with TestClient(auth.app, base_url=PUBLIC) as browser:
        params = launch(browser, start(auth, values))
        auth.provider.nonce = params['nonce'][0]
        native = auth.app.state.native_auth
        binding = browser.cookies.get(native.cookie_name)
        entered, release = threading.Event(), threading.Event()
        original = auth.provider.exchange
        def exchange(*args):
            entered.set()
            assert release.wait(3)
            return original(*args)
        monkeypatch.setattr(auth.provider, 'exchange', exchange)
        with ThreadPoolExecutor(max_workers=1) as executor:
            first = executor.submit(native.complete_browser, params['state'][0], binding, 'provider-code')
            try:
                assert entered.wait(2)
                second = native.complete_browser(params['state'][0], binding, 'provider-code')
                assert second == (PUBLIC + '/?auth_error=google_login_failed', False)
            finally:
                release.set()
            ticket = parse_qs(urlsplit(first.result()[0]).query)['ticket'][0]
        assert redeem(auth, ticket, values).status_code == 200 and auth.provider.exchange_calls == 1


@pytest.mark.parametrize('consumed', [False, True])
def test_callback_replay_after_completion_keeps_binding_and_never_sends_app_error(auth, consumed):
    values = proof()
    with TestClient(auth.app, base_url=PUBLIC) as browser:
        params = launch(browser, start(auth, values))
        native = auth.app.state.native_auth
        binding = browser.cookies.get(native.cookie_name)
        auth.provider.nonce = params['nonce'][0]
        first = callback(auth, browser, params)
        ticket = parse_qs(urlsplit(first.headers['location']).query)['ticket'][0]
        if consumed:
            assert redeem(auth, ticket, values).status_code == 200
        browser.cookies.set(native.cookie_name, binding, domain='keepsite.test', path='/')
        replay = callback(auth, browser, params)
        assert replay.headers['location'] == PUBLIC + '/?auth_error=google_login_failed'
        assert 'set-cookie' not in replay.headers
        assert browser.cookies.get(native.cookie_name) == binding
        assert auth.provider.exchange_calls == 1
        if not consumed:
            assert redeem(auth, ticket, values).status_code == 200


def test_callback_state_lookup_database_failure_returns_safe_response(auth, monkeypatch):
    def unavailable_db(*args):
        raise sqlite3.OperationalError('PRIVATE DATABASE QUERY')
    monkeypatch.setattr(auth.app.state.native_auth, 'owns_state', unavailable_db)
    response = auth.client.get('/api/auth/callback?state=private-state&code=private-code', follow_redirects=False)
    assert response.status_code == 303
    assert response.headers['location'] == PUBLIC + '/?auth_error=google_login_failed'
    assert response.headers['cache-control'] == 'no-store'
    assert 'set-cookie' not in response.headers
    assert 'PRIVATE' not in response.text + str(response.headers)


def test_web_and_native_oauth_purposes_cannot_be_exchanged(auth):
    values = proof()
    with TestClient(auth.app, base_url=PUBLIC) as browser:
        params = launch(browser, start(auth, values))
        native = auth.app.state.native_auth
        binding = browser.cookies.get(native.cookie_name)
        with pytest.raises(AuthError, match='用途'):
            auth.service.consume_google_attempt(params['state'][0], binding, purpose='web')
        with auth.service.db() as db:
            assert db.execute('SELECT COUNT(*) FROM oauth').fetchone()[0] == 1
        web_start = browser.get('/api/auth/google', follow_redirects=False)
        web_state = parse_qs(urlsplit(web_start.headers['location']).query)['state'][0]
        location, cleared = native.complete_browser(web_state, binding, 'code')
        assert location == PUBLIC + '/?auth_error=google_login_failed' and not cleared


@pytest.mark.parametrize('damage', ['binding', 'nonce', 'unverified', 'provider', 'cancel'])
def test_invalid_browser_identity_never_creates_native_session_or_leaks_provider_details(auth, monkeypatch, damage):
    values = proof()
    with TestClient(auth.app, base_url=PUBLIC) as browser:
        params = launch(browser, start(auth, values))
        auth.provider.nonce = params['nonce'][0]
        if damage == 'binding':
            browser.cookies.set(auth.app.state.native_auth.cookie_name, secrets.token_urlsafe(32))
        elif damage == 'nonce':
            auth.provider.nonce = 'wrong'
        elif damage == 'unverified':
            auth.provider.id_changes['email_verified'] = False
        elif damage == 'provider':
            monkeypatch.setattr(auth.provider, 'exchange', lambda *args: (_ for _ in ()).throw(AuthError(503, 'PRIVATE PROVIDER SECRET')))
        result = callback(auth, browser, params, error='access_denied' if damage == 'cancel' else None)
        assert result.status_code == 303 and 'ticket=' not in result.headers['location']
        assert 'PRIVATE' not in result.text + result.headers['location']
        if damage == 'binding':
            assert result.headers['location'] == PUBLIC + '/?auth_error=google_login_failed'
            assert auth.provider.exchange_calls == 0
        else:
            returned = parse_qs(urlsplit(result.headers['location']).query)
            assert returned['state'] == [values['state']]
            assert returned['error'] == ['cancelled' if damage == 'cancel' else 'google_login_failed']
        with auth.service.db() as db:
            assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM native_oauth WHERE token_payload IS NOT NULL').fetchone()[0] == 0


def test_launch_and_completed_ticket_have_independent_bounded_expiry(auth):
    values = proof()
    with TestClient(auth.app, base_url=PUBLIC) as browser:
        url = start(auth, values)
        auth.now[0] += 601
        assert browser.get(url, follow_redirects=False).status_code == 410
        values = proof()
        ticket, _ = completed(auth, browser, values)
        auth.now[0] += 61
        assert redeem(auth, ticket, values).status_code == 410
        with auth.service.db() as db:
            assert db.execute('SELECT COUNT(*) FROM native_oauth').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0] == 0


def test_native_endpoints_require_android_origin_and_existing_session_csrf(auth):
    values = proof()
    body = {key: values[key] for key in ('challenge', 'state')}
    for headers in ({}, HEADERS, {**ANDROID, 'Origin': 'https://foreign.example'}):
        assert auth.client.post('/api/auth/native/google/start', headers=headers, json=body).status_code == 403
    session = login(auth).json()
    assert auth.client.post('/api/auth/native/google/start', headers=ANDROID, json=body).status_code == 403
    assert start(auth, values, headers={**ANDROID, 'X-CSRF-Token': session['csrfToken']})


def test_native_payload_rejects_duplicates_redirect_injection_and_oversize(auth):
    values = proof()
    body = {key: values[key] for key in ('challenge', 'state')}
    assert auth.client.post('/api/auth/native/google/start', headers=ANDROID, json={**body, 'returnUrl': 'https://foreign.example'}).status_code == 422
    raw = json.dumps(body)[:-1] + ',"state":"' + values['state'] + '"}'
    assert auth.client.post('/api/auth/native/google/start', headers=ANDROID, content=raw).status_code == 422
    assert auth.client.post('/api/auth/native/google/start', headers=ANDROID, content='x' * 4097).status_code == 422
    assert APP_RETURN_URI == 'io.github.youxiangyu.keepsite:/oauth/callback'


def test_native_pending_flow_capacity_is_bounded_and_expiry_reclaims_it(auth):
    native = auth.app.state.native_auth
    for _ in range(MAX_FLOWS):
        values = proof()
        native.start(values['challenge'], values['state'])
    with pytest.raises(AuthError) as error:
        native.start(values['challenge'], values['state'])
    assert error.value.status == 429
    auth.now[0] += 601
    native.start(values['challenge'], values['state'])
    with auth.service.db() as db:
        assert db.execute('SELECT COUNT(*) FROM native_oauth').fetchone()[0] == 1

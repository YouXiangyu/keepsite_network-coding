"""System-browser Google login with one-use, App-PKCE-bound BFF session handoff."""
import base64
import hashlib
import json
import re
import secrets
import sqlite3
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .auth import AuthError, OAUTH_SECONDS, equal_secret, hashed

APP_RETURN_URI = 'io.github.youxiangyu.memoverse:/oauth/callback'
TICKET_SECONDS = 60
MAX_FLOWS = 64
OPAQUE = re.compile(r'[A-Za-z0-9_-]{43}\Z')
VERIFIER = re.compile(r'[A-Za-z0-9._~-]{43,128}\Z')


def opaque(value):
    return isinstance(value, str) and OPAQUE.fullmatch(value) is not None


def unavailable():
    return AuthError(410, '登录交接已失效，请重新发起登录。', 'native_login_expired')


class NativeAuth:
    def __init__(self, auth):
        self.auth = auth
        self.cookie_name = '__Host-memoverse_native_oauth' if auth.config and auth.config.secure else 'memoverse_native_oauth'
        with auth.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS native_oauth (
                flow_hash TEXT PRIMARY KEY, challenge TEXT NOT NULL, app_state TEXT NOT NULL,
                phase TEXT NOT NULL, expires REAL NOT NULL,
                oauth_state_hash TEXT UNIQUE, binding_hash TEXT,
                ticket_hash TEXT UNIQUE, token_payload BLOB
            )''')

    def require_config(self):
        self.auth.require_config()
        if not self.auth.config.google_enabled:
            raise AuthError(503, 'Google 登录尚未配置完成。', 'provider_disabled')
        if not self.auth.config.secure:
            raise AuthError(503, '原生 Google 登录需要 HTTPS 服务地址。', 'native_login_unavailable')

    def _cleanup(self, db):
        stamp = self.auth.clock()
        db.execute('DELETE FROM oauth WHERE state_hash IN (SELECT oauth_state_hash FROM native_oauth WHERE expires<=?)', (stamp,))
        db.execute('DELETE FROM native_oauth WHERE expires<=?', (stamp,))

    def start(self, challenge, state):
        self.require_config()
        if not opaque(challenge) or not opaque(state):
            raise AuthError(422, '登录交接参数格式不正确。', 'invalid_request')
        flow, expiry = secrets.token_urlsafe(32), self.auth.clock() + OAUTH_SECONDS
        with self.auth.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._cleanup(db)
            if db.execute('SELECT COUNT(*) FROM native_oauth').fetchone()[0] >= MAX_FLOWS:
                db.rollback()
                raise AuthError(429, '登录请求较多，请稍后重试。', 'rate_limited')
            db.execute('INSERT INTO native_oauth(flow_hash,challenge,app_state,phase,expires) VALUES (?,?,?,?,?)',
                       (hashed(flow), challenge, state, 'requested', expiry))
            db.commit()
        return {'browserUrl': self.auth.config.public_url + '/api/auth/native/google/launch?' + urlencode({'flow': flow}),
                'expiresAt': int(expiry * 1000)}

    def launch(self, flow):
        self.require_config()
        if not opaque(flow):
            raise unavailable()
        with self.auth.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM native_oauth WHERE flow_hash=?', (hashed(flow),)).fetchone()
            if row is None or row['expires'] <= self.auth.clock() or row['phase'] != 'requested':
                db.rollback()
                raise unavailable()
            attempt = self.auth.google_attempt(purpose='android', expires=row['expires'])
            state_hash, binding_hash = hashed(attempt['state']), hashed(attempt['binding'])
            db.execute('INSERT INTO oauth VALUES (?,?,?,?)', (state_hash, binding_hash, attempt['payload'], attempt['expires']))
            db.execute("UPDATE native_oauth SET phase='launched',oauth_state_hash=?,binding_hash=? WHERE flow_hash=?",
                       (state_hash, binding_hash, row['flow_hash']))
            db.commit()
        return attempt

    def owns_state(self, state):
        if not opaque(state):
            return False
        with self.auth.db() as db:
            return db.execute('SELECT 1 FROM native_oauth WHERE oauth_state_hash=?', (hashed(state),)).fetchone() is not None

    def _fail(self, flow_hash):
        with self.auth.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("DELETE FROM oauth WHERE state_hash IN (SELECT oauth_state_hash FROM native_oauth WHERE flow_hash=? AND phase='exchanging')", (flow_hash,))
            db.execute("UPDATE native_oauth SET phase='failed',expires=?,ticket_hash=NULL,token_payload=NULL WHERE flow_hash=? AND phase='exchanging'",
                       (self.auth.clock() + TICKET_SECONDS, flow_hash))
            db.commit()

    def complete_browser(self, state, binding, code, provider_error=None):
        """Only a verified browser binding permits returning the stored App state."""
        self.require_config()
        fallback = self.auth.config.public_url + '/?auth_error=google_login_failed'
        if not opaque(state) or not opaque(binding):
            return fallback, False
        with self.auth.db() as db:
            row = db.execute('SELECT * FROM native_oauth WHERE oauth_state_hash=?', (hashed(state),)).fetchone()
        if row is None or not equal_secret(row['binding_hash'], hashed(binding)):
            return fallback, False
        if row['phase'] != 'launched' or row['expires'] <= self.auth.clock():
            return fallback, False
        error_code = 'google_login_failed'
        claimed = False
        try:
            with self.auth.db() as db:
                claimed = db.execute("UPDATE native_oauth SET phase='exchanging' WHERE flow_hash=? AND phase='launched' AND expires>?",
                                     (row['flow_hash'], self.auth.clock())).rowcount == 1
            if not claimed:
                return fallback, False
            payload = self.auth.consume_google_attempt(state, binding, purpose='android')
            if provider_error is not None:
                if provider_error == 'access_denied':
                    error_code = 'cancelled'
                raise unavailable()
            tokens = self.auth.exchange_google_attempt(payload, code)
            self.auth.validate_login_tokens(tokens, payload['nonce'])
            ticket = secrets.token_urlsafe(32)
            encrypted = self.auth.encrypt({'tokens': tokens, 'nonce': payload['nonce']})
            with self.auth.db() as db:
                changed = db.execute("UPDATE native_oauth SET phase='complete',expires=?,ticket_hash=?,token_payload=? WHERE flow_hash=? AND phase='exchanging' AND expires>?",
                                     (self.auth.clock() + TICKET_SECONDS, hashed(ticket), encrypted, row['flow_hash'], self.auth.clock())).rowcount
            if changed != 1:
                raise unavailable()
            return APP_RETURN_URI + '?' + urlencode({'ticket': ticket, 'state': row['app_state']}), True
        except (AuthError, sqlite3.Error):
            if not claimed:
                return fallback, False
            if claimed:
                try:
                    self._fail(row['flow_hash'])
                except sqlite3.Error:
                    pass  # Consumed OAuth state cannot be replayed; pending rows still expire.
            return APP_RETURN_URI + '?' + urlencode({'error': error_code, 'state': row['app_state']}), True

    def redeem(self, ticket, verifier, state):
        self.require_config()
        if not opaque(ticket) or not opaque(state) or not isinstance(verifier, str) or not VERIFIER.fullmatch(verifier):
            raise unavailable()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode('ascii').rstrip('=')
        with self.auth.db() as db:
            self._cleanup(db)
            row = db.execute("SELECT * FROM native_oauth WHERE ticket_hash=? AND phase='complete'", (hashed(ticket),)).fetchone()
        if (row is None or row['expires'] <= self.auth.clock() or not equal_secret(row['app_state'], state)
                or not equal_secret(row['challenge'], challenge)):
            raise unavailable()
        payload = self.auth.decrypt(row['token_payload'])
        if set(payload) != {'tokens', 'nonce'}:
            raise unavailable()
        session, values = self.auth.prepare_session(payload['tokens'], payload['nonce'])
        with self.auth.db() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE native_oauth SET phase='consumed',expires=?,ticket_hash=NULL,token_payload=NULL WHERE flow_hash=? AND phase='complete' AND ticket_hash=? AND expires>?",
                                 (self.auth.clock() + TICKET_SECONDS, row['flow_hash'], hashed(ticket), self.auth.clock())).rowcount
            if changed != 1:
                db.rollback()
                raise unavailable()
            self.auth.insert_session(db, values)
            db.commit()
        return session


def install_native_auth(app, auth, login_response, failure):
    native = NativeAuth(auth)
    app.state.native_auth = native

    async def payload(request, fields):
        auth.check_write(request)
        if request.headers.get('x-memoverse-client') != 'android':
            raise AuthError(403, '原生登录请求来源验证失败。', 'invalid_origin')
        auth.limiter.check(request.client.host if request.client else '', 'native-auth')
        sid = request.cookies.get(auth.cookie_name)
        if sid:
            def current_csrf():
                with auth.db() as db:
                    return db.execute('SELECT csrf FROM sessions WHERE sid_hash=? AND expires>?', (hashed(sid), auth.clock())).fetchone()
            row = await run_in_threadpool(current_csrf)
            if row:
                auth.check_write(request, {'csrf': row['csrf']})
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 4096:
                    raise ValueError()
            value = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value, dict) or set(value) != set(fields):
                raise ValueError()
            return value
        except (ValueError, TypeError):
            raise AuthError(422, '登录交接参数格式不正确。', 'invalid_request') from None

    @app.post('/api/auth/native/google/start')
    async def start(request: Request):
        try:
            body = await payload(request, ('challenge', 'state'))
            result = await run_in_threadpool(native.start, body['challenge'], body['state'])
            return JSONResponse(result, headers={'Cache-Control': 'no-store'})
        except (AuthError, sqlite3.Error) as error:
            return failure(error)

    @app.get('/api/auth/native/google/launch')
    async def launch(request: Request):
        try:
            auth.limiter.check(request.client.host if request.client else '', 'native-launch')
            if set(request.query_params) != {'flow'} or len(request.query_params.getlist('flow')) != 1:
                raise unavailable()
            attempt = await run_in_threadpool(native.launch, request.query_params['flow'])
            response = RedirectResponse(attempt['url'], status_code=302, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
            auth.cookie(response, native.cookie_name, attempt['binding'], attempt['expires'] - auth.clock())
            return response
        except (AuthError, sqlite3.Error) as error:
            return failure(error)

    @app.post('/api/auth/native/google/redeem')
    async def redeem(request: Request):
        try:
            body = await payload(request, ('ticket', 'verifier', 'state'))
            session = await run_in_threadpool(native.redeem, body['ticket'], body['verifier'], body['state'])
            return await login_response(session, request)
        except (AuthError, sqlite3.Error) as error:
            return failure(error)

    return native

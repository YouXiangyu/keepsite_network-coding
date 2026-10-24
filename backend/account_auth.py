"""Explicit, proven sign-in identities for an immutable application account.

Cognito profiles stay independent. Email equality never joins accounts, and an
identity that has already opened another workspace cannot be moved or merged.
"""
import base64
import hashlib
import json
import secrets
import sqlite3
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .auth import AuthError, OAUTH_SECONDS, code_value, equal_secret, hashed, normalize_email, password_value
from .native_auth import APP_RETURN_URI, TICKET_SECONDS, opaque, VERIFIER


class AccountAuth:
    def __init__(self, auth):
        self.auth = auth
        self.owner_exists = lambda _owner: False
        self.cookie_name = '__Host-keepsite_account_oauth' if auth.config and auth.config.secure else 'keepsite_account_oauth'
        with auth.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS account_profiles (
                    account_id TEXT PRIMARY KEY, user_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS account_identities (
                    principal_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    method TEXT NOT NULL CHECK(method IN ('password','google')),
                    email TEXT NOT NULL, UNIQUE(account_id,method)
                );
                CREATE TABLE IF NOT EXISTS account_password_pending (
                    sid_hash TEXT PRIMARY KEY, email TEXT NOT NULL, expires REAL NOT NULL,
                    verified INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS account_oauth (
                    flow_hash TEXT PRIMARY KEY, sid_hash TEXT NOT NULL, account_id TEXT NOT NULL,
                    kind TEXT NOT NULL, challenge TEXT, app_state TEXT,
                    phase TEXT NOT NULL, expires REAL NOT NULL,
                    oauth_state_hash TEXT UNIQUE, binding_hash TEXT,
                    ticket_hash TEXT UNIQUE, candidate BLOB
                );
            ''')

    def principal(self, user, tokens):
        claims = self.auth.validator.claims(tokens.get('id_token'), 'id')
        identities = claims.get('identities', [])
        google = isinstance(identities, list) and any(
            isinstance(item, dict) and item.get('providerName') == 'Google'
            and isinstance(item.get('userId'), str) and item['userId'] for item in identities)
        return user['id'], 'google' if google else 'password', user['email']

    def resolve(self, user, tokens):
        """Resolve only verified subjects; retain the original account profile and ID."""
        principal, method, email = self.principal(user, tokens)
        with self.auth.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT account_id FROM account_identities WHERE principal_id=?', (principal,)).fetchone()
            owner = row['account_id'] if row else principal
            if not row:
                db.execute('INSERT INTO account_identities VALUES (?,?,?,?)', (principal, owner, method, email))
            else:
                db.execute('UPDATE account_identities SET email=? WHERE principal_id=?', (email, principal))
            db.execute('INSERT OR IGNORE INTO account_profiles VALUES (?,?)', (owner, json.dumps({**user, 'id': owner})))
            profile = json.loads(db.execute('SELECT user_json FROM account_profiles WHERE account_id=?', (owner,)).fetchone()[0])
            db.commit()
        return profile

    def methods(self, owner):
        result = {method: {'linked': False, 'email': None} for method in ('password', 'google')}
        with self.auth.db() as db:
            rows = db.execute('SELECT method,email FROM account_identities WHERE account_id=?', (owner,)).fetchall()
        for row in rows:
            result[row['method']] = {'linked': True, 'email': row['email']}
        return result

    def require_target(self, sid, *, recent=False):
        session = self.auth.session(sid)
        if session is None:
            raise AuthError(401, '请先登录。', 'authentication_required')
        if recent:
            with self.auth.db() as db:
                row = db.execute('SELECT created FROM sessions WHERE sid_hash=?', (hashed(sid),)).fetchone()
            if row is None or self.auth.clock() - row['created'] > OAUTH_SECONDS:
                raise AuthError(403, '为保护账户，请重新登录后再添加登录方式。', 'reauthentication_required')
        return session

    def _live_target(self, db, sid_hash, owner):
        row = db.execute('SELECT user_json FROM sessions WHERE sid_hash=? AND expires>?', (sid_hash, self.auth.clock())).fetchone()
        if row is None or json.loads(row['user_json'])['id'] != owner:
            raise AuthError(403, '发起绑定的登录状态已结束，请重新操作。', 'link_session_expired')

    def attach(self, sid_hash, owner, candidate, tokens, method, *, flow_hash=None, ticket_hash=None):
        principal, proved_method, email = self.principal(candidate, tokens)
        if proved_method != method:
            raise AuthError(403, '请使用对应的登录方式完成验证。', 'identity_method_mismatch')
        with self.auth.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._live_target(db, sid_hash, owner)
            if flow_hash:
                row = db.execute('SELECT * FROM account_oauth WHERE flow_hash=?', (flow_hash,)).fetchone()
                phase = 'complete' if ticket_hash else 'exchanging'
                if (row is None or row['phase'] != phase or row['expires'] <= self.auth.clock()
                        or row['sid_hash'] != sid_hash or row['account_id'] != owner
                        or ticket_hash and not equal_secret(row['ticket_hash'], ticket_hash)):
                    raise AuthError(410, '绑定请求已失效，请重新发起。', 'link_expired')
            existing = db.execute('SELECT account_id FROM account_identities WHERE principal_id=?', (principal,)).fetchone()
            other = db.execute('SELECT principal_id FROM account_identities WHERE account_id=? AND method=?', (owner, method)).fetchone()
            if existing and existing['account_id'] != owner or other and other['principal_id'] != principal:
                raise AuthError(409, '此登录方式已属于一个账户，无法合并；原有数据保持不变。', 'identity_in_use')
            if not existing and principal != owner and (self.owner_exists(principal)
                    or db.execute('SELECT 1 FROM sessions WHERE json_extract(user_json,\'$.id\')=? LIMIT 1', (principal,)).fetchone()):
                raise AuthError(409, '此登录方式已有独立账户，无法合并；原有数据保持不变。', 'identity_in_use')
            db.execute('INSERT OR IGNORE INTO account_identities VALUES (?,?,?,?)', (principal, owner, method, email))
            if flow_hash:
                db.execute("UPDATE account_oauth SET phase='consumed',candidate=NULL,ticket_hash=NULL WHERE flow_hash=?", (flow_hash,))
            if method == 'password':
                db.execute('DELETE FROM account_password_pending WHERE sid_hash=?', (sid_hash,))
            db.commit()
        return self.methods(owner)

    def password_register(self, session, email, password):
        if self.methods(session['user']['id'])['password']['linked']:
            raise AuthError(409, '此账户已设置邮箱密码登录。', 'identity_in_use')
        with self.auth.db() as db:
            self._live_target(db, hashed(session['sid']), session['user']['id'])
            db.execute('DELETE FROM account_password_pending WHERE expires<=?', (self.auth.clock(),))
            pending = db.execute('SELECT email FROM account_password_pending WHERE sid_hash=?', (hashed(session['sid']),)).fetchone()
            db.execute('INSERT OR REPLACE INTO account_password_pending(sid_hash,email,expires) VALUES (?,?,?)',
                       (hashed(session['sid']), email, self.auth.clock() + OAUTH_SECONDS))
        # Save the challenge binding before the provider call. A lost signup
        # response must still permit confirmation or a safe resend on retry.
        if pending and pending['email'] == email:
            try:
                self.auth.provider().resend(email)
                return {'verificationRequired': True, 'email': email}
            except AuthError as error:
                if error.code != 'invalid_credentials':
                    raise AuthError(error.status, '验证码未能重发。如果邮箱已验证，请选择关联已有邮箱密码；也可使用另一邮箱。', error.code) from None
        try:
            self.auth.provider().register(email, password)
        except AuthError as error:
            if error.code != 'registration_unavailable':
                raise
            try:
                self.auth.provider().resend(email)
            except AuthError:
                raise AuthError(409, '此邮箱无法创建独立密码登录。已有密码请选关联；Google 邮箱受服务限制时，请使用另一邮箱。', 'registration_unavailable') from None
        return {'verificationRequired': True, 'email': email}

    def password_link(self, session, email, password, code=None):
        if code is not None:
            with self.auth.db() as db:
                row = db.execute('SELECT email,verified FROM account_password_pending WHERE sid_hash=? AND expires>?',
                                 (hashed(session['sid']), self.auth.clock())).fetchone()
            if row is None or row['email'] != email:
                raise AuthError(410, '邮箱验证请求已失效，请重新设置。', 'link_expired')
            if not row['verified']:
                # Never request ForceAliasCreation. On an ambiguous prior success,
                # an already-confirmed error may continue only with full password
                # proof below, just like the explicit existing-password route.
                try:
                    self.auth.provider().verify(email, code)
                except AuthError as error:
                    if error.code not in ('invalid_credentials', 'invalid_request'):
                        raise
                else:
                    with self.auth.db() as db:
                        changed = db.execute('UPDATE account_password_pending SET verified=1 WHERE sid_hash=? AND email=? AND expires>?',
                            (hashed(session['sid']), email, self.auth.clock())).rowcount
                    if changed != 1:
                        raise AuthError(410, '邮箱验证请求已失效，请重新设置。', 'link_expired')
        tokens = self.auth.provider().login(email, password)
        try:
            candidate, _ = self.auth.validate_login_tokens(tokens)
            if candidate['email'] != email:
                raise AuthError(403, '邮箱验证与登录身份不一致。', 'identity_method_mismatch')
            return self.attach(hashed(session['sid']), session['user']['id'], candidate, tokens, 'password')
        finally:
            self.revoke_proof(tokens)

    def revoke_proof(self, tokens):
        refresh = tokens.get('refresh_token') if isinstance(tokens, dict) else None
        if not isinstance(refresh, str) or not refresh:
            return
        try:
            self.auth.provider().revoke(refresh)
        except AuthError:
            pass  # Proof tokens are never installed as an application session.

    def start(self, session, *, challenge=None, state=None):
        if not self.auth.config.google_enabled:
            raise AuthError(503, 'Google 登录尚未配置完成。', 'provider_disabled')
        native = challenge is not None
        if native and (not self.auth.config.secure or not opaque(challenge) or not opaque(state)):
            raise AuthError(422, '原生绑定参数格式不正确。', 'invalid_request')
        flow, expiry = secrets.token_urlsafe(32), self.auth.clock() + OAUTH_SECONDS
        with self.auth.db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._cleanup(db)
            self._live_target(db, hashed(session['sid']), session['user']['id'])
            if db.execute("SELECT COUNT(*) FROM account_oauth WHERE phase IN ('requested','launched','exchanging','complete')").fetchone()[0] >= 64:
                raise AuthError(429, '绑定请求较多，请稍后重试。', 'rate_limited')
            db.execute('INSERT INTO account_oauth(flow_hash,sid_hash,account_id,kind,challenge,app_state,phase,expires) VALUES (?,?,?,?,?,?,?,?)',
                       (hashed(flow), hashed(session['sid']), session['user']['id'], 'native' if native else 'web', challenge, state, 'requested', expiry))
            db.commit()
        return {'browserUrl': self.auth.config.public_url + '/api/auth/account/google/launch?' + urlencode({'flow': flow}),
                'expiresAt': int(expiry * 1000)}

    def _cleanup(self, db):
        db.execute('DELETE FROM oauth WHERE state_hash IN (SELECT oauth_state_hash FROM account_oauth WHERE expires<=?)', (self.auth.clock(),))
        db.execute('DELETE FROM account_oauth WHERE expires<=?', (self.auth.clock(),))

    def launch(self, flow, sid):
        if not opaque(flow):
            raise AuthError(410, '绑定请求已失效。', 'link_expired')
        with self.auth.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM account_oauth WHERE flow_hash=?', (hashed(flow),)).fetchone()
            if row is None or row['phase'] != 'requested' or row['expires'] <= self.auth.clock():
                raise AuthError(410, '绑定请求已失效。', 'link_expired')
            self._live_target(db, row['sid_hash'], row['account_id'])
            if row['kind'] == 'web' and (not sid or not equal_secret(hashed(sid), row['sid_hash'])):
                raise AuthError(403, '绑定请求不属于此登录状态。', 'link_session_expired')
            attempt = self.auth.google_attempt(purpose='account', expires=row['expires'])
            db.execute('INSERT INTO oauth VALUES (?,?,?,?)', (hashed(attempt['state']), hashed(attempt['binding']), attempt['payload'], attempt['expires']))
            db.execute("UPDATE account_oauth SET phase='launched',oauth_state_hash=?,binding_hash=? WHERE flow_hash=?",
                       (hashed(attempt['state']), hashed(attempt['binding']), hashed(flow)))
            db.commit()
        attempt['url'] += '&prompt=select_account'
        return attempt

    def owns_state(self, state):
        if not opaque(state):
            return False
        with self.auth.db() as db:
            return db.execute('SELECT 1 FROM account_oauth WHERE oauth_state_hash=?', (hashed(state),)).fetchone() is not None

    def complete(self, state, binding, code, sid, provider_error=None):
        fallback = self.auth.config.public_url + '/devices?account_link=failed'
        if not opaque(state) or not opaque(binding):
            return fallback
        with self.auth.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM account_oauth WHERE oauth_state_hash=?', (hashed(state),)).fetchone()
            if (row is None or row['phase'] != 'launched' or row['expires'] <= self.auth.clock()
                    or not equal_secret(row['binding_hash'], hashed(binding))):
                return fallback
            self._live_target(db, row['sid_hash'], row['account_id'])
            if row['kind'] == 'web' and (not sid or not equal_secret(hashed(sid), row['sid_hash'])):
                return fallback
            db.execute("UPDATE account_oauth SET phase='exchanging' WHERE flow_hash=?", (row['flow_hash'],))
            db.commit()
        tokens = None
        try:
            payload = self.auth.consume_google_attempt(state, binding, purpose='account')
            if provider_error is not None:
                raise AuthError(400, 'Google 验证未完成。', 'link_failed')
            tokens = self.auth.exchange_google_attempt(payload, code)
            candidate, _ = self.auth.validate_login_tokens(tokens, payload['nonce'])
            if self.principal(candidate, tokens)[1] != 'google':
                raise AuthError(403, '请使用 Google 完成验证。', 'identity_method_mismatch')
            if row['kind'] == 'web':
                self.attach(row['sid_hash'], row['account_id'], candidate, tokens, 'google', flow_hash=row['flow_hash'])
                return self.auth.config.public_url + '/devices?account_link=success'
            ticket = secrets.token_urlsafe(32)
            # Retain only short-lived signed proof, encrypted, until App PKCE.
            proof_tokens = {key: tokens[key] for key in ('id_token', 'access_token')}
            with self.auth.db() as db:
                self._live_target(db, row['sid_hash'], row['account_id'])
                changed = db.execute("UPDATE account_oauth SET phase='complete',ticket_hash=?,candidate=?,expires=? WHERE flow_hash=? AND phase='exchanging' AND expires>?",
                    (hashed(ticket), self.auth.encrypt(proof_tokens), self.auth.clock() + TICKET_SECONDS, row['flow_hash'], self.auth.clock())).rowcount
                if changed != 1:
                    raise AuthError(410, '绑定请求已失效。', 'link_expired')
            return APP_RETURN_URI + '?' + urlencode({'ticket': ticket, 'state': row['app_state']})
        except (AuthError, sqlite3.Error):
            with self.auth.db() as db:
                db.execute("UPDATE account_oauth SET phase='failed',candidate=NULL,ticket_hash=NULL WHERE flow_hash=? AND phase='exchanging'", (row['flow_hash'],))
            return (APP_RETURN_URI + '?' + urlencode({'error': 'account_link_failed', 'state': row['app_state']})) if row['kind'] == 'native' else fallback
        finally:
            if tokens:
                self.revoke_proof(tokens)

    def redeem(self, session, ticket, verifier, state):
        if not opaque(ticket) or not opaque(state) or not isinstance(verifier, str) or not VERIFIER.fullmatch(verifier):
            raise AuthError(410, '绑定交接已失效。', 'link_expired')
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode('ascii').rstrip('=')
        with self.auth.db() as db:
            row = db.execute("SELECT * FROM account_oauth WHERE ticket_hash=? AND phase='complete'", (hashed(ticket),)).fetchone()
        if (row is None or row['expires'] <= self.auth.clock() or row['kind'] != 'native'
                or not equal_secret(row['challenge'], challenge) or not equal_secret(row['app_state'], state)
                or row['sid_hash'] != hashed(session['sid']) or row['account_id'] != session['user']['id']):
            raise AuthError(410, '绑定交接已失效。', 'link_expired')
        proof = self.auth.decrypt(row['candidate'])
        candidate, _ = self.auth.validator.pair(proof)
        return self.attach(row['sid_hash'], row['account_id'], candidate, proof, 'google',
                           flow_hash=row['flow_hash'], ticket_hash=hashed(ticket))

    async def callback(self, request):
        fallback = self.auth.config.public_url + '/devices?account_link=failed'
        try:
            if (len(request.query_params.getlist('state')) != 1 or len(request.query_params.getlist('code')) > 1
                    or len(request.query_params.getlist('error')) > 1
                    or 'code' in request.query_params and 'error' in request.query_params):
                raise AuthError(400, '绑定参数格式不正确。', 'invalid_request')
            self.auth.limiter.check(request.client.host if request.client else '', 'account-callback')
            url = await run_in_threadpool(self.complete, request.query_params.get('state'), request.cookies.get(self.cookie_name),
                request.query_params.get('code'), request.cookies.get(self.auth.cookie_name), request.query_params.get('error'))
        except (AuthError, sqlite3.Error):
            url = fallback
        response = RedirectResponse(url, status_code=303, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        self.auth.clear_cookie(response, self.cookie_name)
        return response


def install_account_auth(app, auth, failure):
    account = auth.account_auth
    store = getattr(app.state, 'store', None)
    if store is not None:
        def owner_exists(owner):
            with store.transaction() as db:
                return db.execute('SELECT 1 FROM users WHERE id=?', (owner,)).fetchone() is not None
        account.owner_exists = owner_exists

    async def authorized(request, fields=None, *, recent=False, native=False):
        session = await run_in_threadpool(account.require_target, request.cookies.get(auth.cookie_name), recent=recent)
        if fields is None:
            return session, None
        auth.check_write(request, session)
        if native and request.headers.get('x-keepsite-client') != 'android':
            raise AuthError(403, '原生绑定请求来源验证失败。', 'invalid_origin')
        auth.limiter.check(request.client.host if request.client else '', 'account-write', session['user']['id'])
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError()
                    result[key] = value
                return result
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 4096:
                    raise ValueError()
            body = json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(body, dict) or set(body) != set(fields):
                raise ValueError()
        except (ValueError, TypeError):
            raise AuthError(422, '登录方式信息格式不正确。', 'invalid_request') from None
        return session, body

    @app.get('/api/auth/account/methods')
    async def methods(request: Request):
        try:
            session, _ = await authorized(request)
            return JSONResponse(await run_in_threadpool(account.methods, session['user']['id']))
        except (AuthError, sqlite3.Error) as error:
            return failure(error)

    async def password_action(request, action):
        try:
            fields = ('email', 'password', 'code') if action == 'confirm' else ('email', 'password')
            session, body = await authorized(request, fields, recent=True)
            email, password = normalize_email(body['email']), password_value(body['password'])
            code = code_value(body['code']) if action == 'confirm' else None
            try:
                if action == 'register':
                    result = await run_in_threadpool(account.password_register, session, email, password)
                else:
                    result = await run_in_threadpool(account.password_link, session, email, password, code)
            except AuthError as error:
                if error.status == 401:
                    raise AuthError(403, '待关联的邮箱或密码验证失败，请核对后重试。', 'identity_proof_failed') from None
                raise
            return JSONResponse(result)
        except (AuthError, sqlite3.Error) as error:
            return failure(error)

    @app.post('/api/auth/account/password/register')
    async def password_register(request: Request):
        return await password_action(request, 'register')

    @app.post('/api/auth/account/password/confirm')
    async def password_confirm(request: Request):
        return await password_action(request, 'confirm')

    @app.post('/api/auth/account/password/link')
    async def password_link(request: Request):
        return await password_action(request, 'link')

    async def google_start(request, native=False):
        try:
            session, body = await authorized(request, ('challenge', 'state') if native else (), recent=True, native=native)
            result = await run_in_threadpool(account.start, session, **({'challenge': body['challenge'], 'state': body['state']} if native else {}))
            return JSONResponse(result)
        except (AuthError, sqlite3.Error) as error:
            return failure(error)

    @app.post('/api/auth/account/google/start')
    async def google_web(request: Request):
        return await google_start(request)

    @app.post('/api/auth/account/google/native/start')
    async def google_native(request: Request):
        return await google_start(request, native=True)

    @app.get('/api/auth/account/google/launch')
    async def google_launch(request: Request):
        try:
            auth.limiter.check(request.client.host if request.client else '', 'account-launch')
            if set(request.query_params) != {'flow'} or len(request.query_params.getlist('flow')) != 1:
                raise AuthError(410, '绑定请求已失效。', 'link_expired')
            attempt = await run_in_threadpool(account.launch, request.query_params['flow'], request.cookies.get(auth.cookie_name))
            response = RedirectResponse(attempt['url'], status_code=302, headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
            auth.cookie(response, account.cookie_name, attempt['binding'], attempt['expires'] - auth.clock())
            return response
        except (AuthError, sqlite3.Error) as error:
            return failure(error)

    @app.post('/api/auth/account/google/native/redeem')
    async def native_redeem(request: Request):
        try:
            session, body = await authorized(request, ('ticket', 'verifier', 'state'), native=True)
            try:
                result = await run_in_threadpool(account.redeem, session, body['ticket'], body['verifier'], body['state'])
            except AuthError as error:
                if error.status == 401:
                    raise AuthError(410, 'Google 验证已失效，请重新发起绑定。', 'link_expired') from None
                raise
            return JSONResponse(result)
        except (AuthError, sqlite3.Error) as error:
            return failure(error)

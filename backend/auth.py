"""Cognito BFF authentication. Browser cookies contain opaque IDs, never tokens."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import string
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request as URLRequest, build_opener

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
import jwt
from starlette.concurrency import run_in_threadpool

SESSION_SECONDS = 7 * 24 * 3600
OAUTH_SECONDS = 600
WRITE_METHODS = {'POST', 'PUT', 'PATCH', 'DELETE'}


class AuthError(Exception):
    def __init__(self, status, detail, code='authentication_failed'):
        super().__init__(detail)
        self.status, self.detail, self.code = status, detail, code


def hashed(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def equal_secret(left, right):
    return isinstance(left, str) and isinstance(right, str) and hmac.compare_digest(left.encode('utf-8'), right.encode('utf-8'))


def normalize_email(value):
    if not isinstance(value, str):
        raise AuthError(422, '请输入有效邮箱。', 'invalid_email')
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\.[a-z]{2,63}", value):
        raise AuthError(422, '请输入有效邮箱。', 'invalid_email')
    local, domain = value.rsplit('@', 1)
    if len(local) > 64 or local.startswith('.') or local.endswith('.') or '..' in value or any(part.startswith('-') or part.endswith('-') for part in domain.split('.')):
        raise AuthError(422, '请输入有效邮箱。', 'invalid_email')
    return value


def password_value(value):
    if not isinstance(value, str) or not 12 <= len(value) <= 128 or not all((
        any(c in string.ascii_uppercase for c in value), any(c in string.ascii_lowercase for c in value),
        any(c in string.digits for c in value), any(c in string.punctuation for c in value),
    )):
        raise AuthError(422, '密码需为12–128个字符，并包含大小写字母、数字和符号。', 'password_policy')
    return value  # Never strip or normalize passwords.


def code_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,16}', value):
        raise AuthError(422, '请输入邮件中的验证码。', 'invalid_code')
    return value


@dataclass(frozen=True)
class AuthConfig:
    region: str
    pool_id: str
    client_id: str
    domain: str
    public_url: str
    session_key: str
    google_enabled: bool = False

    def __post_init__(self):
        if not re.fullmatch(r'[a-z]{2}(?:-[a-z]+)+-\d', self.region) or not self.pool_id.startswith(self.region + '_') or not re.fullmatch(r'[\w-]+', self.pool_id):
            raise ValueError('Invalid Cognito region/pool')
        if not re.fullmatch(r'[A-Za-z0-9]{1,128}', self.client_id):
            raise ValueError('Invalid Cognito client')
        for value, public in ((self.domain, False), (self.public_url, True)):
            parsed = urlsplit(value)
            localhost = parsed.hostname in ('localhost', '127.0.0.1', '::1')
            if any(ord(character) <= 32 for character in value) or (not public and parsed.port not in (None, 443)):
                raise ValueError('Invalid authentication origin')
            if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
                raise ValueError('Authentication URLs must be exact origins')
            if parsed.scheme != 'https' and not (public and localhost and parsed.scheme == 'http'):
                raise ValueError('HTTPS required outside explicit localhost development')
        Fernet(self.session_key.encode('ascii'))
        object.__setattr__(self, 'domain', self.domain.rstrip('/'))
        object.__setattr__(self, 'public_url', self.public_url.rstrip('/'))

    @property
    def issuer(self):
        return f'https://cognito-idp.{self.region}.amazonaws.com/{self.pool_id}'

    @property
    def secure(self):
        return self.public_url.startswith('https://')

    @classmethod
    def from_env(cls):
        fields = ('REGION', 'USER_POOL_ID', 'CLIENT_ID', 'DOMAIN')
        values = [os.environ.get('MEMOVERSE_COGNITO_' + field, '') for field in fields]
        values += [os.environ.get('MEMOVERSE_PUBLIC_URL', ''), os.environ.get('MEMOVERSE_SESSION_KEY', '')]
        if not all(values):
            return None
        try:
            return cls(*values, google_enabled=os.environ.get('MEMOVERSE_GOOGLE_ENABLED', '').lower() == 'true')
        except (ValueError, TypeError, UnicodeError):
            return None  # Fail closed, without printing configuration or key material.


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class CognitoProvider:
    """Public Cognito operations use UNSIGNED requests and never AWS static keys."""
    def __init__(self, config):
        self.config = config
        self.client = boto3.client('cognito-idp', region_name=config.region,
            config=Config(signature_version=UNSIGNED, connect_timeout=3, read_timeout=5,
                          retries={'mode': 'standard', 'total_max_attempts': 1}))
        self.http = build_opener(NoRedirect())

    def call(self, operation, **kwargs):
        try:
            return getattr(self.client, operation)(**kwargs)
        except ClientError as error:
            code = error.response.get('Error', {}).get('Code', '')
            if code == 'UserNotConfirmedException':
                raise AuthError(403, '请先验证邮箱，再登录。', 'email_unverified') from None
            if code in ('TooManyRequestsException', 'LimitExceededException', 'TooManyFailedAttemptsException'):
                raise AuthError(429, '操作过于频繁，请稍后再试。', 'rate_limited') from None
            if code in ('NotAuthorizedException', 'UserNotFoundException'):
                raise AuthError(401, '邮箱或密码不正确，或登录状态已失效。', 'invalid_credentials') from None
            if code in ('CodeMismatchException', 'ExpiredCodeException'):
                raise AuthError(400, '验证码不正确或已过期。', 'invalid_code') from None
            if code == 'UsernameExistsException':
                raise AuthError(409, '暂时无法注册此邮箱，请尝试登录或找回密码。', 'registration_unavailable') from None
            if code == 'InvalidPasswordException':
                raise AuthError(422, '密码不符合安全要求。', 'password_policy') from None
            if code in ('InvalidParameterException', 'ForbiddenException'):
                raise AuthError(400, '认证请求未能完成，请检查输入后重试。', 'invalid_request') from None
            raise AuthError(503, '认证服务暂不可用，请稍后重试。', 'provider_unavailable') from None
        except BotoCoreError:
            raise AuthError(503, '认证服务暂不可用，请稍后重试。', 'provider_unavailable') from None

    @staticmethod
    def tokens(result):
        values = result.get('AuthenticationResult')
        if not isinstance(values, dict):
            raise AuthError(403, '当前登录流程需要额外验证，暂不支持。', 'unsupported_challenge')
        return {'id_token': values.get('IdToken'), 'access_token': values.get('AccessToken'), 'refresh_token': values.get('RefreshToken')}

    def login(self, email, password):
        return self.tokens(self.call('initiate_auth', ClientId=self.config.client_id, AuthFlow='USER_PASSWORD_AUTH', AuthParameters={'USERNAME': email, 'PASSWORD': password}))

    def refresh(self, refresh_token):
        return self.tokens(self.call('get_tokens_from_refresh_token', ClientId=self.config.client_id, RefreshToken=refresh_token))

    def revoke(self, refresh_token):
        self.call('revoke_token', ClientId=self.config.client_id, Token=refresh_token)

    def register(self, email, password):
        self.call('sign_up', ClientId=self.config.client_id, Username=email, Password=password, UserAttributes=[{'Name': 'email', 'Value': email}])

    def verify(self, email, code):
        self.call('confirm_sign_up', ClientId=self.config.client_id, Username=email, ConfirmationCode=code)

    def resend(self, email):
        self.call('resend_confirmation_code', ClientId=self.config.client_id, Username=email)

    def forgot(self, email):
        self.call('forgot_password', ClientId=self.config.client_id, Username=email)

    def reset(self, email, code, password):
        self.call('confirm_forgot_password', ClientId=self.config.client_id, Username=email, ConfirmationCode=code, Password=password)

    def http_json(self, url, data=None):
        request = URLRequest(url, data=data, headers={'Accept': 'application/json', **({'Content-Type': 'application/x-www-form-urlencoded'} if data else {})})
        try:
            with self.http.open(request, timeout=5) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise AuthError(503, '认证服务返回无效响应。', 'provider_unavailable')
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError()
                return result
        except HTTPError as error:
            status = error.code
            error.close()
            if status in (400, 401, 403):
                raise AuthError(401, '登录凭证已失效，请重新登录。', 'invalid_credentials') from None
            if status == 429:
                raise AuthError(429, '认证服务繁忙，请稍后重试。', 'rate_limited') from None
            raise AuthError(503, '认证服务暂不可用。', 'provider_unavailable') from None
        except (URLError, OSError, ValueError):
            raise AuthError(503, '认证服务暂不可用。', 'provider_unavailable') from None

    def jwks(self):
        # At most two read attempts. Token exchanges and writes are never replayed.
        for attempt in range(2):
            try:
                return self.http_json(self.config.issuer + '/.well-known/jwks.json')
            except AuthError as error:
                if error.status != 503 or attempt:
                    raise

    def exchange(self, code, verifier):
        return self.http_json(self.config.domain + '/oauth2/token', urlencode({
            'grant_type': 'authorization_code', 'client_id': self.config.client_id,
            'redirect_uri': self.config.public_url + '/api/auth/callback',
            'code': code, 'code_verifier': verifier,
        }).encode())


class TokenValidator:
    def __init__(self, config, provider, clock):
        self.config, self.provider, self.clock = config, provider, clock
        self.keys, self.loaded = {}, 0.0
        self.lock = threading.Lock()

    def claims(self, token, use):
        try:
            if not isinstance(token, str) or not 1 <= len(token) <= 32768:
                raise ValueError()
            header = jwt.get_unverified_header(token)
            kid = header.get('kid')
            if header.get('alg') != 'RS256' or not isinstance(kid, str) or not 1 <= len(kid) <= 128:
                raise ValueError()
            with self.lock:
                now = self.clock()
                if not self.keys or now - self.loaded >= 3600 or (kid not in self.keys and now - self.loaded >= 30):
                    raw = self.provider().jwks()
                    if not isinstance(raw, dict) or not isinstance(raw.get('keys'), list) or not 1 <= len(raw['keys']) <= 20:
                        raise AuthError(503, '签名密钥暂不可用。', 'provider_unavailable')
                    keys = {}
                    try:
                        for item in raw['keys']:
                            if not isinstance(item, dict):
                                raise ValueError()
                            if item.get('kty') == 'RSA' and item.get('alg', 'RS256') == 'RS256' and item.get('use', 'sig') == 'sig' and isinstance(item.get('kid'), str):
                                keys[item['kid']] = jwt.PyJWK.from_dict(item, algorithm='RS256').key
                        if not keys:
                            raise ValueError()
                    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
                        raise AuthError(503, '签名密钥暂不可用。', 'provider_unavailable') from None
                    self.keys, self.loaded = keys, now
                key = self.keys.get(kid)
            if key is None:
                raise ValueError()
            required = ['iss', 'sub', 'exp', 'iat', 'token_use', 'aud' if use == 'id' else 'client_id']
            claims = jwt.decode(token, key, algorithms=['RS256'], issuer=self.config.issuer,
                audience=self.config.client_id if use == 'id' else None,
                options={'require': required, 'verify_aud': use == 'id', 'strict_aud': True,
                         'verify_exp': False, 'verify_iat': False, 'verify_nbf': False})
            # Explicit injected clock keeps all temporal checks consistent and testable.
            now = self.clock()
            if any(type(claims[name]) not in (int, float) or not math.isfinite(claims[name]) for name in ('exp', 'iat')):
                raise ValueError()
            if claims['exp'] <= now or claims['iat'] > now + 30 or claims['exp'] <= claims['iat']:
                raise ValueError()
            if 'nbf' in claims and (type(claims['nbf']) not in (int, float) or not math.isfinite(claims['nbf']) or claims['nbf'] > now + 30):
                raise ValueError()
            if claims['token_use'] != use or not isinstance(claims['sub'], str) or not claims['sub']:
                raise ValueError()
            if use == 'access' and claims['client_id'] != self.config.client_id:
                raise ValueError()
            return claims
        except AuthError:
            raise
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise AuthError(401, '登录凭证验证失败，请重新登录。', 'invalid_token') from None

    def pair(self, tokens, nonce=None):
        identity = self.claims(tokens.get('id_token'), 'id')
        access = self.claims(tokens.get('access_token'), 'access')
        if identity['sub'] != access['sub']:
            raise AuthError(401, '登录身份不一致。', 'invalid_token')
        if identity.get('email_verified') is not True:
            raise AuthError(403, '请先验证邮箱。', 'email_unverified')
        if nonce is not None and not equal_secret(identity.get('nonce'), nonce):
            raise AuthError(401, '登录验证已失效，请重新开始。', 'invalid_nonce')
        try:
            email = normalize_email(identity.get('email'))
        except AuthError:
            raise AuthError(401, '登录身份缺少有效邮箱。', 'invalid_token') from None
        name = identity.get('name')
        if not isinstance(name, str) or not name.strip():
            name = email.split('@', 1)[0]
        user = {'id': hashed(self.config.issuer + '\0' + identity['sub']), 'email': email, 'name': name[:100]}
        return user, min(identity['exp'], access['exp'])


class RateLimiter:
    def __init__(self, clock, maximum=4096):
        self.clock, self.maximum, self.entries = clock, maximum, OrderedDict()
        self.lock = threading.Lock()

    def check(self, ip, action, email='', *, include_ip=True):
        now = self.clock()
        keys = [('ip:' + hashed(ip), 60, 60)] if include_ip else []
        if email:
            keys.append(('email:' + action + ':' + hashed(email), 600, 5))
        with self.lock:
            for key in list(self.entries):
                if self.entries[key][0] <= now:
                    del self.entries[key]
            for key, window, limit in keys:
                if key not in self.entries:
                    if len(self.entries) >= self.maximum:
                        raise AuthError(429, '请求过于频繁，请稍后重试。', 'rate_limited')
                    self.entries[key] = [now + window, 0]
                if self.entries[key][1] >= limit:
                    raise AuthError(429, '请求过于频繁，请稍后重试。', 'rate_limited')
            for key, _, _ in keys:
                self.entries[key][1] += 1


class AuthService:
    def __init__(self, data_root: Path, *, config=None, provider=None, clock=time.time):
        self.config = config if config is not None else AuthConfig.from_env()
        self.clock, self._provider = clock, provider
        self.configured = self.config is not None
        self.fernet = Fernet(self.config.session_key.encode()) if self.config else None
        self.validator = TokenValidator(self.config, self.provider, clock) if self.config else None
        self.cookie_name = '__Host-memoverse_session' if self.config and self.config.secure else 'memoverse_session'
        self.oauth_cookie = '__Host-memoverse_oauth' if self.config and self.config.secure else 'memoverse_oauth'
        self.limiter = RateLimiter(clock)
        self.locks = [threading.Lock() for _ in range(64)]
        self._revocation_lock = threading.Lock()
        self._revocation_subscribers = set()
        data_root = Path(data_root)
        data_root.mkdir(parents=True, exist_ok=True)
        self.database = data_root / 'auth.sqlite3'
        with self.db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS sessions (
                    sid_hash TEXT PRIMARY KEY, user_json TEXT NOT NULL, tokens BLOB NOT NULL,
                    csrf TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                    refresh_at REAL NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    refresh_owner TEXT, refresh_until REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS oauth (
                    state_hash TEXT PRIMARY KEY, binding_hash TEXT NOT NULL,
                    payload BLOB NOT NULL, expires REAL NOT NULL
                );
            ''')
        if os.name != 'nt':
            self.database.chmod(0o600)
        from .account_auth import AccountAuth
        self.account_auth = AccountAuth(self)

    def db(self):
        connection = sqlite3.connect(self.database, timeout=3, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return closing(connection)

    def require_config(self):
        if not self.configured:
            raise AuthError(503, '登录服务尚未配置完成。', 'auth_not_configured')

    def provider(self):
        self.require_config()
        if self._provider is None:
            self._provider = CognitoProvider(self.config)
        return self._provider

    def public_state(self, session=None):
        return {'authenticated': session is not None, 'configured': self.configured,
                'providers': {'email': self.configured, 'google': bool(self.config and self.config.google_enabled)},
                **({'user': session['user'], 'csrfToken': session['csrf']} if session else {})}

    def cookie(self, response, name, value, seconds):
        response.set_cookie(name, value, max_age=max(0, int(seconds)), httponly=True,
                            secure=bool(self.config and self.config.secure), samesite='lax', path='/')

    def clear_cookie(self, response, name):
        response.delete_cookie(name, path='/', httponly=True, secure=bool(self.config and self.config.secure), samesite='lax')

    def check_write(self, request, session=None):
        self.require_config()
        if request.headers.get('origin') != self.config.public_url or request.headers.get('x-memoverse-client') not in ('web', 'android'):
            raise AuthError(403, '请求来源验证失败。', 'invalid_origin')
        if session and not equal_secret(request.headers.get('x-csrf-token', ''), session['csrf']):
            raise AuthError(403, '页面验证已失效，请刷新后重试。', 'invalid_csrf')

    def encrypt(self, tokens):
        return self.fernet.encrypt(json.dumps(tokens, separators=(',', ':')).encode())

    def decrypt(self, value):
        try:
            result = json.loads(self.fernet.decrypt(value))
            if not isinstance(result, dict):
                raise ValueError()
            return result
        except (InvalidToken, ValueError, TypeError):
            raise AuthError(401, '登录状态已失效，请重新登录。', 'invalid_session') from None

    def login(self, email, password):
        return self.create_session(self.provider().login(email, password))

    def validate_login_tokens(self, tokens, nonce=None):
        self.require_config()
        if not isinstance(tokens, dict):
            raise AuthError(401, '认证服务未提供完整登录状态。', 'invalid_token')
        user, expiry = self.validator.pair(tokens, nonce)
        if not isinstance(tokens.get('refresh_token'), str) or not 1 <= len(tokens['refresh_token']) <= 65536:
            raise AuthError(401, '认证服务未提供完整登录状态。', 'invalid_token')
        return user, expiry

    def prepare_session(self, tokens, nonce=None):
        """Validate outside SQL locks; a caller can atomically insert the prepared session."""
        user, expiry = self.validate_login_tokens(tokens, nonce)
        user = self.account_auth.resolve(user, tokens)
        sid, csrf, now = secrets.token_urlsafe(32), secrets.token_urlsafe(32), self.clock()
        session = {'sid': sid, 'user': user, 'csrf': csrf, 'expires': now + SESSION_SECONDS}
        values = (hashed(sid), json.dumps(user), self.encrypt(tokens), csrf, now, session['expires'], min(expiry - 30, now + 900))
        return session, values

    @staticmethod
    def insert_session(db, values):
        db.execute('DELETE FROM sessions WHERE expires<=?', (values[4],))
        db.execute('INSERT INTO sessions(sid_hash,user_json,tokens,csrf,created,expires,refresh_at) VALUES (?,?,?,?,?,?,?)', values)

    def create_session(self, tokens, nonce=None):
        session, values = self.prepare_session(tokens, nonce)
        with self.db() as db:
            self.insert_session(db, values)
        return session

    def invalidate(self, sid):
        if sid:
            self._invalidate_digest(hashed(sid))

    def _invalidate_digest(self, digest):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE sid_hash=?', (digest,))
        self._notify_revoked(digest)

    def subscribe_revocations(self, callback):
        """The callback receives only a session digest and must enqueue without I/O."""
        with self._revocation_lock:
            self._revocation_subscribers.add(callback)

        def unsubscribe():
            with self._revocation_lock:
                self._revocation_subscribers.discard(callback)
        return unsubscribe

    def _notify_revoked(self, session_key):
        with self._revocation_lock:
            callbacks = tuple(self._revocation_subscribers)
        for callback in callbacks:
            try:
                callback(session_key)
            except Exception:
                logging.getLogger(__name__).warning('Session revocation notification deferred')

    def websocket_session(self, sid, origin):
        """Browser WS uses the same opaque cookie and existing refresh semantics."""
        self.require_config()
        if origin != self.config.public_url:
            raise AuthError(403, '请求来源验证失败。', 'invalid_origin')
        session = self.session(sid)
        if session is None:
            raise AuthError(401, '请先登录。', 'authentication_required')
        return {'user': session['user'], 'sessionKey': hashed(session['sid']),
                'expiresAt': int(session['expires'] * 1000)}

    def session(self, sid):
        if not self.configured or not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', sid):
            return None
        return self._session_for_digest(hashed(sid), sid)

    def websocket_session_key(self, session_key):
        """Server-internal peer authorization; no route accepts a session digest."""
        self.require_config()
        if not isinstance(session_key, str) or not re.fullmatch(r'[a-f0-9]{64}', session_key):
            raise AuthError(401, '请先登录。', 'authentication_required')
        session = self._session_for_digest(session_key)
        if session is None:
            raise AuthError(401, '请先登录。', 'authentication_required')
        return {'user': session['user'], 'sessionKey': session_key,
                'expiresAt': int(session['expires'] * 1000)}

    def _session_for_digest(self, digest, sid=None):
        with self.locks[int(digest[:4], 16) % len(self.locks)]:
            with self.db() as db:
                row = db.execute('SELECT * FROM sessions WHERE sid_hash=?', (digest,)).fetchone()
            if row is None:
                return None
            if row['expires'] <= self.clock():
                self._invalidate_digest(digest)
                return None
            owner = None
            try:
                tokens = self.decrypt(row['tokens'])
                if row['refresh_at'] <= self.clock():
                    owner = secrets.token_urlsafe(16)
                    with self.db() as db:
                        claimed = db.execute('UPDATE sessions SET refresh_owner=?,refresh_until=? WHERE sid_hash=? AND version=? AND refresh_until<=?',
                            (owner, self.clock() + 30, digest, row['version'], self.clock())).rowcount
                    if not claimed:
                        raise AuthError(503, '登录状态正在更新，请稍后重试。', 'refresh_in_progress')
                    updated = self.provider().refresh(tokens['refresh_token'])
                    if not updated.get('refresh_token'):
                        updated['refresh_token'] = tokens['refresh_token']
                    user, expiry = self.validator.pair(updated)
                    user = self.account_auth.resolve(user, updated)
                    if user['id'] != json.loads(row['user_json'])['id']:
                        raise AuthError(401, '刷新后的登录身份不一致。', 'invalid_token')
                    with self.db() as db:
                        changed = db.execute('UPDATE sessions SET tokens=?,user_json=?,refresh_at=?,version=version+1,refresh_owner=NULL,refresh_until=0 WHERE sid_hash=? AND version=? AND refresh_owner=?',
                            (self.encrypt(updated), json.dumps(user), min(expiry - 30, self.clock() + 900), digest, row['version'], owner)).rowcount
                    if not changed:
                        raise AuthError(401, '登录状态已退出。', 'invalid_session')
                    tokens = updated
                user, _ = self.validator.pair(tokens)
                user = self.account_auth.resolve(user, tokens)
                return {'sid': sid, 'user': user, 'csrf': row['csrf'], 'expires': row['expires']}
            except AuthError as error:
                if error.status in (401, 403):
                    self._invalidate_digest(digest)
                raise
            finally:
                if owner:
                    with self.db() as db:
                        db.execute('UPDATE sessions SET refresh_owner=NULL,refresh_until=0 WHERE sid_hash=? AND refresh_owner=?', (digest, owner))

    def logout(self, sid):
        tokens = None
        with self.db() as db:
            row = db.execute('SELECT tokens FROM sessions WHERE sid_hash=?', (hashed(sid),)).fetchone() if sid else None
            if sid:
                db.execute('DELETE FROM sessions WHERE sid_hash=?', (hashed(sid),))
        if sid:
            self._notify_revoked(hashed(sid))
        if row:
            try:
                tokens = self.decrypt(row['tokens'])
                self.provider().revoke(tokens['refresh_token'])
            except AuthError:
                pass  # Local invalidation is authoritative even during provider outages.
        return {'ok': True, **({'logoutUrl': self.config.domain + '/logout?' + urlencode({'client_id': self.config.client_id, 'logout_uri': self.config.public_url + '/'})} if self.config else {})}

    def google_attempt(self, *, purpose='web', expires=None):
        """Build one browser-bound OAuth attempt without opening a write transaction."""
        self.require_config()
        if not self.config.google_enabled:
            raise AuthError(503, 'Google 登录尚未配置完成。', 'provider_disabled')
        state, binding, nonce, verifier = [secrets.token_urlsafe(32) for _ in range(4)]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        if purpose not in ('web', 'android', 'account'):
            raise ValueError('Unsupported OAuth purpose')
        expiry = min(self.clock() + OAUTH_SECONDS, expires) if expires is not None else self.clock() + OAUTH_SECONDS
        payload = self.encrypt({'verifier': verifier, 'nonce': nonce, **({'purpose': purpose} if purpose != 'web' else {})})
        url = self.config.domain + '/oauth2/authorize?' + urlencode({'response_type': 'code', 'client_id': self.config.client_id,
            'redirect_uri': self.config.public_url + '/api/auth/callback', 'scope': 'openid email profile',
            'identity_provider': 'Google', 'state': state, 'nonce': nonce, 'code_challenge_method': 'S256', 'code_challenge': challenge})
        return {'url': url, 'state': state, 'binding': binding, 'payload': payload, 'expires': expiry}

    def start_google(self):
        attempt = self.google_attempt()
        with self.db() as db:
            db.execute('DELETE FROM oauth WHERE expires<=?', (self.clock(),))
            db.execute('INSERT INTO oauth VALUES (?,?,?,?)',
                       (hashed(attempt['state']), hashed(attempt['binding']), attempt['payload'], attempt['expires']))
        return attempt['url'], attempt['binding']

    def consume_google_attempt(self, state, binding, *, purpose='web'):
        self.require_config()
        if not self.config.google_enabled or any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', value) for value in (state, binding)):
            raise AuthError(400, '登录请求已过期或不属于此浏览器。', 'invalid_state')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM oauth WHERE state_hash=?', (hashed(state),)).fetchone()
            if row is None or row['expires'] <= self.clock() or not equal_secret(row['binding_hash'], hashed(binding)):
                db.rollback()
                raise AuthError(400, '登录请求已过期或不属于此浏览器。', 'invalid_state')
            payload = self.decrypt(row['payload'])
            if (payload.get('purpose', 'web') != purpose
                    or set(payload) != ({'verifier', 'nonce'} if purpose == 'web' else {'verifier', 'nonce', 'purpose'})
                    or any(not isinstance(payload.get(key), str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', payload[key]) for key in ('verifier', 'nonce'))):
                db.rollback()
                raise AuthError(400, '登录请求用途不一致。', 'invalid_state')
            db.execute('DELETE FROM oauth WHERE state_hash=?', (hashed(state),))
            db.commit()
        return payload

    def exchange_google_attempt(self, payload, code):
        if not isinstance(code, str) or not 1 <= len(code) <= 4096:
            raise AuthError(400, '登录未完成，请重新开始。', 'invalid_code')
        return self.provider().exchange(code, payload['verifier'])

    def callback(self, state, binding, code):
        payload = self.consume_google_attempt(state, binding)
        tokens = self.exchange_google_attempt(payload, code)
        return self.create_session(tokens, payload['nonce'])


class AuthAccessLogFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple) and len(record.args) == 5:
            args = list(record.args)
            if isinstance(args[2], str) and args[2].startswith('/api/auth/'):
                args[2] = args[2].split('?', 1)[0]
                record.args = tuple(args)
        return True


def install_auth(app: FastAPI, data_root: Path, on_authenticated=None, *, config=None, provider=None, clock=time.time):
    service = AuthService(data_root, config=config, provider=provider, clock=clock)
    logging.getLogger('uvicorn.access').addFilter(AuthAccessLogFilter())

    def failure(error):
        if isinstance(error, sqlite3.Error):
            error = AuthError(503, '登录状态暂时无法读取，请稍后重试。', 'session_unavailable')
        return JSONResponse({'detail': error.detail, 'code': error.code}, status_code=error.status, headers={'Cache-Control': 'no-store'})

    @app.exception_handler(sqlite3.Error)
    async def database_error(request: Request, error):
        return failure(error)

    async def initialize(session):
        if on_authenticated:
            try:
                await run_in_threadpool(on_authenticated, session['user'])
            except Exception:
                raise AuthError(503, '账户工作区暂时无法打开，请稍后重试。', 'workspace_unavailable') from None

    @app.middleware('http')
    async def authentication(request: Request, call_next):
        path = request.url.path
        if not path.startswith('/api') or path == '/api/health' or path.startswith('/api/auth/'):
            response = await call_next(request)
        else:
            try:
                session = await run_in_threadpool(service.session, request.cookies.get(service.cookie_name))
                if session is None:
                    raise AuthError(401, '请先登录。', 'authentication_required')
                if request.method in WRITE_METHODS:
                    service.check_write(request, session)
                await initialize(session)
                request.state.user = session['user']
                response = await call_next(request)
            except (AuthError, sqlite3.Error) as error:
                response = failure(error)
                if isinstance(error, AuthError) and error.status == 401:
                    service.clear_cookie(response, service.cookie_name)
        if path.startswith('/api'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.get('/api/auth/session')
    async def session_route(request: Request):
        try:
            session = await run_in_threadpool(service.session, request.cookies.get(service.cookie_name))
            if session:
                await initialize(session)
            response = JSONResponse(service.public_state(session))
            if not session:
                service.clear_cookie(response, service.cookie_name)
            return response
        except AuthError as error:
            if error.status in (401, 403):
                response = JSONResponse(service.public_state())
                service.clear_cookie(response, service.cookie_name)
                return response
            return failure(error)

    async def payload(request, allowed):
        service.check_write(request)
        service.limiter.check(request.client.host if request.client else '', 'auth-write')
        try:
            raw = bytearray()
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 4096:
                    raise ValueError()
            data = json.loads(raw)
            if not isinstance(data, dict) or set(data) != set(allowed):
                raise ValueError()
            return data
        except (ValueError, TypeError):
            raise AuthError(422, '认证信息格式不正确。', 'invalid_request') from None

    async def login_response(session, request, redirect=False):
        try:
            await initialize(session)
        except AuthError:
            await run_in_threadpool(service.invalidate, session['sid'])
            raise
        old = request.cookies.get(service.cookie_name)
        if old:
            await run_in_threadpool(service.invalidate, old)
        response = RedirectResponse(service.config.public_url + '/', status_code=303) if redirect else JSONResponse(service.public_state(session))
        service.cookie(response, service.cookie_name, session['sid'], session['expires'] - service.clock())
        return response

    from .native_auth import install_native_auth
    native = install_native_auth(app, service, login_response, failure)
    from .account_auth import install_account_auth
    install_account_auth(app, service, failure)

    @app.post('/api/auth/login')
    async def login_route(request: Request):
        try:
            body = await payload(request, ('email', 'password'))
            email, password = normalize_email(body['email']), password_value(body['password'])
            service.limiter.check(request.client.host if request.client else '', 'login', email, include_ip=False)
            session = await run_in_threadpool(service.login, email, password)
            return await login_response(session, request)
        except AuthError as error:
            return failure(error)

    async def email_action(request, action, fields):
        try:
            body = await payload(request, fields)
            email = normalize_email(body['email'])
            service.limiter.check(request.client.host if request.client else '', action, email, include_ip=False)
            arguments = [email]
            if 'code' in fields:
                arguments.append(code_value(body['code']))
            if 'password' in fields:
                arguments.append(password_value(body['password']))
            try:
                await run_in_threadpool(lambda: getattr(service.provider(), action)(*arguments))
            except AuthError as error:
                if action not in ('forgot', 'resend') or error.status not in (400, 401, 403):
                    raise
            if action == 'register':
                return JSONResponse({'verificationRequired': True, 'email': email})
            if action == 'verify':
                return JSONResponse({'verified': True})
            return JSONResponse({'ok': True})
        except AuthError as error:
            return failure(error)

    @app.post('/api/auth/register')
    async def register_route(request: Request):
        return await email_action(request, 'register', ('email', 'password'))

    @app.post('/api/auth/verify-email')
    async def verify_route(request: Request):
        return await email_action(request, 'verify', ('email', 'code'))

    @app.post('/api/auth/resend-code')
    async def resend_route(request: Request):
        return await email_action(request, 'resend', ('email',))

    @app.post('/api/auth/forgot-password')
    async def forgot_route(request: Request):
        return await email_action(request, 'forgot', ('email',))

    @app.post('/api/auth/reset-password')
    async def reset_route(request: Request):
        return await email_action(request, 'reset', ('email', 'code', 'password'))

    @app.post('/api/auth/logout')
    async def logout_route(request: Request):
        try:
            service.check_write(request)
            service.limiter.check(request.client.host if request.client else '', 'logout')
            sid = request.cookies.get(service.cookie_name)
            # No refresh/network dependency before local invalidation.
            with service.db() as db:
                row = db.execute('SELECT csrf FROM sessions WHERE sid_hash=?', (hashed(sid),)).fetchone() if sid else None
            if row:
                service.check_write(request, {'csrf': row['csrf']})
            result = await run_in_threadpool(service.logout, sid)
            # Only the request's sid was revoked. A later login or OAuth start
            # in another tab may already have replaced either shared cookie.
            # Do not let this delayed response clear those newer cookies; the
            # old opaque sid is invalid until expiry or the next login replaces it.
            return JSONResponse(result)
        except AuthError as error:
            return failure(error)

    @app.get('/api/auth/google')
    async def google_route(request: Request):
        try:
            service.limiter.check(request.client.host if request.client else '', 'google')
            url, binding = await run_in_threadpool(service.start_google)
            response = RedirectResponse(url, status_code=302)
            service.cookie(response, service.oauth_cookie, binding, OAUTH_SECONDS)
            return response
        except AuthError as error:
            return failure(error)

    @app.get('/api/auth/callback')
    async def callback_route(request: Request):
        state = request.query_params.get('state')
        try:
            account_state = await run_in_threadpool(service.account_auth.owns_state, state)
        except sqlite3.Error:
            return failure(AuthError(503, '登录方式暂时无法更新，请稍后重试。', 'session_unavailable'))
        if account_state:
            return await service.account_auth.callback(request)
        try:
            native_state = await run_in_threadpool(native.owns_state, state)
        except sqlite3.Error:
            origin = service.config.public_url if service.config else ''
            return RedirectResponse(origin + '/?auth_error=google_login_failed', status_code=303,
                                    headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        if native_state:
            try:
                service.limiter.check(request.client.host if request.client else '', 'callback')
                if (len(request.query_params.getlist('state')) != 1 or len(request.query_params.getlist('code')) > 1
                        or len(request.query_params.getlist('error')) > 1
                        or 'code' in request.query_params and 'error' in request.query_params):
                    raise AuthError(400, '登录交接参数格式不正确。', 'invalid_request')
                url, clear_binding = await run_in_threadpool(native.complete_browser, state,
                    request.cookies.get(native.cookie_name), request.query_params.get('code'), request.query_params.get('error'))
                response = RedirectResponse(url, status_code=303,
                    headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
                if clear_binding:
                    service.clear_cookie(response, native.cookie_name)
                return response
            except (AuthError, sqlite3.Error):
                return RedirectResponse(service.config.public_url + '/?auth_error=google_login_failed', status_code=303,
                                        headers={'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer'})
        try:
            service.limiter.check(request.client.host if request.client else '', 'callback')
            session = await run_in_threadpool(service.callback, state, request.cookies.get(service.oauth_cookie), request.query_params.get('code'))
            response = await login_response(session, request, redirect=True)
        except (AuthError, sqlite3.Error):
            # Never return provider details or the OAuth query to the browser.
            origin = service.config.public_url if service.config else ''
            response = RedirectResponse(origin + '/?auth_error=google_login_failed', status_code=303)
        service.clear_cookie(response, service.oauth_cookie)
        return response

    return service

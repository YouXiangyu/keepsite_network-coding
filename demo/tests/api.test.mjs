import { test, after, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { signedSession, serverState } from './web-fixtures.mjs';

const apiUrl = new URL('../src/api.ts', import.meta.url).href;
const hook = registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL === apiUrl && specifier === './data') return { url: new URL('../src/data.ts', import.meta.url).href, shortCircuit: true };
  return nextResolve(specifier, context);
} });
const api = await import(apiUrl);
hook.deregister();
const originalFetch = globalThis.fetch;
const origin = 'https://memoverse.example';
const logoutUrl = 'https://memoverse.auth.ap-northeast-3.amazoncognito.com/logout?'
  + new URLSearchParams({ client_id: 'testclient123', logout_uri: origin + '/' });
afterEach(() => { api.installSession(null); globalThis.fetch = originalFetch; });
after(() => { globalThis.fetch = originalFetch; });

test('anonymous code cannot request workspace, upload, model or mutations', async () => {
  let requests = 0;
  globalThis.fetch = async () => { requests += 1; throw new Error('unexpected fetch'); };
  await assert.rejects(api.getState(), error => error.status === 401);
  await assert.rejects(api.mutate('/api/collections', 'POST', {}), error => error.status === 401);
  await assert.rejects(api.uploadMedia(new File(['x'], 'image.png', { type: 'image/png' })), error => error.status === 401);
  await assert.rejects(api.fetchModel('/api/models/model/model.glb', new AbortController().signal), error => error.status === 401);
  assert.equal(requests, 0);
});

test('session lookup is same-origin and never initializes account data', async () => {
  const requests = [];
  globalThis.fetch = async (path, options) => {
    requests.push(path);
    assert.equal(options.credentials, 'same-origin');
    return Response.json({ authenticated: false, configured: true, providers: { google: false, email: true } });
  };
  assert.equal((await api.getSession()).authenticated, false);
  assert.deepEqual(requests, ['/api/auth/session']);
});

test('invalid session identity cannot retain a previously active account', async () => {
  api.installSession(signedSession('a'));
  assert.throws(() => api.installSession({ ...signedSession('b'), user: { id: 42, email: 'b@example.test', name: 'B' } }), error => error.status === 401);
  await assert.rejects(api.getState(), error => error.status === 401);
});

test('login preserves password bytes and uses the Web client header', async () => {
  const password = '  Aa123456789!  ';
  globalThis.fetch = async (path, options) => {
    assert.equal(path, '/api/auth/login');
    assert.equal(options.headers.get('X-MemoVerse-Client'), 'web');
    assert.equal(options.credentials, 'same-origin');
    assert.equal(JSON.parse(options.body).password, password);
    return Response.json(signedSession());
  };
  assert.equal((await api.signIn('a@example.test', password)).user.id, 'a');
});

test('unverified email retains actionable error code', async () => {
  globalThis.fetch = async () => Response.json({ detail: '请验证邮箱', code: 'email_unverified' }, { status: 403 });
  await assert.rejects(api.signIn('a@example.test', 'A-password-12!'), error => error.status === 403 && error.code === 'email_unverified');
});

test('email codes are passed verbatim without length assumptions', async () => {
  const code = ' long-code-AB12 ';
  globalThis.fetch = async (path, options) => {
    assert.equal(JSON.parse(options.body).code, code);
    assert.equal(options.headers.get('X-MemoVerse-Client'), 'web');
    return Response.json(path.endsWith('verify-email') ? { verified: true } : { ok: true });
  };
  assert.equal((await api.verifyEmail('a@example.test', code)).verified, true);
  await api.resetPassword('a@example.test', code, 'A-password-12!');
});

test('authenticated writes attach the active CSRF token and preserve versions', async () => {
  api.installSession(signedSession());
  globalThis.fetch = async (path, options) => {
    assert.equal(path, '/api/collections/item-1');
    assert.equal(options.headers.get('X-MemoVerse-Client'), 'web');
    assert.equal(options.headers.get('X-CSRF-Token'), 'test-csrf-a');
    assert.equal(options.credentials, 'same-origin');
    assert.deepEqual(JSON.parse(options.body), { version: 2, title: '保留草稿' });
    return Response.json(serverState());
  };
  assert.equal((await api.mutate('/api/collections/item-1', 'PATCH', { version: 2, title: '保留草稿' })).revision, 7);
});

test('a state from another user invalidates the current session', async () => {
  api.installSession(signedSession());
  let expired = 0;
  const unsubscribe = api.onSessionExpired(() => { expired += 1; });
  globalThis.fetch = async () => Response.json(serverState('b'));
  await assert.rejects(api.getState(), error => error.status === 401);
  await assert.rejects(api.getState(), error => error.status === 401);
  assert.equal(expired, 1);
  unsubscribe();
});

test('an old account response cannot become the new account state even if fetch ignores abort', async () => {
  api.installSession(signedSession('a'));
  let resolve;
  let signal;
  globalThis.fetch = (_path, options) => { signal = options.signal; return new Promise(done => { resolve = done; }); };
  const pending = api.getState();
  api.installSession(signedSession('b'));
  assert.equal(signal.aborted, true);
  resolve(Response.json(serverState('a')));
  await assert.rejects(pending, error => error.name === 'AbortError');
  globalThis.fetch = async () => Response.json(serverState('b'));
  assert.equal((await api.getState()).userId, 'b');
});

test('a late unauthorized response must not log out the new account', async () => {
  api.installSession(signedSession('a'));
  let resolve;
  let expired = 0;
  const unsubscribe = api.onSessionExpired(() => { expired += 1; });
  globalThis.fetch = () => new Promise(done => { resolve = done; });
  const pending = api.getState();
  api.installSession(signedSession('b'));
  resolve(Response.json({ detail: 'expired' }, { status: 401 }));
  await assert.rejects(pending, error => error.name === 'AbortError');
  assert.equal(expired, 0);
  unsubscribe();
});

test('a completed upload cannot continue as a create operation in a replacement session', () => {
  api.installSession(signedSession('a'));
  const submittingScope = api.sessionEpoch();
  api.installSession(signedSession('b'));
  assert.throws(() => api.assertSessionEpoch(submittingScope), error => error.name === 'AbortError');
});

test('switching account while decoding a body also discards the old response', async () => {
  api.installSession(signedSession('a'));
  let resolveBody;
  let started;
  const bodyStarted = new Promise(resolve => { started = resolve; });
  globalThis.fetch = async () => ({ ok: true, json: () => { started(); return new Promise(resolve => { resolveBody = resolve; }); } });
  const pending = api.getState();
  await bodyStarted;
  api.installSession(signedSession('b'));
  resolveBody(serverState('a'));
  await assert.rejects(pending, error => error.name === 'AbortError');
});

test('model 401 expires the session instead of leaving private business UI authenticated', async () => {
  api.installSession(signedSession());
  let expired = false;
  const unsubscribe = api.onSessionExpired(() => { expired = true; });
  globalThis.fetch = async () => Response.json({ detail: '登录已过期' }, { status: 401 });
  await assert.rejects(api.fetchModel('/api/models/model/model.glb', new AbortController().signal), error => error.status === 401);
  assert.equal(expired, true);
  unsubscribe();
});

test('logout aborts pending model loading and clears the account scope', async () => {
  api.installSession(signedSession());
  let resolveModel;
  let modelSignal;
  globalThis.fetch = async (path, options) => {
    if (path === '/api/auth/logout') {
      assert.equal(options.headers.get('X-CSRF-Token'), 'test-csrf-a');
      return Response.json({ ok: true });
    }
    modelSignal = options.signal;
    return new Promise(resolve => { resolveModel = resolve; });
  };
  const loading = api.fetchModel('/api/models/model/model.glb', new AbortController().signal);
  await api.signOut(origin);
  assert.equal(modelSignal.aborted, true);
  resolveModel(new Response(new Uint8Array([1, 2, 3])));
  await assert.rejects(loading, error => error.name === 'AbortError');
  await assert.rejects(api.getState(), error => error.status === 401);
});

test('explicit logout forwards only the validated Cognito destination after clearing the local session', async () => {
  api.installSession(signedSession());
  let ended;
  const unsubscribe = api.onSessionExpired(event => { ended = event; });
  try {
    globalThis.fetch = async () => Response.json({ ok: true, logoutUrl });
    await api.signOut(origin);
    assert.deepEqual(ended, { reason: 'logout', logoutUrl });
    await assert.rejects(api.getState(), error => error.status === 401);
  } finally { unsubscribe(); }
});

test('logout rejects unsafe provider and return URLs while still clearing the local session', async () => {
  const unsafe = [
    'javascript:alert(1)', logoutUrl.replace('https:', 'http:'),
    logoutUrl.replace('memoverse.auth.ap-northeast-3.amazoncognito.com', 'evil.example'),
    logoutUrl.replace('.amazoncognito.com', '.amazoncognito.com.evil.example'),
    logoutUrl.replace('https://', 'https://user:password@'),
    logoutUrl.replace('.com/logout', '.com:444/logout'), logoutUrl.replace('/logout?', '/oauth2/authorize?'),
    logoutUrl.replace(encodeURIComponent(origin + '/'), encodeURIComponent('https://evil.example/')),
    logoutUrl.replace(encodeURIComponent(origin + '/'), encodeURIComponent(origin + '/redirect')),
    logoutUrl.replace(encodeURIComponent(origin + '/'), encodeURIComponent(origin + '/?next=https://evil.example')),
    logoutUrl.replace('logout_uri=', 'redirect_uri='), logoutUrl + '&logout_uri=https%3A%2F%2Fevil.example%2F',
    logoutUrl + '&redirect_uri=https%3A%2F%2Fevil.example%2F', logoutUrl + '#fragment',
    logoutUrl.replace('client_id=testclient123&', ''), logoutUrl.replace('testclient123', ''),
    logoutUrl.replace('https://', '//'), 42, null, undefined,
  ];
  let ended;
  const unsubscribe = api.onSessionExpired(event => { ended = event; });
  try {
    for (const value of unsafe) {
      api.installSession(signedSession());
      ended = undefined;
      globalThis.fetch = async () => Response.json({ ok: true, logoutUrl: value });
      await api.signOut(origin);
      assert.equal(ended?.reason, 'logout');
      assert.equal(ended?.logoutUrl, undefined, String(value));
      await assert.rejects(api.getState(), error => error.status === 401);
    }
  } finally { unsubscribe(); }
});

test('a delayed logout response cannot clear or redirect a replacement account', async () => {
  api.installSession(signedSession('a'));
  let resolveBody;
  let started;
  let ended = 0;
  const bodyStarted = new Promise(resolve => { started = resolve; });
  const unsubscribe = api.onSessionExpired(() => { ended += 1; });
  try {
    globalThis.fetch = async () => ({ ok: true, json: () => { started(); return new Promise(resolve => { resolveBody = resolve; }); } });
    const pending = api.signOut(origin);
    await bodyStarted;
    api.installSession(signedSession('b'));
    resolveBody({ ok: true, logoutUrl });
    await assert.rejects(pending, error => error.name === 'AbortError');
    assert.equal(ended, 0);
    globalThis.fetch = async () => Response.json(serverState('b'));
    assert.equal((await api.getState()).userId, 'b');
  } finally { unsubscribe(); }
});

test('multipart uploads keep original bytes and authenticated CSRF', async () => {
  api.installSession(signedSession());
  const file = new File([new Uint8Array([137, 80, 78, 71])], 'memory.png', { type: 'image/png' });
  globalThis.fetch = async (path, options) => {
    assert.equal(path, '/api/media');
    assert.ok(options.body instanceof FormData);
    assert.equal(options.body.get('file'), file);
    assert.equal(options.headers.get('Content-Type'), null);
    assert.equal(options.headers.get('X-CSRF-Token'), 'test-csrf-a');
    return Response.json({ assetId: 'asset-1', url: '/api/media/asset-1', sha256: 'hash', size: 4, width: 1, height: 1, mimeType: 'image/png' });
  };
  assert.equal((await api.uploadMedia(file)).url, '/api/media/asset-1');
});

test('conflict and network errors do not turn into fake successful state', async () => {
  api.installSession(signedSession());
  globalThis.fetch = async () => Response.json({ detail: '版本已更新' }, { status: 409 });
  await assert.rejects(api.mutate('/api/collections/item-1', 'PATCH', {}), error => error.status === 409 && error.message === '版本已更新');
  globalThis.fetch = async () => { throw new TypeError('network failed'); };
  await assert.rejects(api.getState(), /暂时无法连接服务/);
});

for (const [label, consume, start, limit] of [
  ['JSON save', 'json', () => api.mutate('/api/collections/item-1', 'PATCH', { version: 1 }), 30000],
  ['backup', 'blob', () => api.downloadBackup(), 120000],
  ['model', 'arrayBuffer', () => api.fetchModel('/api/models/model/model.glb', new AbortController().signal), 30000],
]) {
  test(`${label} expires even when successful response body ignores cancellation`, async context => {
    context.mock.timers.enable({ apis: ['setTimeout'] });
    api.installSession(signedSession());
    let bodyStarted;
    const consuming = new Promise(resolve => { bodyStarted = resolve; });
    let signal;
    globalThis.fetch = async (_path, options) => {
      signal = options.signal;
      return { ok: true, [consume]: () => { bodyStarted(); return new Promise(() => {}); } };
    };
    const request = start();
    const rejected = assert.rejects(request, error => error instanceof api.ApiError && /请求超时/.test(error.message));
    await consuming;
    context.mock.timers.tick(limit);
    await rejected;
    assert.equal(signal.aborted, true);
    globalThis.fetch = async () => Response.json(serverState());
    assert.equal((await api.getState()).revision, 7);
  });
}

test('expired error-body consumption cannot later invalidate a new session', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  api.installSession(signedSession('a'));
  let resolveBody;
  let bodyStarted;
  const consuming = new Promise(resolve => { bodyStarted = resolve; });
  globalThis.fetch = async () => ({ ok: false, status: 401, json: () => {
    bodyStarted();
    return new Promise(resolve => { resolveBody = resolve; });
  } });
  const request = api.getState();
  const rejected = assert.rejects(request, /请求超时/);
  await consuming;
  context.mock.timers.tick(30000);
  await rejected;
  api.installSession(signedSession('b'));
  resolveBody({ detail: 'expired' });
  await Promise.resolve();
  globalThis.fetch = async () => Response.json(serverState('b'));
  assert.equal((await api.getState()).userId, 'b');
});

test('account cancellation promptly rejects a body that never finishes', async () => {
  api.installSession(signedSession('a'));
  let bodyStarted;
  const consuming = new Promise(resolve => { bodyStarted = resolve; });
  globalThis.fetch = async () => ({ ok: true, json: () => { bodyStarted(); return new Promise(() => {}); } });
  const request = api.getState();
  const rejected = assert.rejects(request, error => error.name === 'AbortError');
  await consuming;
  api.installSession(signedSession('b'));
  await rejected;
});

test('malformed generation recovery metadata cannot silently hide a recovery action', async () => {
  api.installSession(signedSession());
  globalThis.fetch = async () => Response.json({ ...serverState(), generationRecovery: [{ jobId: 'j', error: 'failed' }] });
  await assert.rejects(api.getState(), /生成恢复状态不完整/);
});

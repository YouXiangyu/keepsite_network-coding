import { test, after, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { signedSession, snapshotPage, media, png } from './web-fixtures.mjs';

const apiUrl = new URL('../src/api.ts', import.meta.url).href;
const hook = registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL?.includes('/src/') && specifier.startsWith('.') && !specifier.endsWith('.ts')) return { url: new URL(specifier + '.ts', context.parentURL).href, shortCircuit: true };
  return nextResolve(specifier, context);
} });
const api = await import(apiUrl);
hook.deregister();
const originalFetch = globalThis.fetch;
const origin = 'https://keepsite.example';
const logoutUrl = 'https://keepsite.auth.ap-northeast-3.amazoncognito.com/logout?'
  + new URLSearchParams({ client_id: 'testclient123', logout_uri: origin + '/' });
afterEach(() => { api.installSession(null); globalThis.fetch = originalFetch; });
after(() => { globalThis.fetch = originalFetch; });

test('account methods and linking require the active session and preserve CSRF and password bytes', async () => {
  await assert.rejects(api.getAccountMethods(), error => error.status === 401);
  await assert.rejects(api.addAccountPassword('link', 'a@example.test', 'Password-12!'), error => error.status === 401);
  api.installSession(signedSession('a'));
  const methods = { password: { linked: true, email: 'a@example.test' }, google: { linked: false, email: null } };
  globalThis.fetch = async (path, options) => {
    assert.equal(options.credentials, 'same-origin');
    if (options.method === 'POST') {
      assert.equal(options.headers.get('X-CSRF-Token'), 'test-csrf-a');
      assert.equal(JSON.parse(options.body).password, '  Password-12!  ');
    }
    return Response.json(methods);
  };
  assert.deepEqual(await api.getAccountMethods(), methods);
  assert.deepEqual(await api.addAccountPassword('link', 'a@example.test', '  Password-12!  '), methods);
  globalThis.fetch = async () => Response.json({ password: { linked: true, email: null }, google: { linked: false, email: null } });
  await assert.rejects(api.getAccountMethods(), /信息不完整/);
});
test('Google account binding navigates only to the same-origin single-flow launch', async () => {
  const previousWindow = globalThis.window;
  globalThis.window = { location: { origin } };
  api.installSession(signedSession('a'));
  const browserUrl = origin + '/api/auth/account/google/launch?flow=opaque-flow';
  try {
    globalThis.fetch = async (_path, options) => {
      assert.equal(options.headers.get('X-CSRF-Token'), 'test-csrf-a');
      return Response.json({ browserUrl, expiresAt: Date.now() + 600000 });
    };
    assert.equal(await api.startAccountGoogle(), browserUrl);
    for (const url of ['https://evil.test/api/auth/account/google/launch?flow=x', browserUrl + '&flow=other', browserUrl + '#fragment', origin + '/api/auth/google?flow=x']) {
      globalThis.fetch = async () => Response.json({ browserUrl: url, expiresAt: Date.now() + 600000 });
      await assert.rejects(api.startAccountGoogle(), /绑定地址无效/);
    }
  } finally { globalThis.window = previousWindow; }
});

test('anonymous code cannot request workspace, upload or mutations', async () => {
  let requests = 0;
  globalThis.fetch = async () => { requests += 1; throw new Error('unexpected fetch'); };
  await assert.rejects(api.getSnapshotPage(), error => error.status === 401);
  await assert.rejects(api.mutate('/api/tickets', 'POST', {}), error => error.status === 401);
  await assert.rejects(api.uploadMedia(new File(['x'], 'image.png', { type: 'image/png' }), 'upload-1'), error => error.status === 401);
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
  await assert.rejects(api.getSnapshotPage(), error => error.status === 401);
});

test('login preserves password bytes and uses the Web client header', async () => {
  const password = '  Aa123456789!  ';
  globalThis.fetch = async (path, options) => {
    assert.equal(path, '/api/auth/login');
    assert.equal(options.headers.get('X-Keepsite-Client'), 'web');
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
    assert.equal(options.headers.get('X-Keepsite-Client'), 'web');
    return Response.json(path.endsWith('verify-email') ? { verified: true } : { ok: true });
  };
  assert.equal((await api.verifyEmail('a@example.test', code)).verified, true);
  await api.resetPassword('a@example.test', code, 'A-password-12!');
});

test('authenticated writes attach the active CSRF token and preserve operation identifiers', async () => {
  api.installSession(signedSession());
  globalThis.fetch = async (path, options) => {
    assert.equal(path, '/api/tickets/item-1');
    assert.equal(options.headers.get('X-Keepsite-Client'), 'web');
    assert.equal(options.headers.get('X-CSRF-Token'), 'test-csrf-a');
    assert.equal(options.credentials, 'same-origin');
    assert.deepEqual(JSON.parse(options.body), { operationId: 'op-2', fields: { title: '保留草稿' } });
    return Response.json({ serverId: 'server', operationId: 'op-2', committedCursor: 7 });
  };
  assert.equal((await api.mutate('/api/tickets/item-1', 'PATCH', { operationId: 'op-2', fields: { title: '保留草稿' } })).committedCursor, 7);
});

test('a state from another user invalidates the current session', async () => {
  api.installSession(signedSession());
  let expired = 0;
  const unsubscribe = api.onSessionExpired(() => { expired += 1; });
  globalThis.fetch = async () => Response.json(snapshotPage('b'));
  await assert.rejects(api.getSnapshotPage(), error => error.status === 401);
  await assert.rejects(api.getSnapshotPage(), error => error.status === 401);
  assert.equal(expired, 1);
  unsubscribe();
});

test('an old account response cannot become the new account state even if fetch ignores abort', async () => {
  api.installSession(signedSession('a'));
  let resolve;
  let signal;
  globalThis.fetch = (_path, options) => { signal = options.signal; return new Promise(done => { resolve = done; }); };
  const pending = api.getSnapshotPage();
  api.installSession(signedSession('b'));
  assert.equal(signal.aborted, true);
  resolve(Response.json(snapshotPage('a')));
  await assert.rejects(pending, error => error.name === 'AbortError');
  globalThis.fetch = async () => Response.json(snapshotPage('b'));
  assert.equal((await api.getSnapshotPage()).userId, 'b');
});

test('a late unauthorized response must not log out the new account', async () => {
  api.installSession(signedSession('a'));
  let resolve;
  let expired = 0;
  const unsubscribe = api.onSessionExpired(() => { expired += 1; });
  globalThis.fetch = () => new Promise(done => { resolve = done; });
  const pending = api.getSnapshotPage();
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
  globalThis.fetch = async () => new Response(new ReadableStream({ start(controller) { started(); resolveBody = value => { try { controller.enqueue(new TextEncoder().encode(JSON.stringify(value))); controller.close(); } catch {} }; } }));
  const pending = api.getSnapshotPage();
  await bodyStarted;
  api.installSession(signedSession('b'));
  resolveBody(snapshotPage('a'));
  await assert.rejects(pending, error => error.name === 'AbortError');
});

test('explicit logout forwards only the validated Cognito destination after clearing the local session', async () => {
  api.installSession(signedSession());
  let ended;
  const unsubscribe = api.onSessionExpired(event => { ended = event; });
  try {
    globalThis.fetch = async () => Response.json({ ok: true, logoutUrl });
    await api.signOut(origin);
    assert.deepEqual(ended, { reason: 'logout', logoutUrl });
    await assert.rejects(api.getSnapshotPage(), error => error.status === 401);
  } finally { unsubscribe(); }
});

test('logout rejects unsafe provider and return URLs while still clearing the local session', async () => {
  const unsafe = [
    'javascript:alert(1)', logoutUrl.replace('https:', 'http:'),
    logoutUrl.replace('keepsite.auth.ap-northeast-3.amazoncognito.com', 'evil.example'),
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
      await assert.rejects(api.getSnapshotPage(), error => error.status === 401);
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
    globalThis.fetch = async () => Response.json(snapshotPage('b'));
    assert.equal((await api.getSnapshotPage()).userId, 'b');
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
    assert.equal(options.body.get('uploadId'), 'upload-1');
    return Response.json({ uploadId: 'upload-1', assetId: 'a'.repeat(64), url: '/api/media/' + 'a'.repeat(64), sha256: 'a'.repeat(64), size: 4, width: 1, height: 1, mimeType: 'image/png' });
  };
  assert.equal((await api.uploadMedia(file, 'upload-1')).url, '/api/media/' + 'a'.repeat(64));
});

test('conflict and network errors do not turn into fake successful state', async () => {
  api.installSession(signedSession());
  globalThis.fetch = async () => Response.json({ detail: '版本已更新' }, { status: 409 });
  await assert.rejects(api.mutate('/api/tickets/item-1', 'PATCH', { operationId: 'conflict' }), error => error.status === 409 && error.message === '版本已更新');
  globalThis.fetch = async () => { throw new TypeError('network failed'); };
  await assert.rejects(api.getSnapshotPage(), /暂时无法连接服务/);
});

for (const [label, consume, start, limit] of [
  ['JSON save', 'json', () => api.mutate('/api/tickets/item-1', 'PATCH', { operationId: 'save-1' }), 30000],
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
    globalThis.fetch = async () => Response.json(snapshotPage());
    assert.equal((await api.getSnapshotPage()).cursor, 7);
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
  const request = api.getSnapshotPage();
  const rejected = assert.rejects(request, /请求超时/);
  await consuming;
  context.mock.timers.tick(30000);
  await rejected;
  api.installSession(signedSession('b'));
  resolveBody({ detail: 'expired' });
  await Promise.resolve();
  globalThis.fetch = async () => Response.json(snapshotPage('b'));
  assert.equal((await api.getSnapshotPage()).userId, 'b');
});

test('account cancellation promptly rejects a body that never finishes', async () => {
  api.installSession(signedSession('a'));
  let bodyStarted;
  const consuming = new Promise(resolve => { bodyStarted = resolve; });
  globalThis.fetch = async () => new Response(new ReadableStream({ start() { bodyStarted(); } }));
  const request = api.getSnapshotPage();
  const rejected = assert.rejects(request, error => error.name === 'AbortError');
  await consuming;
  api.installSession(signedSession('b'));
  await rejected;
});

test('malformed capabilities cannot claim a supported device sync mode', async () => {
  api.installSession(signedSession());
  globalThis.fetch = async () => Response.json({ ...snapshotPage(), capabilities: { processing: false, deviceSync: 'enabled', cloudStorage: true } });
  await assert.rejects(api.getSnapshotPage(), /功能状态不完整/);
});

test('upload confirmation rejects mismatched identities, unsupported types and incomplete dimensions', async () => {
  api.installSession(signedSession());
  const valid = { uploadId: 'upload-1', assetId: 'a'.repeat(64), sha256: 'a'.repeat(64), url: '/api/media/' + 'a'.repeat(64), size: 4, width: 1, height: 1, mimeType: 'image/png' };
  for (const invalid of [{ ...valid, assetId: 'b'.repeat(64) }, { ...valid, width: 0 }, { ...valid, mimeType: 'text/html' }, { ...valid, url: '/api/media/elsewhere' }]) {
    globalThis.fetch = async () => Response.json(invalid);
    await assert.rejects(api.uploadMedia(new File(['test'], 'ticket.png', { type: 'image/png' }), 'upload-1'), /图片上传结果不完整/);
  }
});

test('operation lookup uses the active session and validates the exact receipt', async () => {
  await assert.rejects(api.getOperationStatus('write-1'), error => error.status === 401);
  api.installSession(signedSession());
  globalThis.fetch = async (path, options) => {
    assert.equal(path, '/api/operations/write-1'); assert.equal(options.credentials, 'same-origin');
    return Response.json({ serverId: 'server', operationId: 'write-1', applied: false, committedCursor: null });
  };
  assert.deepEqual(await api.getOperationStatus('write-1'), { serverId: 'server', operationId: 'write-1', applied: false, committedCursor: null });
  for (const receipt of [{ operationId: 'other', applied: true }, { operationId: 'write-1', applied: 'true' }]) {
    globalThis.fetch = async () => Response.json(receipt);
    await assert.rejects(api.getOperationStatus('write-1'), /操作核对结果不完整/);
  }
  await assert.rejects(api.getOperationStatus('../other'), /操作标识无效/);
});
test('a receipt from a replaced session cannot confirm another account draft', async () => {
  api.installSession(signedSession('a')); let release;
  globalThis.fetch = () => new Promise(resolve => { release = resolve; });
  const pending = api.getOperationStatus('write-1'); const rejection = assert.rejects(pending, error => error.name === 'AbortError');
  api.installSession(signedSession('b')); release(Response.json({ serverId: 'server', operationId: 'write-1', applied: true, committedCursor: 7 })); await rejection;
});

const signedImage = 'https://private-bucket.s3.ap-northeast-3.amazonaws.com/ticket.png?X-Amz-Signature=test';
const authorization = (url = signedImage, expiresAt = Date.now() + 60000) => ({ url, expiresAt, transport: 's3', sha256: media().sha256, size: png.length, mimeType: 'image/png' });
const imageResponse = () => new Response(png, { headers: { 'Content-Type': 'image/png' } });

test('S3 images omit credentials and retry a rejected signature exactly once', async () => {
  api.installSession(signedSession()); let authorizations = 0, downloads = 0;
  globalThis.fetch = async (path, options) => {
    if (path.endsWith('/download')) { authorizations++; assert.equal(options.credentials, 'same-origin'); return Response.json(authorization()); }
    assert.equal(path, signedImage); assert.equal(options.credentials, 'omit'); assert.equal(options.referrerPolicy, 'no-referrer'); assert.equal(options.redirect, 'error'); assert.equal(options.headers.get('X-CSRF-Token'), null);
    return downloads++ === 0 ? new Response('', { status: 403 }) : imageResponse();
  };
  const result = await api.downloadMedia(media(), new AbortController().signal);
  assert.equal(result.transport, 's3'); assert.equal(result.blob.size, png.length); assert.equal(authorizations, 2); assert.equal(downloads, 2);
});
test('expired short URLs are reauthorized before any image request', async () => {
  api.installSession(signedSession()); let authorizations = 0, downloads = 0;
  globalThis.fetch = async path => {
    if (path.endsWith('/download')) return Response.json(authorization(signedImage, authorizations++ === 0 ? Date.now() - 1 : Date.now() + 60000));
    downloads++; return imageResponse();
  };
  await api.downloadMedia(media(), new AbortController().signal); assert.equal(authorizations, 2); assert.equal(downloads, 1);
});
test('S3 CORS or network failure falls back once to the authorized same-origin stream', async () => {
  api.installSession(signedSession()); const calls = [];
  globalThis.fetch = async (path, options) => {
    calls.push(path);
    if (path.endsWith('/download')) return Response.json(authorization());
    if (path === signedImage) throw new TypeError('CORS');
    assert.equal(path, media().url); assert.equal(options.credentials, 'same-origin'); return imageResponse();
  };
  const result = await api.downloadMedia(media(), new AbortController().signal);
  assert.equal(result.transport, 'server'); assert.equal(calls.length, 3);
});
test('untrusted download URLs and mismatched metadata are rejected before requesting bytes', async () => {
  api.installSession(signedSession());
  for (const invalid of [authorization('https://evil.test/image'), authorization('https://private.s3.us-east-1.amazonaws.com.evil.test/image'), { ...authorization(), sha256: 'b'.repeat(64) }, { ...authorization(), transport: 'server', url: '/api/media/another' }]) {
    let calls = 0; globalThis.fetch = async () => { calls++; return Response.json(invalid); };
    await assert.rejects(api.downloadMedia(media(), new AbortController().signal), /图片下载授权不完整/); assert.equal(calls, 1);
  }
});
test('media streams reject unexpected bytes instead of allocating unbounded images', async () => {
  api.installSession(signedSession());
  globalThis.fetch = async path => path.endsWith('/download')
    ? Response.json({ ...authorization(media().url), transport: 'server' })
    : new Response(Buffer.concat([png, png]), { headers: { 'Content-Type': 'image/png' } });
  await assert.rejects(api.downloadMedia(media(), new AbortController().signal), /图片大小与记录不一致/);
});
test('account replacement cancels a signed image and prevents fallback into the new session', async () => {
  api.installSession(signedSession('a')); let started, release; const downloading = new Promise(resolve => { started = resolve; }); let calls = 0;
  globalThis.fetch = async path => { calls++; if (path.endsWith('/download')) return Response.json(authorization()); started(); return new Promise(resolve => { release = resolve; }); };
  const pending = api.downloadMedia(media(), new AbortController().signal); const rejected = assert.rejects(pending, error => error.name === 'AbortError');
  await downloading; api.installSession(signedSession('b')); release(imageResponse()); await rejected; assert.equal(calls, 2);
});
test('upload status cannot confuse another intent or mark a pending object usable', async () => {
  api.installSession(signedSession());
  for (const invalid of [{ uploadId: 'other', status: 'stored', asset: media() }, { uploadId: 'upload-1', status: 'pending', asset: media() }, { uploadId: 'upload-1', status: 'attached', asset: null }]) {
    globalThis.fetch = async () => Response.json(invalid); await assert.rejects(api.getUploadStatus('upload-1'), /上传核对结果不完整/);
  }
  globalThis.fetch = async () => Response.json({ uploadId: 'upload-1', status: 'stored', asset: media() });
  assert.equal((await api.getUploadStatus('upload-1')).asset.assetId, media().assetId);
});

test('sync pages reject oversized bodies, malformed pagination and mismatched cursors', async () => {
  api.installSession(signedSession());
  globalThis.fetch = async () => new Response(' '.repeat(1024 * 1024 + 1));
  await assert.rejects(api.getSnapshotPage(), /超过大小限制/);
  for (const patch of [{ total: 10001 }, { offset: 2 }, { nextPageToken: '' }, { cursor: -1 }]) {
    globalThis.fetch = async () => Response.json({ ...snapshotPage(), ...patch });
    await assert.rejects(api.getSnapshotPage(), /快照分页格式异常/);
  }
  const page = { serverId: 'server', userId: 'a', fromCursor: 7, nextCursor: 7, headCursor: 7, hasMore: false, changes: [], capabilities: snapshotPage().capabilities };
  for (const patch of [{ fromCursor: 6 }, { nextCursor: 8 }, { hasMore: true }, { changes: [{ cursor: 9, type: 'delete', id: 'ticket', version: 2, deletedAt: 0 }] }]) {
    globalThis.fetch = async () => Response.json({ ...page, ...patch });
    await assert.rejects(api.getChanges('server', 7), error => error.code === 'cursor_gap');
  }
});

test('structured cursor expiration is preserved without expiring authentication', async () => {
  api.installSession(signedSession());
  globalThis.fetch = async () => Response.json({ detail: { code: 'cursor_expired', message: '游标已过期' } }, { status: 410 });
  await assert.rejects(api.getChanges('server', 7), error => error.status === 410 && error.code === 'cursor_expired' && error.message === '游标已过期');
  globalThis.fetch = async () => Response.json(snapshotPage()); assert.equal((await api.getSnapshotPage()).userId, 'a');
});

test('write and lookup receipts require the server domain and never accept the old shape', async () => {
  api.installSession(signedSession());
  globalThis.fetch = async () => Response.json({ operationId: 'op-1', committedCursor: 10 });
  await assert.rejects(api.mutate('/api/tickets/item-1', 'PATCH', { operationId: 'op-1' }), /写入回执不完整/);
  globalThis.fetch = async () => Response.json({ operationId: 'op-1', applied: false, committedCursor: null });
  await assert.rejects(api.getOperationStatus('op-1'), /操作核对结果不完整/);
});

test('cloud observation counts received bytes from failed responses as well as successful downloads', async () => {
  api.installSession(signedSession()); const events = [];
  globalThis.fetch = async path => path.endsWith('/download') ? Response.json({ ...authorization(media().url), transport: 'server' }) : new Response(Buffer.concat([png, png]), { headers: { 'Content-Type': 'image/png' } });
  await assert.rejects(api.downloadMedia(media(), new AbortController().signal, value => events.push(value)), /大小与记录不一致/);
  assert.equal(events[0].event, 'start'); assert.equal(events.at(-1).event, 'failed');
  assert.equal(events.filter(event => event.event === 'bytes').reduce((sum, event) => sum + event.bytes, 0), png.length * 2);
});

test('the shared socket accepts bounded peer envelopes but closes malformed UTF-8 controls without logging out', async () => {
  const originalSocket = globalThis.WebSocket, originalWindow = globalThis.window, sockets = [];
  class Socket extends EventTarget {
    static OPEN = 1; readyState = 1; bufferedAmount = 0; sent = [];
    constructor(url) { super(); this.url = String(url); sockets.push(this); }
    close() { this.readyState = 3; this.dispatchEvent(new Event('close')); }
    send(value) { this.sent.push(value); }
  }
  globalThis.WebSocket = Socket; globalThis.window = { location: { origin: 'https://keepsite.example' } };
  api.installSession(signedSession()); let closed = 0; const received = [];
  let connection;
  try {
    connection = api.openSyncEvents(value => received.push(value), () => { closed++; });
    assert.equal(sockets[0].url, 'wss://keepsite.example/api/sync/events');
    sockets[0].dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ v: 1, type: 'device.peers', serverId: 'server', userId: 'a', peers: [] }) }));
    assert.equal(received[0].type, 'device.peers'); assert.ok(received[0].wireBytes > 0);
    assert.equal(connection.send({ v: 1, type: 'peer.signal', requestId: 'r', payload: 'x'.repeat(65536) }), false);
    sockets[0].dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ v: 1, type: 'peer.error', serverId: 'server', userId: 'a', message: '字'.repeat(1600) }) }));
    assert.equal(closed, 1); assert.equal(received.length, 1);
    globalThis.fetch = async () => Response.json(snapshotPage()); assert.equal((await api.getSnapshotPage()).userId, 'a');
  } finally { connection?.stop(); globalThis.WebSocket = originalSocket; globalThis.window = originalWindow; }
});

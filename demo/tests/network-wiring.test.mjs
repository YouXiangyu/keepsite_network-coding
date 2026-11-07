import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { signedSession, snapshotPage, media, png, sampleItem } from './web-fixtures.mjs';
const hook = registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL?.includes('/src/') && specifier.startsWith('.') && !specifier.endsWith('.ts')) return { url: new URL(specifier + '.ts', context.parentURL).href, shortCircuit: true };
  return nextResolve(specifier, context);
} });
const api = await import('../src/api.ts');
const { AccountSync } = await import('../src/app/accountSync.ts');
const runtime = await import('../src/network/runtime.ts');
const { networkMonitor: monitor } = runtime; hook.deregister();
const original = { fetch: globalThis.fetch, window: globalThis.window, WebSocket: globalThis.WebSocket };
const actors = [], sockets = [], connections = [];
afterEach(() => {
  for (const actor of actors.splice(0)) actor.stop();
  for (const connection of connections.splice(0)) connection.stop();
  api.installSession(null); sockets.length = 0; Object.assign(globalThis, original);
});
const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };
const exported = () => JSON.stringify(monitor.exportSnapshot(monitor.exportLease()));
const details = () => { api.installSession(signedSession()); monitor.setDetailed(true); };
const changes = (serverId, after) => ({ serverId, userId: 'a', fromCursor: after, nextCursor: after, headCursor: after, hasMore: false, changes: [], capabilities: snapshotPage().capabilities });
function setupSync(overrides = {}) {
  const events = [];
  const actor = new AccountSync('a', {
    snapshot: overrides.snapshot ?? (async () => snapshotPage()), changes: overrides.changes ?? (async (server, after) => changes(server, after)),
    events: (hint, close) => { const item = { hint, close, stopped: false }; events.push(item); return { stop: () => { item.stopped = true; }, send: () => true }; },
  });
  actors.push(actor); actor.start(); return { actor, events };
}
class Socket extends EventTarget {
  static OPEN = 1; readyState = 1; bufferedAmount = 0;
  constructor(url) { super(); this.url = String(url); sockets.push(this); }
  close(code = 1000, reason = '') { if (this.readyState === 3) return; this.readyState = 3; const event = new Event('close'); Object.assign(event, { code, reason }); this.dispatchEvent(event); }
  send() {}
  message(value) { this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify(value) })); }
}
function browser() { globalThis.WebSocket = Socket; globalThis.window = { location: { origin: 'https://keepsite.example' } }; }

test('real API requests collect only authenticated same-origin headers and fixed templates', async () => {
  const requests = []; api.installSession(signedSession());
  globalThis.fetch = async (path, options) => { requests.push({ path, headers: options.headers }); return Response.json(snapshotPage()); };
  await api.getSnapshotPage('private-pagination-token');
  assert.equal(requests[0].headers.has('X-Keepsite-Diagnostics'), false); assert.equal(monitor.snapshot().events.length, 0);
  assert.equal(monitor.snapshot().cards.api.state, 'reachable');
  monitor.setDetailed(true); await api.getSnapshotPage('private-pagination-token');
  assert.match(requests[1].headers.get('X-Keepsite-Diagnostics'), /^[a-f0-9]{32}$/);
  assert.match(requests[1].headers.get('X-Keepsite-Trace-Id'), /^[a-f0-9]{32}$/);
  const events = monitor.snapshot().events;
  assert.deepEqual(events.map(value => [value.template, value.status]), [['sync.snapshot', 'started'], ['sync.snapshot', 'completed']]);
  assert.equal(events[0].traceId, requests[1].headers.get('X-Keepsite-Trace-Id'));
  assert.equal(events[1].operationId, events[0].operationId); assert.ok(events[1].durationMs >= 0);
  assert.doesNotMatch(exported(), /private-pagination-token|test-csrf|example\.test|snapshot-a|item-1|星际穿越/);
});

test('OAuth, anonymous authentication, signed S3 and GitHub never receive diagnostic headers', async () => {
  details(); browser(); const requests = []; const asset = media();
  globalThis.fetch = async (path, options) => {
    requests.push({ path, options });
    if (String(path).includes('account/google')) return Response.json({ browserUrl: 'https://keepsite.example/api/auth/account/google/launch?flow=opaque', expiresAt: Date.now() + 600000 });
    if (String(path).endsWith('/download')) return Response.json({ ...asset, url: 'https://bucket.s3.amazonaws.com/private?X-Amz-Signature=secret', expiresAt: Date.now() + 600000, transport: 's3' });
    if (String(path).startsWith('https://bucket.')) return new Response(png, { headers: { 'Content-Type': 'image/png' } });
    if (String(path).startsWith('https://api.github.com/')) return Response.json({ draft: false, prerelease: false, tag_name: 'v0.1', assets: [] });
    return Response.json(signedSession());
  };
  await api.startAccountGoogle(); await api.signIn('private@example.test', 'private-password');
  await api.downloadMedia(asset, new AbortController().signal);
  const before = monitor.snapshot().cards.api;
  await api.latestAndroidRelease(new AbortController().signal);
  assert.deepEqual(monitor.snapshot().cards.api, before);
  for (const request of requests) {
    const headers = new Headers(request.options.headers);
    assert.equal(headers.has('X-Keepsite-Diagnostics'), request.path.endsWith('/download'));
    assert.equal(headers.has('X-Keepsite-Trace-Id'), request.path.endsWith('/download'));
  }
  const external = requests.find(request => request.path.startsWith('https://bucket.'));
  assert.equal(external.options.credentials, 'omit');
  assert.equal(monitor.snapshot().events.filter(event => event.event === 'http.request').length, 6);
  assert.doesNotMatch(exported(), /private-password|private@example|X-Amz|bucket|opaque|[a-f0-9]{64}/);
});

test('clear during body consumption keeps live status but cannot revive a retired operation', async () => {
  details(); const gate = deferred(); let headers;
  globalThis.fetch = async (_path, options) => { headers = options.headers; return { ok: true, status: 200, json: () => gate.promise }; };
  const pending = api.getAccountMethods(); await settle();
  const capture = headers.get('X-Keepsite-Diagnostics'); monitor.clearHistory();
  gate.resolve({ password: { linked: true, email: 'a@example.test' }, google: { linked: false, email: null } });
  await pending;
  assert.notEqual(monitor.snapshot().captureId, capture); assert.equal(monitor.snapshot().events.length, 0);
  assert.equal(monitor.snapshot().cards.api.active, 0); assert.equal(monitor.snapshot().cards.api.state, 'reachable');
  globalThis.fetch = async () => Response.json(snapshotPage()); await api.getSnapshotPage();
  assert.equal(monitor.snapshot().events.length, 2);
});

test('replacement account rejects delayed HTTP completion and resets all diagnostic preferences', async () => {
  details(); const gate = deferred();
  globalThis.fetch = async () => gate.promise; const old = api.getSnapshotPage(); const rejected = assert.rejects(old, error => error instanceof DOMException && error.name === 'AbortError');
  api.installSession(signedSession('b')); const replacement = monitor.snapshot().sessionId;
  gate.resolve(Response.json(snapshotPage())); await rejected; await settle();
  assert.equal(monitor.snapshot().sessionId, replacement); assert.equal(monitor.snapshot().detailed, false);
  assert.equal(monitor.snapshot().events.length, 0); assert.deepEqual(monitor.snapshot().cards.api, { state: 'unknown' });
});

test('403 remains reachable and 401 retires the account without exporting private error details', async () => {
  details();
  globalThis.fetch = async () => Response.json({ detail: 'secret signedUrl=email@example.test&token=x' }, { status: 403 });
  await assert.rejects(api.getSnapshotPage(), error => error.status === 403);
  assert.equal(monitor.snapshot().cards.api.state, 'reachable'); assert.equal(monitor.snapshot().cards.api.auth, 'forbidden');
  assert.equal(monitor.snapshot().events.at(-1).httpStatus, 403); assert.doesNotMatch(exported(), /secret|email@|signedUrl/);
  globalThis.fetch = async () => Response.json({ detail: 'private account' }, { status: 401 });
  await assert.rejects(api.getSnapshotPage(), error => error.status === 401);
  assert.equal(monitor.snapshot().active, false); assert.equal(monitor.snapshot().events.length, 0); assert.equal(monitor.snapshot().captureId, null);
});

test('HTTP timeout and observer failure preserve the original business result', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] }); details();
  globalThis.fetch = async () => new Promise(() => {});
  const pending = api.getSnapshotPage(); const rejected = assert.rejects(pending, /超时/);
  context.mock.timers.tick(30001); await rejected;
  assert.equal(monitor.snapshot().events.at(-1).errorCode, 'timeout'); assert.equal(monitor.snapshot().cards.api.active, 0);
  const update = monitor.update;
  try { monitor.update = () => { throw new Error('observer only'); }; globalThis.fetch = async () => Response.json(snapshotPage()); assert.equal((await api.getSnapshotPage()).cursor, 7); }
  finally { monitor.update = update; }
});

test('real socket observes open, validated messages and policy close without inferring logout', () => {
  details(); browser(); const received = []; let closes = 0;
  connections.push(api.openSyncEvents(value => received.push(value), () => { closes++; })); const socket = sockets[0];
  assert.equal(monitor.snapshot().cards.websocket.state, 'connecting'); assert.equal(monitor.snapshot().cards.websocket.watchdogMs, null);
  socket.dispatchEvent(new Event('open')); assert.equal(monitor.snapshot().cards.websocket.state, 'open');
  socket.message({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'private-server', cursor: 7, heartbeatMs: 15000, diagnosticsVersion: 1 });
  assert.equal(received.length, 1); assert.equal(monitor.snapshot().cards.websocket.messageType, 'sync.ready');
  // Only AccountSync can declare a subscription ready; receipt alone is not that state.
  assert.equal(monitor.snapshot().cards.websocket.state, 'open');
  socket.close(1008, 'private session token');
  assert.equal(closes, 1); assert.equal(monitor.snapshot().cards.websocket.closeCode, 1008); assert.equal(monitor.snapshot().active, true);
  assert.doesNotMatch(exported(), /private-server|private session|token/);
});

test('socket reconnection rejects old frames and malformed messages without storing payloads', () => {
  details(); browser(); const first = api.openSyncEvents(() => {}, () => {}); connections.push(first);
  first.stop(); const next = api.openSyncEvents(() => {}, () => {}); connections.push(next);
  const before = monitor.snapshot(); sockets[0].message({ v: 1, type: 'sync.hint', userId: 'a', serverId: 's', cursor: 9 });
  assert.equal(monitor.snapshot(), before);
  sockets[1].message({ v: 1, type: 'sync.hint', userId: 'a', serverId: 's', cursor: 'malformed secret' });
  assert.equal(monitor.snapshot().cards.websocket.errorCode, 'invalid'); assert.equal(monitor.snapshot().active, true);
  assert.doesNotMatch(exported(), /malformed secret/);
});

test('WS ready and observed watermarks cannot claim a snapshot was already applied', async () => {
  details(); const snapshot = deferred(), delta = deferred(), subsequent = deferred(); let reads = 0;
  const h = setupSync({ snapshot: () => snapshot.promise, changes: () => ++reads === 1 ? delta.promise : subsequent.promise });
  h.events[0].hint({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'server', cursor: 9, heartbeatMs: 15000, diagnosticsVersion: 1 });
  assert.equal(monitor.snapshot().cards.websocket.state, 'ready'); assert.equal(monitor.snapshot().cards.websocket.heartbeatConfiguredMs, 15000);
  assert.equal(h.actor.eventState().diagnosticsVersion, 1); assert.equal(h.actor.eventState().connected, false);
  assert.equal(monitor.snapshot().cards.sync.localCursor, undefined); assert.equal(monitor.snapshot().cards.sync.lastSnapshotAt, undefined);
  snapshot.resolve(snapshotPage()); await settle();
  assert.equal(monitor.snapshot().cards.sync.localCursor, undefined);
  monitor.clearHistory();
  delta.resolve(changes('server', 7)); await settle();
  assert.equal(monitor.snapshot().cards.sync.localCursor, 7); assert.equal(monitor.snapshot().cards.sync.serverCursor, 9);
  assert.equal(monitor.snapshot().cards.sync.pending, true);
  // Catch-up can start a genuinely new read after clear; the old read cannot finish into this history.
  assert.deepEqual(monitor.snapshot().events.map(event => [event.event, event.status]), [['sync.delta', 'started']]);
  assert.ok(monitor.snapshot().cards.sync.lastSnapshotAt > 0);
});

test('ready timeout, retry and reconnect use actual scheduler values and reset optional capabilities', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] }); details(); const h = setupSync(); await h.actor.catchUp();
  assert.equal(monitor.snapshot().cards.websocket.initialTimeoutMs, 8000); assert.equal(monitor.snapshot().cards.websocket.watchdogMs, 45000);
  monitor.clearHistory();
  context.mock.timers.tick(8001); await settle();
  assert.equal(h.events[0].stopped, true); assert.equal(monitor.snapshot().cards.websocket.errorCode, 'timeout');
  assert.equal(monitor.snapshot().cards.websocket.state, 'reconnecting'); assert.equal(monitor.snapshot().cards.websocket.retryDelayMs, 1000);
  assert.equal(monitor.snapshot().events.length, 0);
  context.mock.timers.tick(1001); await settle(); assert.equal(h.events.length, 2);
  h.events[1].hint({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'server', cursor: 7, heartbeatMs: 15000, diagnosticsVersion: 1 }); await settle();
  assert.equal(monitor.snapshot().cards.websocket.diagnosticsVersion, 1); assert.equal(monitor.snapshot().cards.websocket.retryAt, null);
  context.mock.timers.tick(45001); await settle(); context.mock.timers.tick(1001); await settle();
  assert.equal(monitor.snapshot().cards.websocket.heartbeatConfiguredMs, null); assert.equal(h.actor.eventState().diagnosticsVersion, undefined);
  h.events.at(-1).hint({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'server', cursor: 7, heartbeatMs: -3, diagnosticsVersion: 9 });
  assert.equal(monitor.snapshot().cards.websocket.heartbeatConfiguredMs, null); assert.equal(monitor.snapshot().cards.websocket.diagnosticsVersion, null);
});

test('stopped paginated snapshots and callbacks cannot advance applied cursor or revive cards', async () => {
  details(); const gate = deferred(), expiresAt = Date.now() + 600000;
  const h = setupSync({ snapshot: async token => token ? gate.promise : { ...snapshotPage(), total: 2, nextPageToken: 'secret-page', expiresAt } });
  await settle(); assert.equal(monitor.snapshot().cards.sync.lastSnapshotAt, undefined);
  h.actor.stop(); const state = monitor.snapshot();
  gate.resolve({ ...snapshotPage(), offset: 1, total: 2, tickets: [sampleItem('second')], expiresAt });
  h.events[0].hint({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'server', cursor: 200, diagnosticsVersion: 1 }); await settle();
  assert.equal(monitor.snapshot(), state); assert.equal(monitor.snapshot().cards.sync.localCursor, undefined);
  assert.equal(monitor.snapshot().cards.sync.lastSnapshotAt, undefined); assert.doesNotMatch(exported(), /secret-page/);
});

test('repeated HTTP observer faults keep owners and release slots while preserving success and original errors', async () => {
  details(); const methods = { password: { linked: true, email: 'a@example.test' }, google: { linked: false, email: null } };
  const businessError = new api.ApiError('business error remains original', 409);
  globalThis.fetch = async () => Response.json(methods); await api.getAccountMethods();
  for (const method of ['update', 'record']) {
    const originalMethod = monitor[method]; monitor[method] = () => { throw new Error('observer fault'); };
    try {
      for (let i = 0; i < 40; i++) {
        globalThis.fetch = async () => Response.json(methods); assert.deepEqual(await api.getAccountMethods(), methods);
        globalThis.fetch = async () => ({ status: 200, ok: true, json: async () => { throw businessError; } });
        await assert.rejects(api.getAccountMethods(), error => error === businessError);
        assert.equal(monitor.snapshot().metrics.activeOperations, 0);
      }
    } finally { monitor[method] = originalMethod; }
    globalThis.fetch = async () => Response.json(methods); await api.getAccountMethods();
    assert.equal(monitor.snapshot().cards.api.active, 0); assert.equal(monitor.snapshot().metrics.activeOperations, 0);
    assert.match(monitor.snapshot().events.at(-1).traceId, /^[a-f0-9]{32}$/);
  }
});

test('HTTP terminal update failure still publishes zero active and releases the operation', async () => {
  details(); const update = monitor.update; let faults = 0;
  monitor.update = function(scope, card, values) {
    if (card === 'api' && Object.hasOwn(values, 'durationMs')) { faults++; throw new Error('terminal update fault'); }
    return update.call(this, scope, card, values);
  };
  try {
    globalThis.fetch = async () => Response.json(snapshotPage());
    for (let i = 0; i < 40; i++) { await api.getSnapshotPage(); assert.equal(monitor.snapshot().metrics.activeOperations, 0); assert.equal(monitor.snapshot().cards.api.active, 0); }
  } finally { monitor.update = update; }
  assert.equal(faults, 40);
});

test('socket and pull observers retain allocated handles and release them after start or finish faults', () => {
  details(); const sync = runtime.startSync();
  for (const method of ['update', 'record']) {
    for (const phase of ['start', 'finish']) {
      const originalMethod = monitor[method];
      const fault = () => { throw new Error('observer fault'); };
      for (let i = 0; i < 40; i++) {
        try {
          if (phase === 'start') monitor[method] = fault;
          const socket = runtime.startSocket(), pull = runtime.startPull(sync, 'delta');
          assert.ok(socket?.operation); assert.ok(pull?.operation); assert.equal(monitor.snapshot().metrics.activeOperations, 2);
          monitor[method] = phase === 'finish' ? fault : originalMethod;
          runtime.socketClose(socket); runtime.finishPull(pull, 'completed', { cursor: 7, head: 7, count: 0, pending: false });
          assert.equal(monitor.snapshot().metrics.activeOperations, 0);
        } finally { monitor[method] = originalMethod; }
      }
    }
  }
});

test('HTTP server_changed rebuild clears the old watermark before lower-cursor apply and a later WS ready', async () => {
  details(); let server = 'old', changed = false;
  const h = setupSync({ snapshot: async () => ({ ...snapshotPage(), serverId: server, cursor: server === 'old' ? 100 : 3 }),
    changes: async (serverId, after) => { if (serverId !== server) { changed = true; throw new api.ApiError('server changed', 410, 'server_changed'); } return changes(serverId, after); } });
  await h.actor.catchUp(); h.events[0].hint({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'old', cursor: 100 }); await settle();
  server = 'new'; await h.actor.catchUp();
  assert.equal(changed, true); assert.equal(h.actor.snapshot().serverId, 'new');
  assert.equal(monitor.snapshot().cards.sync.localCursor, 3); assert.equal(monitor.snapshot().cards.sync.serverCursor, 3); assert.equal(monitor.snapshot().cards.sync.pending, false);
  h.events.at(-1).hint({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'new', cursor: 3 }); await settle();
  assert.equal(monitor.snapshot().cards.sync.serverCursor, 3); assert.equal(monitor.snapshot().cards.sync.pending, false);
});

test('same-cursor heartbeat preserves a pending delta until its actual successful apply', async () => {
  details(); const gate = deferred(); let hold = false;
  const h = setupSync({ changes: async (serverId, after) => { if (hold) { hold = false; return gate.promise; } return changes(serverId, after); } });
  await h.actor.catchUp(); h.events[0].hint({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'server', cursor: 7 }); await settle();
  hold = true; const pull = h.actor.catchUp(); await settle();
  assert.equal(monitor.snapshot().cards.sync.state, 'delta');
  h.events[0].hint({ v: 1, type: 'sync.hint', userId: 'a', serverId: 'server', cursor: 7 });
  assert.equal(monitor.snapshot().cards.sync.state, 'delta'); assert.equal(monitor.snapshot().cards.sync.pending, true);
  gate.resolve(changes('server', 7)); await pull;
  assert.equal(monitor.snapshot().cards.sync.state, 'current'); assert.equal(monitor.snapshot().cards.sync.pending, false);
});

test('watermark updates cannot clear failed or snapshot lifecycle without a successful apply', () => {
  details(); const sync = runtime.startSync();
  const initial = runtime.startPull(sync, 'snapshot'); runtime.finishPull(initial, 'completed', { cursor: 7, head: 7, count: 1, pending: false });
  const failed = runtime.startPull(sync, 'delta'); runtime.finishPull(failed, 'failed');
  runtime.syncWatermark(sync, 7);
  assert.equal(monitor.snapshot().cards.sync.state, 'failed'); assert.equal(monitor.snapshot().cards.sync.pending, true);
  const snapshot = runtime.startPull(sync, 'snapshot'); runtime.syncWatermark(sync, 7);
  assert.equal(monitor.snapshot().cards.sync.state, 'snapshot'); assert.equal(monitor.snapshot().cards.sync.pending, true);
  runtime.finishPull(snapshot, 'completed', { cursor: 7, head: 7, count: 1, pending: false });
  assert.equal(monitor.snapshot().cards.sync.state, 'current'); assert.equal(monitor.snapshot().cards.sync.pending, false);
});

test('retired old-domain WS hints cannot refill watermark or desired cursor while the new snapshot is pending', async () => {
  details(); const replacement = deferred(); let server = 'old';
  const h = setupSync({ snapshot: async () => server === 'old' ? { ...snapshotPage(), serverId: 'old', cursor: 100 } : replacement.promise,
    changes: async (serverId, after) => { if (serverId !== server) throw new api.ApiError('server changed', 410, 'server_changed'); return changes(serverId, after); } });
  await h.actor.catchUp(); const oldSocket = h.events[0];
  oldSocket.hint({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'old', cursor: 100, diagnosticsVersion: 1 }); await settle();
  server = 'new'; const rebuild = h.actor.catchUp().then(() => null, error => error); await settle();
  assert.equal(monitor.snapshot().cards.sync.state, 'snapshot');
  assert.equal(h.actor.eventState().connected, false); assert.equal(h.actor.eventState().diagnosticsVersion, undefined);
  oldSocket.hint({ v: 1, type: 'sync.hint', userId: 'a', serverId: 'old', cursor: 100 });
  const oldHeadDuringRebuild = monitor.snapshot().cards.sync.serverCursor;
  replacement.resolve({ ...snapshotPage(), serverId: 'new', cursor: 3 });
  const result = await rebuild; await settle();
  assert.equal(oldHeadDuringRebuild, undefined); assert.equal(result, null);
  assert.equal(oldSocket.stopped, true); assert.equal(h.events.length, 2);
  assert.equal(h.actor.snapshot().error, ''); assert.equal(monitor.snapshot().cards.sync.localCursor, 3);
  assert.equal(monitor.snapshot().cards.sync.serverCursor, 3); assert.equal(monitor.snapshot().cards.sync.pending, false);
  assert.equal(monitor.snapshot().cards.sync.state, 'current');
  h.events[1].hint({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'new', cursor: 3 }); await settle();
  assert.equal(monitor.snapshot().cards.sync.serverCursor, 3); assert.equal(monitor.snapshot().cards.sync.pending, false);
  assert.equal(monitor.snapshot().cards.sync.state, 'current');
});

for (const observationMissing of [false, true]) test(`an old socket close keeps retry in its original domain (initial observation missing: ${observationMissing})`, async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] }); details(); const replacement = deferred(); let server = 'old';
  const replaceConnection = monitor.replaceConnection;
  if (observationMissing) monitor.replaceConnection = function(channel) {
    if (channel === 'sync') { monitor.replaceConnection = replaceConnection; throw new Error('initial observer unavailable'); }
    return replaceConnection.call(this, channel);
  };
  let h;
  try { h = setupSync({ snapshot: async () => server === 'old' ? { ...snapshotPage(), serverId: 'old', cursor: 100 } : replacement.promise,
    changes: async (serverId, after) => { if (serverId !== server) throw new api.ApiError('server changed', 410, 'server_changed'); return changes(serverId, after); } }); }
  finally { monitor.replaceConnection = replaceConnection; }
  await h.actor.catchUp(); h.events[0].hint({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'old', cursor: 100 }); await settle();
  server = 'new'; const rebuild = h.actor.catchUp(); await settle(); h.events[0].close();
  const retryDuringReplacement = monitor.snapshot().cards.sync.retryDelayMs;
  replacement.resolve({ ...snapshotPage(), serverId: 'new', cursor: 3 }); await rebuild;
  assert.equal(retryDuringReplacement, undefined); assert.equal(monitor.snapshot().cards.sync.retryAt, undefined);
  context.mock.timers.tick(1001); await settle();
  assert.equal(h.events.length, 2); assert.equal(monitor.snapshot().cards.sync.serverCursor, 3);
  assert.equal(monitor.snapshot().cards.sync.pending, false);
});

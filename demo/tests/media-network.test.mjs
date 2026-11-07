import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { signedSession, media, png, imageFixture } from './web-fixtures.mjs';
const hook = registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL?.includes('/src/') && specifier.startsWith('.') && !specifier.endsWith('.ts')) return { url: new URL(specifier + '.ts', context.parentURL).href, shortCircuit: true };
  return nextResolve(specifier, context);
} });
const api = await import('../src/api.ts'); const { AccountMediaCache } = await import('../src/mediaCache.ts');
const { networkMonitor: monitor } = await import('../src/network/runtime.ts');
const runtime = await import('../src/network/mediaRuntime.ts'); hook.deregister();
const original = { fetch: globalThis.fetch, indexedDB: globalThis.indexedDB, IDBKeyRange: globalThis.IDBKeyRange };
const caches = [];
afterEach(() => { for (const cache of caches.splice(0)) cache.dispose(); api.installSession(null); Object.assign(globalThis, original); });
const details = () => { api.installSession(signedSession()); monitor.setDetailed(true); };
const metadata = { protocol: 'http', event: 'http.request', status: 'completed' };
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };
function memoryDatabase(seed = [], quota = false) {
  const records = new Map(seed.map(item => [item.accountId + '/' + item.sha256, item]));
  globalThis.IDBKeyRange = { only: value => value };
  globalThis.indexedDB = { open() {
    const open = {}; queueMicrotask(() => { open.result = { close() {}, transaction() {
      let pending = 0, ended = false;
      const tx = { abort() { if (!ended) { ended = true; queueMicrotask(() => tx.onabort?.()); } }, objectStore: () => store };
      const complete = () => queueMicrotask(() => { if (!pending && !ended) { ended = true; tx.oncomplete?.(); } });
      const request = result => { const value = { result }; pending++; queueMicrotask(() => { if (!ended) value.onsuccess?.(); pending--; complete(); }); return value; };
      const cursor = account => {
        const rows = [...records.values()].filter(item => item.accountId === account); let i = 0; const value = {}; pending++;
        const next = () => queueMicrotask(() => {
          if (ended) return;
          const row = rows[i++]; value.result = row ? { value: row, delete: () => records.delete(row.accountId + '/' + row.sha256), continue: next } : null;
          value.onsuccess?.(); if (!row) { pending--; complete(); }
        }); next(); return value;
      };
      const store = {
        get: key => request(records.get(key.join('/'))), delete: key => { records.delete(key.join('/')); return request(undefined); },
        put: item => { if (quota) throw new DOMException('isolated quota', 'QuotaExceededError'); records.set(item.accountId + '/' + item.sha256, item); return request([item.accountId, item.sha256]); },
        index: () => ({ getAll: account => request([...records.values()].filter(item => item.accountId === account)), openCursor: cursor }),
      };
      return tx;
    } }; open.onsuccess?.(); }); return open;
  } };
  return records;
}
function cacheFor(assets) { const cache = new AccountMediaCache('a'); caches.push(cache); cache.setReferences(assets); return cache; }
function loaded(cache, asset) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { stop(); reject(new Error('media load did not settle')); }, 2000);
    const check = () => { const view = cache.snapshot(asset.sha256); if (view.status !== 'loading' && view.status !== 'idle') { clearTimeout(timer); stop(); resolve(view); } };
    const stop = cache.subscribe(check); check();
  });
}
const parentEvents = () => monitor.snapshot().events.filter(event => event.origin === 'client' && event.event === 'media.read');

test('a real AccountMediaCache cache hit reports one verified parent and actual cache attempt without HTTP', async () => {
  details(); const asset = media(); memoryDatabase([{ accountId: 'a', sha256: asset.sha256, blob: new Blob([png], { type: asset.mimeType }), size: png.length, accessedAt: 0 }]);
  let requests = 0; globalThis.fetch = async () => { requests++; throw new Error('cache hit must not fetch'); };
  const cache = cacheFor([asset]), release = cache.acquire(asset); const view = await loaded(cache, asset);
  assert.equal(view.source, 'cache'); assert.equal(requests, 0);
  assert.deepEqual(parentEvents().map(event => event.status), ['started', 'completed']);
  assert.equal(parentEvents()[0].protocol, 'media'); assert.equal(parentEvents().at(-1).protocol, 'cache');
  const terminal = parentEvents().at(-1); assert.equal(terminal.bytes, png.length); assert.equal(terminal.verifiedBytes, png.length); assert.equal(terminal.expectedBytes, asset.size); assert.equal(terminal.verified, true);
  const attempt = monitor.snapshot().events.find(event => event.event === 'media.attempt' && event.status === 'completed');
  assert.equal(attempt.parentOperationId, terminal.operationId); assert.equal(attempt.traceId, terminal.traceId); assert.equal(attempt.route, 'cache');
  assert.equal(monitor.snapshot().metrics.activeOperations, 0); release();
});

test('corrupt cache and partial S3 failure keep actual failed bytes before one verified server fallback', async () => {
  details(); const asset = media(); memoryDatabase([{ accountId: 'a', sha256: asset.sha256, blob: new Blob([new Uint8Array(png.length)], { type: asset.mimeType }), size: png.length, accessedAt: 0 }]);
  const headers = []; let serverReads = 0;
  globalThis.fetch = async (path, options) => {
    headers.push({ path, values: options.headers });
    if (path.endsWith('/download')) return Response.json({ ...asset, url: 'https://bucket.s3.amazonaws.com/isolated?signature=secret', expiresAt: Date.now() + 600000, transport: 's3' });
    if (path.startsWith('https://')) return new Response(png.subarray(0, 5), { headers: { 'Content-Type': 'image/png' } });
    serverReads++; return new Response(png, { headers: { 'Content-Type': 'image/png' } });
  };
  const cache = cacheFor([asset]), release = cache.acquire(asset); const view = await loaded(cache, asset);
  assert.equal(view.source, 'server'); assert.equal(serverReads, 1); assert.equal(parentEvents().at(-1).verified, true);
  const attempts = monitor.snapshot().events.filter(event => event.event === 'media.attempt' && event.status !== 'started');
  assert.deepEqual(attempts.map(event => [event.route, event.status, event.bytes]), [['cache', 'failed', png.length], ['direct_s3', 'failed', 5], ['server_proxy', 'completed', png.length]]);
  assert.equal(parentEvents().at(-1).bytes, png.length * 2 + 5);
  assert.equal(parentEvents().at(-1).verifiedBytes, png.length); assert.notEqual(parentEvents().at(-1).bytes, parentEvents().at(-1).verifiedBytes);
  const trace = parentEvents()[0].traceId; for (const item of headers) assert.equal(item.values.get('X-Keepsite-Trace-Id'), item.path.startsWith('https://') ? null : trace);
  assert.doesNotMatch(JSON.stringify(monitor.exportSnapshot(monitor.exportLease())), /signature|bucket|[a-f0-9]{64}/);
  assert.equal(monitor.snapshot().metrics.activeOperations, 0); release();
});

test('two concurrent actual media loads keep distinct parent traces and quota does not erase verified delivery', async () => {
  details(); memoryDatabase([], true); const assets = [imageFixture(31), imageFixture(32)]; const traces = new Map();
  globalThis.fetch = async (path, options) => {
    const fixture = assets.find(item => path.includes(item.asset.assetId)); assert.ok(fixture);
    const values = traces.get(fixture.asset.assetId) ?? new Set(); values.add(options.headers.get('X-Keepsite-Trace-Id')); traces.set(fixture.asset.assetId, values);
    return path.endsWith('/download') ? Response.json({ ...fixture.asset, url: fixture.asset.url, expiresAt: Date.now() + 600000, transport: 'server' }) : new Response(fixture.bytes, { headers: { 'Content-Type': 'image/png' } });
  };
  const cache = cacheFor(assets.map(item => item.asset)), releases = assets.map(item => cache.acquire(item.asset));
  const views = await Promise.all(assets.map(item => loaded(cache, item.asset))); assert.ok(views.every(view => view.status === 'quota'));
  const completed = parentEvents().filter(event => event.status === 'completed'); assert.equal(completed.length, 2); assert.ok(completed.every(event => event.verified));
  assert.notEqual(completed[0].traceId, completed[1].traceId); assert.ok([...traces.values()].every(values => values.size === 1));
  assert.equal(monitor.snapshot().metrics.activeOperations, 0); for (const release of releases) release();
});

test('aborting an actual media resource does not finish it or start fallback under a replacement account', async () => {
  details(); memoryDatabase(); const asset = media(), cache = cacheFor([asset]); let signal, resolve;
  globalThis.fetch = async (_path, options) => { signal = options.signal; return new Promise(yes => { resolve = yes; }); };
  const release = cache.acquire(asset); await settle(); release(); assert.equal(signal.aborted, true);
  api.installSession(signedSession('b')); resolve(Response.json({ ...asset, url: asset.url, expiresAt: Date.now() + 600000, transport: 'server' })); await settle();
  assert.equal(monitor.snapshot().events.length, 0); assert.equal(monitor.snapshot().metrics.activeOperations, 0);
});

test('media observer update/record faults never consume slots and old history cannot regain trace headers', () => {
  details(); const scope = runtime.mediaScope();
  for (const method of ['update', 'record']) {
    const originalMethod = monitor[method]; monitor[method] = () => { throw new Error('observer only'); };
    try { for (let i = 0; i < 40; i++) { const parent = runtime.startMedia(scope, 12, new AbortController().signal); const attempt = runtime.startAttempt(parent, 's3', 'direct_s3'); assert.ok(parent?.operation); assert.ok(attempt?.operation); runtime.attemptBytes(attempt, 4); runtime.finishAttempt(attempt, 'failed'); runtime.finishMedia(parent, 'failed'); assert.equal(monitor.snapshot().metrics.activeOperations, 0); } }
    finally { monitor[method] = originalMethod; }
  }
  const parent = runtime.startMedia(scope, 12, new AbortController().signal); monitor.clearHistory();
  const attempt = runtime.startAttempt(parent, 'http', 'server_proxy'); assert.equal(attempt.operation, undefined);
  runtime.attemptBytes(attempt, 12); runtime.finishAttempt(attempt, 'completed', undefined, true); runtime.finishMedia(parent, 'completed');
  assert.equal(monitor.snapshot().events.length, 0); assert.equal(monitor.traceContext(parent.operation), null);
});

test('peer retirement cannot clear a cloud-owned card or publish stale selected-route completion', () => {
  details(); const parent = runtime.startMedia(runtime.mediaScope(), 12, new AbortController().signal), peerScope = monitor.replaceConnection('peer');
  const peer = runtime.startAttempt(parent, 'p2p', 'p2p_direct', peerScope); monitor.replaceConnection('peer');
  const cloud = runtime.startAttempt(parent, 'http', 'server_proxy'); runtime.attemptBytes(cloud, 12); runtime.publishAttempt(cloud);
  runtime.finishAttempt(peer, 'completed', undefined, true); assert.equal(parent.route, 'unknown'); assert.equal(monitor.snapshot().cards.transfer.route, 'server_proxy');
  monitor.replaceConnection('peer'); assert.equal(monitor.snapshot().cards.transfer.route, 'server_proxy');
  runtime.finishAttempt(cloud, 'completed', undefined, true); runtime.finishMedia(parent, 'completed');
  assert.equal(monitor.snapshot().cards.transfer.route, 'server_proxy'); assert.equal(monitor.snapshot().metrics.activeOperations, 0);
});

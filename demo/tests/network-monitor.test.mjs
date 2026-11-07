import { test } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
const hook = registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL?.includes('/src/network/') && specifier.startsWith('.') && !specifier.endsWith('.ts')) return { url: new URL(specifier + '.ts', context.parentURL).href, shortCircuit: true };
  return nextResolve(specifier, context);
} });
const { NetworkMonitor } = await import('../src/network/monitor.ts');
const { LIMITS } = await import('../src/network/schema.ts'); hook.deregister();
function setup() {
  let time = 100, ids = 0;
  const monitor = new NetworkMonitor({ monotonic: () => ++time, wall: () => 1700000000000 + time,
    id: () => (++ids).toString(16).padStart(32, '0'), clockId: 'f'.repeat(32) });
  monitor.startAccount();
  return monitor;
}
const metadata = { protocol: 'http', event: 'http.request', status: 'completed', template: 'tickets' };
const tick = () => new Promise(resolve => queueMicrotask(resolve));

test('default lightweight mode updates cards without collecting history or trace headers', () => {
  const monitor = setup(), scope = monitor.scope('http'), operation = monitor.beginOperation(scope, 'request');
  monitor.update(scope, 'api', { state: 'reachable', auth: 'required', status: 401, durationMs: 34 });
  assert.equal(monitor.record(scope, metadata, operation), false);
  assert.equal(monitor.traceContext(operation), null);
  assert.deepEqual(monitor.snapshot().events, []);
  assert.equal(monitor.snapshot().cards.api.status, 401);
  assert.equal(monitor.snapshot().cards.api.state, 'reachable');
});

test('state and exports discard sensitive fields, non-enumerated values and hostile accessors', () => {
  const monitor = setup(); monitor.setDetailed(true);
  const scope = monitor.scope('http');
  const secrets = ['person@example.test', 'Bearer secret', 'https://private.invalid/path?signature=secret', 'v=0 secret SDP', '192.0.2.1', 'a'.repeat(64)];
  let invoked = 0;
  const input = { ...metadata, url: secrets[2], email: secrets[0], error: secrets[1], payload: secrets[3], ip: secrets[4], assetId: secrets[5], errorCode: secrets[1], bytes: Infinity };
  Object.defineProperty(input, 'durationMs', { get() { invoked++; throw new Error(secrets[0]); } });
  assert.equal(monitor.record(scope, input), true);
  assert.equal(monitor.record(scope, { ...metadata, event: secrets[3] }), false);
  monitor.update(scope, 'api', { state: secrets[0], status: NaN, durationMs: -1, headers: secrets[1], url: secrets[2] });
  assert.equal(monitor.update(scope, '__proto__', input), false);
  assert.equal(monitor.record(new Proxy({}, { getOwnPropertyDescriptor() { throw new Error('hostile'); } }), input), false);
  const output = JSON.stringify(monitor.exportSnapshot(monitor.exportLease()));
  for (const secret of secrets) assert.ok(!output.includes(secret));
  assert.equal(invoked, 0);
  assert.equal(monitor.snapshot().cards.api.state, null);
  assert.equal(monitor.snapshot().cards.api.status, null);
  assert.equal(monitor.snapshot().metrics.rejected, 1);
});

test('finite counts and route enums survive while invalid measurements stay unknown', () => {
  const monitor = setup(), peer = monitor.scope('peer'); monitor.setDetailed(true);
  monitor.update(peer, 'peer', { route: 'p2p_unknown', localType: 'host', remoteType: 'invented',
    roundTripMs: Infinity, channelBytesSent: 4.5, channelBytesReceived: 512, stunConfigured: false });
  const card = monitor.snapshot().cards.peer;
  assert.equal(card.route, 'p2p_unknown'); assert.equal(card.localType, 'host'); assert.equal(card.remoteType, null);
  assert.equal(card.roundTripMs, null); assert.equal(card.channelBytesSent, null); assert.equal(card.channelBytesReceived, 512);
  assert.equal(card.stunConfigured, false);
  assert.equal(monitor.update(peer, 'api', { state: 'offline' }), false);
});

test('event history and encoded bytes stay bounded under a long capture', () => {
  const monitor = setup(); monitor.setDetailed(true); const scope = monitor.scope('http');
  for (let index = 0; index < 4000; index++) monitor.record(scope, { ...metadata, bytes: index, durationMs: index });
  const state = monitor.snapshot(), bytes = state.events.map(event => new TextEncoder().encode(JSON.stringify(event)).byteLength);
  assert.equal(state.events.length, LIMITS.events);
  assert.ok(bytes.every(size => size <= LIMITS.eventBytes));
  assert.ok(bytes.reduce((sum, size) => sum + size, 0) <= LIMITS.bytes);
  assert.equal(state.metrics.retainedBytes, bytes.reduce((sum, size) => sum + size, 0));
  assert.equal(state.metrics.recorded, 4000); assert.equal(state.metrics.dropped, 3800);
  assert.equal(state.events.at(-1).sequence, 4000);
});

test('operation and device registries cannot grow with completed work or peer churn', () => {
  const monitor = setup(); monitor.setDetailed(true); const scope = monitor.scope('media');
  const handles = Array.from({ length: LIMITS.active }, () => monitor.beginOperation(scope, 'media'));
  assert.equal(monitor.beginOperation(scope, 'media'), undefined);
  const devices = Array.from({ length: LIMITS.devices }, () => monitor.device(scope));
  assert.equal(monitor.device(scope), undefined);
  for (const handle of handles) monitor.finishOperation(scope, handle, { protocol: 'cache', event: 'media.read', status: 'completed', verified: true });
  for (const device of devices) monitor.releaseDevice(device);
  for (let i = 0; i < 500; i++) {
    const handle = monitor.beginOperation(scope, 'media');
    monitor.finishOperation(scope, handle, { ...metadata, status: 'failed' });
    assert.equal(monitor.record(scope, metadata, handle), false);
  }
  assert.equal(monitor.snapshot().metrics.activeOperations, 0); assert.equal(monitor.snapshot().metrics.deviceAliases, 0);
});

test('account replacement rejects prior HTTP, connection, operation and export leases', () => {
  const monitor = setup(); monitor.setDetailed(true);
  const old = monitor.scope('http'), handle = monitor.beginOperation(old, 'request'), lease = monitor.exportLease();
  monitor.record(old, metadata, handle); const clock = monitor.snapshot().clockId;
  monitor.startAccount(); monitor.setDetailed(true);
  assert.equal(monitor.update(old, 'api', { state: 'offline' }), false);
  assert.equal(monitor.record(old, metadata), false);
  assert.equal(monitor.finishOperation(old, handle, metadata), false);
  assert.equal(monitor.exportSnapshot(lease), null);
  assert.equal(monitor.snapshot().events.length, 0);
  assert.equal(monitor.snapshot().clockId, clock);
  monitor.stopAccount();
  assert.equal(monitor.snapshot().active, false); assert.equal(monitor.scope('http'), undefined);
  assert.equal(monitor.snapshot().events.length, 0);
});

test('same-account reconnect invalidates old connection observers without invalidating unrelated HTTP', () => {
  const monitor = setup(); monitor.setDetailed(true);
  const old = monitor.scope('ws'), http = monitor.scope('http'), peer = monitor.scope('peer');
  monitor.update(old, 'websocket', { state: 'ready' });
  const current = monitor.replaceConnection('ws');
  assert.equal(monitor.update(old, 'websocket', { state: 'closed' }), false);
  assert.equal(monitor.record(old, { protocol: 'ws', event: 'ws.message', status: 'completed' }), false);
  assert.equal(monitor.update(current, 'websocket', { state: 'connecting' }), true);
  assert.equal(monitor.update(http, 'api', { state: 'reachable' }), true);
  assert.equal(monitor.update(peer, 'peer', { state: 'idle' }), true);
});

test('clear and detail rotations invalidate in-flight history, headers and asynchronous exports', () => {
  const monitor = setup(); monitor.setDetailed(true);
  const scope = monitor.scope('media'), operation = monitor.beginOperation(scope, 'media');
  const initial = monitor.traceContext(operation), lease = monitor.exportLease();
  assert.match(initial.captureId, /^[a-f0-9]{32}$/); assert.match(initial.traceId, /^[a-f0-9]{32}$/);
  monitor.record(scope, metadata, operation); monitor.clearHistory();
  assert.equal(monitor.traceContext(operation), null);
  assert.equal(monitor.finishOperation(scope, operation, metadata), false);
  assert.equal(monitor.exportSnapshot(lease), null); assert.equal(monitor.snapshot().events.length, 0);
  const next = monitor.beginOperation(scope, 'media'), context = monitor.traceContext(next);
  assert.notEqual(context.captureId, initial.captureId); assert.notEqual(context.traceId, initial.traceId);
  const onLease = monitor.exportLease(); monitor.setDetailed(false);
  assert.equal(monitor.traceContext(next), null); assert.equal(monitor.exportSnapshot(onLease), null);
  assert.equal(monitor.record(scope, metadata, next), false);
  monitor.setDetailed(true); assert.notEqual(monitor.snapshot().captureId, context.captureId);
});

test('pause freezes history, keeps status fresh, and resumes without resurrecting pre-pause operations', () => {
  const monitor = setup(); monitor.setDetailed(true); const scope = monitor.scope('http');
  const operation = monitor.beginOperation(scope, 'request');
  monitor.record(scope, metadata, operation); monitor.setPaused(true);
  assert.equal(monitor.snapshot().detailed, true); assert.equal(monitor.snapshot().paused, true);
  assert.equal(monitor.traceContext(operation), null);
  monitor.update(scope, 'api', { state: 'offline' });
  assert.equal(monitor.snapshot().cards.api.state, 'offline');
  assert.equal(monitor.record(scope, metadata), false);
  assert.equal(monitor.snapshot().events.length, 1);
  monitor.setPaused(false); assert.equal(monitor.finishOperation(scope, operation, metadata), false);
  monitor.record(scope, metadata, monitor.beginOperation(scope, 'request'));
  assert.equal(monitor.snapshot().events.length, 2);
});

test('snapshots are stable and immutable; observer failure is isolated and notifications coalesce', async () => {
  const monitor = setup(); await tick(); let notified = 0;
  const unsubscribe = monitor.subscribe(() => { notified++; throw new Error('subscriber failure'); });
  const scope = monitor.scope('http'); monitor.setDetailed(true);
  monitor.update(scope, 'api', { state: 'reachable' }); monitor.record(scope, metadata);
  const state = monitor.snapshot(); assert.equal(monitor.snapshot(), state);
  assert.throws(() => { state.cards.api.state = 'offline'; });
  assert.throws(() => { state.events[0].protocol = 'secret'; });
  assert.throws(() => { state.events.push({}); });
  await tick(); assert.equal(notified, 1); unsubscribe();
  monitor.update(scope, 'api', { status: 200 }); await tick(); assert.equal(notified, 1);
  assert.notEqual(monitor.snapshot(), state); assert.equal(monitor.snapshot().events[0].protocol, 'http');
});

test('unavailable clock or randomness stays unknown and never fabricates trace identifiers', () => {
  const monitor = new NetworkMonitor({ id: () => { throw new Error('no entropy'); }, monotonic: () => NaN, clockId: null });
  assert.equal(monitor.startAccount(), false); assert.equal(monitor.scope('http'), undefined);
  assert.equal(monitor.snapshot().clockId, null); assert.equal(monitor.snapshot().captureId, null);
  const partial = setup(); partial.setDetailed(true);
  assert.ok(partial.snapshot().metrics.captureCostMs >= 0);
});

test('account reopening preserves the clock domain offset instead of restarting it', () => {
  let mono = 1000, ids = 0;
  const monitor = new NetworkMonitor({ monotonic: () => mono, wall: () => 1700000000000 + mono,
    id: () => (++ids).toString(16).padStart(32, '0'), clockId: 'e'.repeat(32) });
  monitor.startAccount(); monitor.setDetailed(true);
  monitor.record(monitor.scope('http'), metadata);
  const first = monitor.exportSnapshot(monitor.exportLease());
  assert.equal(first.events[0].offsetMs, 1000);
  mono = 1250;
  monitor.startAccount(); monitor.setDetailed(true);
  monitor.record(monitor.scope('http'), metadata);
  const reopened = monitor.exportSnapshot(monitor.exportLease());
  assert.equal(reopened.clockId, first.clockId);
  assert.notEqual(reopened.sessionId, first.sessionId);
  assert.equal(reopened.events.length, 1);
  assert.equal(reopened.events[0].offsetMs, 1250);
  assert.equal(first.events[0].offsetMs, 1000);
});

test('monitors created at different times export identical offsets for one clock domain', () => {
  let mono = 40, ids = 0;
  const dependencies = { monotonic: () => mono, wall: () => 1700000000000 + mono,
    id: () => (++ids).toString(16).padStart(32, '0'), clockId: 'd'.repeat(32) };
  const first = new NetworkMonitor(dependencies); first.startAccount(); first.setDetailed(true);
  mono = 80;
  const second = new NetworkMonitor(dependencies); second.startAccount(); second.setDetailed(true);
  mono = 120;
  first.record(first.scope('http'), metadata); second.record(second.scope('http'), metadata);
  const a = first.exportSnapshot(first.exportLease()), b = second.exportSnapshot(second.exportLease());
  assert.equal(a.clockId, b.clockId);
  assert.equal(a.events[0].offsetMs, 120);
  assert.equal(b.events[0].offsetMs, 120);
  assert.equal(a.events[0].offsetMs, b.events[0].offsetMs);
});

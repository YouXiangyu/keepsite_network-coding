import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { signedSession } from './web-fixtures.mjs';
const hook = registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL?.includes('/src/') && specifier.startsWith('.') && !specifier.endsWith('.ts')) return { url: new URL(specifier + '.ts', context.parentURL).href, shortCircuit: true };
  return nextResolve(specifier, context);
} });
const api = await import('../src/api.ts'); const runtime = await import('../src/network/runtime.ts');
const { networkMonitor: monitor } = runtime; hook.deregister();
const original = { WebSocket: globalThis.WebSocket, window: globalThis.window }; const sockets = [], connections = [];
class Socket extends EventTarget {
  static OPEN = 1; readyState = 1; bufferedAmount = 0; sent = [];
  constructor() { super(); sockets.push(this); }
  send(body) { this.sent.push(body); }
  close() { this.readyState = 3; }
}
afterEach(() => { for (const connection of connections.splice(0)) connection.stop(); api.installSession(null); Object.assign(globalThis, original); sockets.length = 0; });
const bytes = value => Buffer.byteLength(typeof value === 'string' ? value : JSON.stringify(value));
function setup(version = 1) {
  api.installSession(signedSession()); monitor.setDetailed(true); globalThis.WebSocket = Socket; globalThis.window = { location: { origin: 'https://keepsite.example' } };
  const observation = runtime.startSocket(); const connection = api.openSyncEvents(() => {}, () => {}, observation); connections.push(connection);
  runtime.socketOpen(observation); runtime.socketReady(observation, 15000, version);
  const operation = monitor.beginOperation(monitor.scope('peer'), 'peer'); return { observation, connection, socket: sockets.at(-1), operation };
}
const message = { v: 1, type: 'device.list', requestId: 'isolated-rpc' };

test('only current ready capability and a live owner add the collector context, with actual final frame bytes', () => {
  const h = setup(); const second = monitor.beginOperation(monitor.scope('peer'), 'peer'); const counts = [];
  assert.equal(h.connection.send(message, { operation: h.operation, onBytes: value => counts.push(value) }), true);
  assert.equal(h.connection.send({ ...message, requestId: 'other' }, { operation: second, onBytes: value => counts.push(value) }), true);
  const parsed = h.socket.sent.map(body => JSON.parse(body));
  assert.equal(parsed[0].diagnostics.captureId, monitor.snapshot().captureId); assert.notEqual(parsed[0].diagnostics.traceId, parsed[1].diagnostics.traceId);
  assert.deepEqual(Object.keys(parsed[0].diagnostics).sort(), ['captureId', 'traceId']);
  assert.deepEqual(counts, h.socket.sent.map(body => bytes(body)));
});

test('old or absent capabilities, pause, clear, detail off and stale connection never attach diagnostics', () => {
  const h = setup(null);
  runtime.socketReady(h.observation, undefined, undefined);
  assert.equal(h.connection.send({ ...message, diagnostics: { captureId: 'private injected', traceId: 'x' } }, { operation: h.operation }), true);
  assert.equal(JSON.parse(h.socket.sent.at(-1)).diagnostics, undefined);
  runtime.socketReady(h.observation, 15000, 1); monitor.setPaused(true); h.connection.send(message, { operation: h.operation }); assert.equal(JSON.parse(h.socket.sent.at(-1)).diagnostics, undefined);
  monitor.setPaused(false); const fresh = monitor.beginOperation(monitor.scope('peer'), 'peer'); monitor.clearHistory();
  h.connection.send(message, { operation: fresh }); assert.equal(JSON.parse(h.socket.sent.at(-1)).diagnostics, undefined);
  const current = monitor.beginOperation(monitor.scope('peer'), 'peer'); monitor.setDetailed(false);
  h.connection.send(message, { operation: current }); assert.equal(JSON.parse(h.socket.sent.at(-1)).diagnostics, undefined);
  monitor.setDetailed(true); const currentAgain = monitor.beginOperation(monitor.scope('peer'), 'peer'); runtime.startSocket();
  h.connection.send(message, { operation: currentAgain }); assert.equal(JSON.parse(h.socket.sent.at(-1)).diagnostics, undefined);
});

test('actual UTF-8 offer boundary omits only diagnostics while preserving the original legal frame', () => {
  const h = setup(); const offer = { v: 1, type: 'peer.signal', requestId: 'r', transferId: 't', kind: 'offer', payload: { type: 'offer', sdp: '界' } };
  const overhead = bytes({ ...offer, payload: { type: 'offer', sdp: '' } });
  // Quotes consume two serialized bytes while CJK proves UTF-8 differs from character count.
  offer.payload.sdp = '界'.repeat(1000) + '"'.repeat(Math.floor((65536 - overhead - 3000) / 2));
  assert.ok(Buffer.byteLength(offer.payload.sdp) <= 49152); assert.ok(bytes(offer) <= 65536); assert.ok(bytes(offer) > 65400);
  assert.ok(JSON.stringify(offer).length < bytes(offer));
  let actual;
  assert.equal(h.connection.send(offer, { operation: h.operation, onBytes: value => { actual = value; } }), true);
  assert.deepEqual(JSON.parse(h.socket.sent.at(-1)), offer); assert.equal(actual, bytes(offer)); assert.equal(h.socket.sent.length, 1);
});

test('buffer budget drops optional diagnostics, exact base still sends once, and overflowing base stays rejected', () => {
  const h = setup(); h.socket.bufferedAmount = 256 * 1024 - bytes(message); const counts = [];
  assert.equal(h.connection.send(message, { operation: h.operation, onBytes: value => counts.push(value) }), true);
  assert.deepEqual(JSON.parse(h.socket.sent[0]), message); assert.deepEqual(counts, [bytes(message)]);
  h.socket.bufferedAmount++; assert.equal(h.connection.send(message, { operation: h.operation }), false); assert.equal(h.socket.sent.length, 1);
});

test('diagnostic serialization or byte-observer failure never changes one successful business send', () => {
  const h = setup(), stringify = JSON.stringify;
  try {
    JSON.stringify = value => { if (value && Object.hasOwn(value, 'diagnostics')) throw new Error('diagnostic formatter failed'); return stringify(value); };
    assert.equal(h.connection.send(message, { operation: h.operation }), true);
  } finally { JSON.stringify = stringify; }
  assert.deepEqual(JSON.parse(h.socket.sent[0]), message);
  assert.equal(h.connection.send(message, { operation: h.operation, onBytes() { throw new Error('observer only'); } }), true);
  assert.equal(h.socket.sent.length, 2);
});

test('optional metadata filtering cannot remove other unknown business fields or mutate their wire shape', () => {
  const h = setup(null), input = JSON.parse('{"v":1,"type":"device.list","requestId":"r","__proto__":{"invalid":"field"},"diagnostics":{"private":"discard"}}');
  assert.equal(h.connection.send(input, { operation: h.operation }), true);
  const sent = JSON.parse(h.socket.sent[0]); assert.equal(Object.hasOwn(sent, '__proto__'), true); assert.deepEqual(sent.__proto__, { invalid: 'field' });
  assert.equal(sent.diagnostics, undefined);
});

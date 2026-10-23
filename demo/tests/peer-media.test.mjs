import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { signedSession, media, png } from './web-fixtures.mjs';
const hook = registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL?.includes('/src/') && specifier.startsWith('.') && !specifier.endsWith('.ts')) return { url: new URL(specifier + '.ts', context.parentURL).href, shortCircuit: true };
  return nextResolve(specifier, context);
} });
const api = await import('../src/api.ts'); const { PeerMedia } = await import('../src/media/peerMedia.ts'); hook.deregister();
const originalRTC = globalThis.RTCPeerConnection; const peers = [];
afterEach(() => { for (const peer of peers.splice(0)) peer.stop(); api.installSession(null); globalThis.RTCPeerConnection = originalRTC; });
const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); };
function setup(overrides = {}) {
  api.installSession(signedSession()); globalThis.RTCPeerConnection = class {};
  const messages = [], eventListeners = new Set(), connectionListeners = new Set(), inventoryListeners = new Set();
  const state = { connected: true, serverId: 'server', generation: 1 };
  let revision = 0, count = 0;
  const emit = message => { for (const listener of eventListeners) listener({ v: 1, serverId: 'server', userId: 'a', ...message }); };
  const port = { eventState: () => state, subscribeConnection: fn => { connectionListeners.add(fn); return () => connectionListeners.delete(fn); }, subscribeEvents: fn => { eventListeners.add(fn); return () => eventListeners.delete(fn); }, sendEvent: message => {
    messages.push(message); queueMicrotask(() => {
      if (overrides.respond?.(message, emit) === false) return;
      let reply;
      if (message.type === 'device.register') { if (!overrides.reuseRevision) revision = 0; reply = { type: 'device.registered', peerId: 'peer', enabled: true, reason: '', heartbeatMs: 15000, leaseMs: 45000, inventoryRevision: revision, limits: { maxInventory: 1024, maxBatchAssets: 32 }, iceServers: [], turnEnabled: false }; }
      else if (message.type === 'device.list') reply = { type: 'device.peers', peers: [{ peerId: 'peer', name: 'browser' }] };
      else if (message.type === 'device.heartbeat') reply = { type: 'device.alive', leaseMs: 45000, inventoryRevision: revision };
      else if (message.type === 'cache.replace') { if (message.batchIndex === 0) count = 0; count += message.assetIds.length; const complete = message.batchIndex === message.batchCount - 1; if (complete) revision = message.revision; reply = { type: 'cache.ack', revision: message.revision, count, complete, nextBatchIndex: complete ? null : message.batchIndex + 1 }; }
      else if (message.type === 'cache.patch') { revision = message.revision; count += message.add.length - message.remove.length; reply = { type: 'cache.ack', revision, count, complete: true, nextBatchIndex: null }; }
      else if (message.type === 'peer.find') reply = { type: 'peer.sources', assetId: message.assetId, peers: [] };
      else if (message.type === 'device.unregister') reply = { type: 'device.unregistered' };
      if (reply) emit({ ...reply, requestId: message.requestId });
    }); return true;
  } };
  const cache = { listShareable: async () => overrides.inventory?.() ?? [], readForPeer: async (...args) => overrides.read?.(...args) ?? null, canShare: () => true, subscribeInventory: fn => { inventoryListeners.add(fn); return () => inventoryListeners.delete(fn); } };
  const peer = new PeerMedia(port, cache); peers.push(peer); peer.setSupported(true); peer.start();
  return { peer, messages, emit, changeConnection: patch => { Object.assign(state, patch); for (const listener of connectionListeners) listener(); }, inventoryChanged: () => { for (const listener of inventoryListeners) listener(); } };
}

test('Redis-disabled registration immediately keeps media on the cloud path', async () => {
  const h = setup({ respond(message, emit) { if (message.type === 'device.register') { emit({ type: 'device.registered', requestId: message.requestId, peerId: null, enabled: false, reason: 'Redis unavailable', heartbeatMs: 15000, leaseMs: 45000, inventoryRevision: 0, iceServers: [], turnEnabled: false }); return false; } } });
  await settle(); assert.equal(h.peer.snapshot().enabled, false); assert.equal(await h.peer.tryDownload(media(), new AbortController().signal), null);
  assert.equal(h.messages.some(message => message.type === 'peer.find'), false);
});

test('cache manifests are batched in order and removals use the next committed revision', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  let entries = Array.from({ length: 65 }, (_, index) => ({ ...media(), assetId: index.toString(16).padStart(64, '0') }));
  const h = setup({ inventory: () => entries }); await settle(); context.mock.timers.tick(51); await settle();
  const batches = h.messages.filter(message => message.type === 'cache.replace'); assert.deepEqual(batches.map(message => message.assetIds.length), [32, 32, 1]);
  assert.deepEqual(batches.map(message => message.revision), [1, 1, 1]); assert.equal(h.peer.snapshot().inventoryCount, 65);
  entries = entries.slice(1); h.inventoryChanged(); context.mock.timers.tick(51); await settle();
  const patch = h.messages.find(message => message.type === 'cache.patch'); assert.equal(patch.revision, 2); assert.equal(patch.remove.length, 1); assert.equal(h.peer.snapshot().inventoryCount, 64);
});

test('two active P2P requests share the page limit and a third immediately uses cloud', async () => {
  const h = setup({ respond: message => message.type === 'peer.find' ? false : undefined }); await settle();
  const first = new AbortController(), second = new AbortController();
  const pending = [h.peer.tryDownload(media(), first.signal), h.peer.tryDownload(media(), second.signal)];
  assert.equal(await h.peer.tryDownload(media(), new AbortController().signal), null); assert.equal(h.messages.filter(message => message.type === 'peer.find').length, 2);
  first.abort(); second.abort(); await Promise.all(pending); assert.equal(h.peer.snapshot().receiving, 0);
});

test('disconnect cancels pending lookup and ignores its stale reply before re-registering', async () => {
  let request; const h = setup({ respond: message => { if (message.type === 'peer.find') { request = message; return false; } } }); await settle();
  const pending = h.peer.tryDownload(media(), new AbortController().signal); await settle(); h.changeConnection({ connected: false }); await pending;
  h.emit({ type: 'peer.sources', requestId: request.requestId, assetId: media().assetId, peers: [{ peerId: 'stale', name: 'stale' }] });
  assert.equal(h.peer.snapshot().receiving, 0); assert.equal(h.messages.some(message => message.type === 'peer.open'), false);
  h.changeConnection({ connected: true, generation: 2 }); await settle(); assert.equal(h.messages.filter(message => message.type === 'device.register').length, 2);
});

test('disabling P2P unregisters only the page and keeps the sync connection available', async () => {
  const h = setup(); await settle(); h.peer.setWanted(false);
  assert.equal(h.peer.snapshot().enabled, false); assert.equal(h.peer.snapshot().connected, true);
  assert.equal(h.messages.filter(message => message.type === 'device.unregister').length, 1);
  h.peer.setWanted(true); await settle(); assert.equal(h.peer.snapshot().enabled, true);
});

test('a source accepts the remaining pending lease rather than demanding the original full lease', async () => {
  let reads = 0; const h = setup({ read() { reads++; return null; } }); await settle();
  h.emit({ type: 'peer.request', transferId: 'transfer', requesterPeerId: 'requester', media: media(), leaseMs: 4992 }); await settle();
  assert.equal(reads, 1); assert.equal(h.peer.snapshot().sending, 0);
  assert.equal(h.messages.some(message => message.type === 'peer.cancel' && message.transferId === 'transfer'), true);
});

test('an unsolicited service-disable event cancels peer work without closing the sync port', async () => {
  const h = setup({ respond: message => message.type === 'peer.find' ? false : undefined }); await settle();
  const pending = h.peer.tryDownload(media(), new AbortController().signal); await settle();
  h.emit({ type: 'device.registered', enabled: false, peerId: null, reason: 'Redis lost state' });
  assert.equal(await pending, null); assert.equal(h.peer.snapshot().enabled, false); assert.equal(h.peer.snapshot().connected, true);
  assert.equal(h.peer.snapshot().reason, 'Redis lost state');
});

for (const reason of ['transport_error', 'expired', 'cancelled']) {
  test(`server ${reason} arriving before the channel close has deterministic retry behavior`, async () => {
    let opens = 0;
    const h = setup({ respond(message, emit) {
      if (message.type === 'peer.find') { emit({ type: 'peer.sources', requestId: message.requestId, assetId: message.assetId, peers: [{ peerId: 'source', name: 'source' }] }); return false; }
      if (message.type === 'peer.open') {
        const transferId = 'transfer-' + ++opens;
        emit({ type: 'peer.pending', requestId: message.requestId, transferId });
        setImmediate(() => emit({ type: 'peer.cancelled', transferId, reason: opens === 1 ? reason : 'expired' }));
        return false;
      }
    } });
    await settle(); assert.equal(await h.peer.tryDownload(media(), new AbortController().signal), null);
    assert.equal(opens, reason === 'transport_error' ? 2 : 1); assert.equal(h.peer.snapshot().receiving, 0);
  });
}

test('a discovery RPC rejection is not retried as a transport interruption', async () => {
  const h = setup({ respond(message, emit) { if (message.type === 'peer.find') { emit({ type: 'peer.error', requestId: message.requestId, code: 'unavailable', message: 'unavailable' }); return false; } } });
  await settle(); assert.equal(await h.peer.tryDownload(media(), new AbortController().signal), null);
  assert.equal(h.messages.filter(message => message.type === 'peer.find').length, 1);
});

test('asymmetric lease renewals use the returned remaining lease and remain live beyond ten seconds', async context => {
  context.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] }); let clock = 0, renewals = 0;
  context.mock.method(performance, 'now', () => clock);
  const advance = async milliseconds => { while (milliseconds > 0) { const step = Math.min(100, milliseconds); clock += step; milliseconds -= step; context.mock.timers.tick(step); await settle(); } };
  const h = setup({ inventory: () => [media()], read: () => new Blob(['x']), respond(message, emit) {
    if (message.type === 'peer.accept') { emit({ type: 'peer.authorized', requestId: message.requestId, transferId: message.transferId, media: media(), sourcePeerId: 'peer', requesterPeerId: 'requester', offererPeerId: 'requester', leaseMs: 9600 }); return false; }
    if (message.type === 'peer.renew') { emit({ type: 'peer.lease', requestId: message.requestId, transferId: message.transferId, leaseMs: ++renewals === 1 ? 4400 : 10000 }); return false; }
  } });
  globalThis.RTCPeerConnection = class { connectionState = 'new'; close() {} };
  await settle(); h.emit({ type: 'peer.request', transferId: 'lease-transfer', requesterPeerId: 'requester', media: media(), leaseMs: 4900 }); await settle();
  assert.equal(h.peer.snapshot().sending, 1, JSON.stringify(h.messages));
  await advance(4801); assert.equal(renewals, 1, JSON.stringify(h.messages));
  await advance(2300); assert.equal(renewals, 2);
  await advance(3900); assert.equal(h.peer.snapshot().sending, 1);
  assert.equal(h.messages.some(message => message.type === 'peer.cancel' && message.transferId === 'lease-transfer'), false);
  await advance(1000); assert.equal(h.peer.snapshot().sending, 0);
});

test('ICE emitted synchronously during setLocalDescription waits behind the offer', async () => {
  const h = setup({ respond(message, emit) {
    if (message.type === 'peer.find') { emit({ type: 'peer.sources', requestId: message.requestId, assetId: message.assetId, peers: [{ peerId: 'source', name: 'source' }] }); return false; }
    if (message.type === 'peer.open') { emit({ type: 'peer.pending', requestId: message.requestId, transferId: 'ice-transfer' }); setImmediate(() => emit({ type: 'peer.authorized', transferId: 'ice-transfer', media: media(), requesterPeerId: 'peer', sourcePeerId: 'source', offererPeerId: 'peer', leaseMs: 10000 })); return false; }
    if (message.type === 'peer.signal') {
      emit({ type: 'peer.signalled', requestId: message.requestId, transferId: 'ice-transfer' });
      if (message.kind === 'ice') setImmediate(() => emit({ type: 'peer.cancelled', transferId: 'ice-transfer', reason: 'cancelled' }));
      return false;
    }
  } });
  globalThis.RTCPeerConnection = class {
    connectionState = 'new'; close() {}
    createDataChannel() { const channel = new EventTarget(); channel.readyState = 'connecting'; return channel; }
    async createOffer() { return { type: 'offer', sdp: 'synthetic offer' }; }
    async setLocalDescription() { this.onicecandidate({ candidate: { toJSON: () => ({ candidate: 'synthetic candidate', sdpMid: '0', sdpMLineIndex: 0 }) } }); }
  };
  await settle(); await h.peer.tryDownload(media(), new AbortController().signal);
  assert.deepEqual(h.messages.filter(message => message.type === 'peer.signal').map(message => message.kind), ['offer', 'ice']);
});

test('lost heartbeat and unregister acknowledgments recover with a full replacement at the current revision', async context => {
  context.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] }); let entries = [media()], heartbeats = 0;
  const h = setup({ reuseRevision: true, inventory: () => entries, respond: message => {
    if (message.type === 'device.heartbeat' && ++heartbeats === 1 || message.type === 'device.unregister') return false;
  } });
  await settle(); context.mock.timers.tick(51); await settle(); assert.equal(h.peer.snapshot().inventoryCount, 1);
  entries = []; context.mock.timers.tick(15000); await settle(); context.mock.timers.tick(1001); await settle();
  assert.equal(h.peer.snapshot().enabled, false); assert.equal(h.messages.some(message => message.type === 'device.unregister'), true);
  context.mock.timers.tick(15000); await settle(); context.mock.timers.tick(51); await settle();
  assert.equal(h.peer.snapshot().enabled, true); assert.equal(h.peer.snapshot().inventoryCount, 0);
  const final = h.messages.filter(message => message.type === 'cache.replace').at(-1); assert.equal(final.revision, 2); assert.deepEqual(final.assetIds, []);
});

test('expiration while awaiting post-verification connection statistics cannot return bytes to the cache', async () => {
  let channel, statsStarted = false;
  const h = setup({ respond(message, emit) {
    if (message.type === 'peer.find') { emit({ type: 'peer.sources', requestId: message.requestId, assetId: message.assetId, peers: [{ peerId: 'source', name: 'source' }] }); return false; }
    if (message.type === 'peer.open') { emit({ type: 'peer.pending', requestId: message.requestId, transferId: 'stats-transfer' }); setImmediate(() => emit({ type: 'peer.authorized', transferId: 'stats-transfer', media: media(), requesterPeerId: 'peer', sourcePeerId: 'source', offererPeerId: 'peer', leaseMs: 10000 })); return false; }
    if (message.type === 'peer.signal') { emit({ type: 'peer.signalled', requestId: message.requestId, transferId: 'stats-transfer' }); setImmediate(() => { channel.readyState = 'open'; channel.dispatchEvent(new Event('open')); }); return false; }
  } });
  class Channel extends EventTarget {
    readyState = 'connecting';
    send(raw) {
      if (JSON.parse(raw).type !== 'get') return;
      queueMicrotask(() => {
        const frame = new ArrayBuffer(png.length + 8); new DataView(frame).setUint32(4, png.length); new Uint8Array(frame, 8).set(png);
        this.dispatchEvent(new MessageEvent('message', { data: frame }));
        this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ v: 1, type: 'end', size: png.length }) }));
      });
    }
  }
  globalThis.RTCPeerConnection = class {
    connectionState = 'new'; sctp = { maxMessageSize: 16384 }; close() {}
    createDataChannel() { channel = new Channel(); return channel; }
    async createOffer() { return { type: 'offer', sdp: 'synthetic offer' }; }
    async setLocalDescription() {}
    getStats() { statsStarted = true; setImmediate(() => h.emit({ type: 'peer.cancelled', transferId: 'stats-transfer', reason: 'expired' })); return new Promise(() => {}); }
  };
  await settle(); assert.equal(await h.peer.tryDownload(media(), new AbortController().signal), null); assert.equal(statsStarted, true);
  assert.equal(h.messages.filter(message => message.type === 'peer.open').length, 1);
});

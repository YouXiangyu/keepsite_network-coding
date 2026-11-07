import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { signedSession, media, png } from './web-fixtures.mjs';
const hook = registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL?.includes('/src/') && specifier.startsWith('.') && !specifier.endsWith('.ts')) return { url: new URL(specifier + '.ts', context.parentURL).href, shortCircuit: true };
  return nextResolve(specifier, context);
} });
const api = await import('../src/api.ts'); const { PeerMedia } = await import('../src/media/peerMedia.ts');
const { networkMonitor: monitor } = await import('../src/network/runtime.ts'); const mediaRuntime = await import('../src/network/mediaRuntime.ts'); hook.deregister();
const originalRTC = globalThis.RTCPeerConnection; const peers = [];
afterEach(() => { for (const peer of peers.splice(0)) peer.stop(); api.installSession(null); globalThis.RTCPeerConnection = originalRTC; });
const settle = async () => { for (let i = 0; i < 10; i++) await new Promise(resolve => setImmediate(resolve)); };
function setup(overrides = {}) {
  api.installSession(signedSession()); globalThis.RTCPeerConnection = class {};
  const messages = [], eventListeners = new Set(), connectionListeners = new Set(), inventoryListeners = new Set();
  const state = { connected: true, serverId: 'server', generation: 1 };
  let revision = 0, count = 0;
  const emit = message => { for (const listener of eventListeners) listener({ v: 1, serverId: 'server', userId: 'a', ...message }); };
  const port = { eventState: () => state, subscribeConnection: fn => { connectionListeners.add(fn); return () => connectionListeners.delete(fn); }, subscribeEvents: fn => { eventListeners.add(fn); return () => eventListeners.delete(fn); }, sendEvent: (message, observation) => {
    overrides.observeSend?.(message, observation);
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

test('two actual peer discovery flows keep their media parents and report only observed final control bytes', async () => {
  const sent = [];
  const h = setup({ observeSend(message, observation) {
    const context = observation?.operation && monitor.traceContext(observation.operation); if (message.type === 'peer.find') sent.push(context?.traceId);
    observation?.onBytes?.(new TextEncoder().encode(JSON.stringify(message)).byteLength);
  } });
  monitor.setDetailed(true); await settle();
  const scope = mediaRuntime.mediaScope(), a = mediaRuntime.startMedia(scope, png.length, new AbortController().signal), b = mediaRuntime.startMedia(scope, png.length, new AbortController().signal);
  const expected = [monitor.traceContext(a.operation).traceId, monitor.traceContext(b.operation).traceId];
  await Promise.all([h.peer.tryDownload(media(), new AbortController().signal, a), h.peer.tryDownload(media(), new AbortController().signal, b)]);
  assert.deepEqual(sent.sort(), expected.sort()); assert.notEqual(sent[0], sent[1]);
  const discovery = monitor.snapshot().events.filter(event => event.event === 'peer.discovery' && event.status === 'completed'); assert.equal(discovery.length, 2); assert.ok(discovery.every(event => event.controlBytes > 0 && event.parentOperationId));
  mediaRuntime.finishMedia(a, 'failed', 'missing_peer'); mediaRuntime.finishMedia(b, 'failed', 'missing_peer');
  assert.equal(monitor.snapshot().metrics.activeOperations, 0); assert.doesNotMatch(JSON.stringify(monitor.exportSnapshot(monitor.exportLease())), /browser|[a-f0-9]{64}/);
});

test('repeated peer observer faults preserve discovery results and release every allocated operation', async () => {
  const h = setup(); monitor.setDetailed(true); await settle();
  for (const method of ['update', 'record']) {
    const original = monitor[method]; monitor[method] = () => { throw new Error('observer only'); };
    try { for (let i = 0; i < 40; i++) { assert.equal(await h.peer.tryDownload(media(), new AbortController().signal), null); assert.equal(monitor.snapshot().metrics.activeOperations, 0); } }
    finally { monitor[method] = original; }
  }
});

test('active projection keeps real per-instance progress/rate/prefix/buffer while authorized device names remain UI-only', async context => {
  context.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] }); let clock = 100;
  context.mock.method(performance, 'now', () => clock);
  const h = setup(); await settle();
  h.peer.publish({ peers: [{ peerId: 'source-private', name: '授权设备名称' }] });
  const parent = mediaRuntime.startMedia(h.peer.observationScope, png.length, new AbortController().signal);
  const attempt = mediaRuntime.startAttempt(parent, 'p2p', 'p2p_unknown', h.peer.observationScope);
  const transfer = h.peer.createTransfer('private-transfer', media(), 'receive', 'source-private', 12000, new AbortController().signal, 0, attempt);
  transfer.channel = { bufferedAmount: 123, readyState: 'connecting', close() {} };
  const options = h.peer.options(transfer); options.position(10, 10); options.observe(10, 4, 0); options.position(20);
  clock = 300; h.peer.publish();
  let active = h.peer.snapshot().active[0]; assert.equal(active.durationMs, 200); assert.equal(active.payloadBytesPerSecond, 50); assert.equal(active.progressBytes, 20); assert.equal(active.resumeOffset, 10); assert.equal(active.payloadBytes, 10); assert.equal(active.bufferedAmount, 123);
  const labels = h.peer.deviceLabels(); assert.equal(labels[0].name, '授权设备名称'); assert.equal(labels[0].alias, active.deviceAlias); assert.deepEqual(labels[0].channels, [active.alias]);
  assert.doesNotMatch(JSON.stringify(h.peer.snapshot().active), /授权设备名称|source-private|private-transfer|[a-f0-9]{64}/);
  monitor.clearHistory(); monitor.setDetailed(false); clock = 500; h.peer.publish(); active = h.peer.snapshot().active[0]; assert.equal(active.progressBytes, 20); assert.equal(active.payloadBytesPerSecond, 25);
  options.position(png.length + 1); assert.equal(transfer.progressBytes, 20);
  h.peer.invalidate(); assert.equal(h.peer.transfers.size, 0); context.mock.timers.tick(100); await settle(); assert.deepEqual(h.peer.snapshot().active, []); options.position(30); assert.equal(transfer.progressBytes, 20);
  mediaRuntime.finishMedia(parent, 'cancelled');
});

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

for (const outcome of ['source-complete', 'source-early', 'cancel', 'deadline', 'deadline-closed', 'expired']) {
  test(`verified receiver waits for delayed source completion and handles ${outcome}`, async context => {
    context.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
    let clock = 0, channel, pc, verifiedResolve, releaseStats;
    context.mock.method(performance, 'now', () => clock);
    const verifiedSent = new Promise(resolve => { verifiedResolve = resolve; });
    const h = setup({ respond(message, emit) {
      if (message.type === 'peer.find') { emit({ type: 'peer.sources', requestId: message.requestId, assetId: message.assetId, peers: [{ peerId: 'source', name: 'source' }] }); return false; }
      if (message.type === 'peer.open') { emit({ type: 'peer.pending', requestId: message.requestId, transferId: 'delayed-transfer' }); setImmediate(() => emit({ type: 'peer.authorized', transferId: 'delayed-transfer', media: media(), requesterPeerId: 'peer', sourcePeerId: 'source', offererPeerId: 'peer', leaseMs: 10000 })); return false; }
      if (message.type === 'peer.signal') { emit({ type: 'peer.signalled', requestId: message.requestId, transferId: 'delayed-transfer' }); setImmediate(() => { channel.readyState = 'open'; channel.dispatchEvent(new Event('open')); }); return false; }
    } });
    monitor.setDetailed(true);
    class Channel extends EventTarget {
      readyState = 'connecting'; bufferedAmount = 0;
      send(raw) {
        const value = JSON.parse(raw);
        if (value.type === 'verified') { this.bufferedAmount = raw.length; verifiedResolve(); return; }
        if (value.type !== 'get') return;
        queueMicrotask(() => {
          const frame = new ArrayBuffer(png.length + 8); new DataView(frame).setUint32(4, png.length); new Uint8Array(frame, 8).set(png);
          this.dispatchEvent(new MessageEvent('message', { data: frame }));
          this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ v: 1, type: 'end', size: png.length }) }));
        });
      }
    }
    globalThis.RTCPeerConnection = class {
      connectionState = 'new'; sctp = { maxMessageSize: 16384 }; closed = false;
      constructor() { pc = this; }
      close() { this.closed = true; }
      createDataChannel() { channel = new Channel(); return channel; }
      async createOffer() { return { type: 'offer', sdp: 'synthetic offer' }; }
      async setLocalDescription() {}
      async getStats() { return outcome === 'source-early' ? new Promise(resolve => { releaseStats = resolve; }) : new Map(); }
    };
    await settle(); const controller = new AbortController(); let settled = false;
    const pending = h.peer.tryDownload(media(), controller.signal).then(result => { settled = true; return result; });
    await verifiedSent; await settle();
    assert.equal(settled, false); assert.equal(pc.closed, false);
    assert.equal(monitor.snapshot().events.some(event => event.event === 'media.read' && event.status === 'completed'), false);
    assert.equal(h.messages.some(message => message.type === 'peer.cancel' && message.reason === 'complete'), false);
    if (outcome === 'source-complete' || outcome === 'source-early') {
      channel.bufferedAmount = 0; await settle(); assert.equal(settled, false, 'Local drain alone is not remote consumption');
      pc.connectionState = 'disconnected'; pc.onconnectionstatechange();
      h.emit({ type: 'peer.cancelled', transferId: 'delayed-transfer', reason: 'complete' });
      if (outcome === 'source-early') releaseStats(new Map());
      assert.equal((await pending)?.transport, 'peer');
    } else {
      if (outcome === 'cancel') controller.abort();
      else if (outcome === 'expired') h.emit({ type: 'peer.cancelled', transferId: 'delayed-transfer', reason: 'expired' });
      else {
        if (outcome === 'deadline-closed') { channel.readyState = 'closed'; pc.connectionState = 'disconnected'; pc.onconnectionstatechange(); }
        clock = 12001; context.mock.timers.tick(12001);
      }
      assert.equal(await pending, null);
    }
    assert.equal(pc.closed, true); assert.equal(h.peer.snapshot().receiving, 0);
    assert.equal(h.peer.transfers.size, 0);
    const completed = monitor.snapshot().events.filter(event => event.event === 'media.read' && event.status === 'completed');
    assert.equal(completed.length, outcome === 'source-complete' || outcome === 'source-early' ? 1 : 0);
    if (completed.length) { assert.equal(completed[0].verified, true); assert.equal(completed[0].bytes, png.length); assert.equal(completed[0].verifiedBytes, png.length); }
    assert.equal(monitor.snapshot().metrics.activeOperations, 0);
  });
}

test('existing transfer ticks cannot overlap stats and an old PC cannot publish into the replacement peer scope', async context => {
  context.mock.timers.enable({ apis: ['setTimeout', 'setInterval'] }); let clock = 0, calls = 0, pc, releaseStats;
  context.mock.method(performance, 'now', () => clock);
  const h = setup({ respond(message, emit) {
    if (message.type === 'peer.find') { emit({ type: 'peer.sources', requestId: message.requestId, assetId: message.assetId, peers: [{ peerId: 'source', name: 'private source' }] }); return false; }
    if (message.type === 'peer.open') { emit({ type: 'peer.pending', requestId: message.requestId, transferId: 'slow-stats' }); setImmediate(() => emit({ type: 'peer.authorized', transferId: 'slow-stats', media: media(), requesterPeerId: 'peer', sourcePeerId: 'source', offererPeerId: 'peer', leaseMs: 10000 })); return false; }
    if (message.type === 'peer.signal') { emit({ type: 'peer.signalled', requestId: message.requestId, transferId: 'slow-stats' }); return false; }
  } });
  monitor.setDetailed(true);
  class Channel extends EventTarget { readyState = 'connecting'; id = 7; bufferedAmount = 0; send() {} }
  globalThis.RTCPeerConnection = class extends EventTarget {
    connectionState = 'connected'; iceConnectionState = 'connected'; sctp = { maxMessageSize: 16384 }; closed = false; stateListeners = 0;
    constructor() { super(); pc = this; }
    addEventListener(type, listener, options) { if (type === 'iceconnectionstatechange') this.stateListeners++; super.addEventListener(type, listener, options); }
    removeEventListener(type, listener, options) { if (type === 'iceconnectionstatechange') this.stateListeners--; super.removeEventListener(type, listener, options); }
    createDataChannel() { return new Channel(); }
    async createOffer() { return { type: 'offer', sdp: 'isolated offer' }; }
    async setLocalDescription() { this.onconnectionstatechange(); }
    getStats() { calls++; return new Promise(resolve => { releaseStats = resolve; }); }
    close() { this.closed = true; this.connectionState = this.iceConnectionState = 'closed'; }
  };
  await settle(); const pending = h.peer.tryDownload(media(), new AbortController().signal); await settle(); assert.equal(calls, 1);
  clock = 1000; context.mock.timers.tick(1000); await settle(); assert.equal(calls, 1);
  clock = 1100; context.mock.timers.tick(100); await settle(); assert.equal(calls, 2);
  const alias = h.peer.snapshot().active[0].alias; assert.equal(h.peer.snapshot().active[0].pcState, 'connected'); assert.equal(h.peer.snapshot().active[0].channelState, 'connecting');
  monitor.clearHistory(); monitor.setDetailed(false);
  releaseStats(new Map([
    ['transport', { type: 'transport', selectedCandidatePairId: 'pair', dtlsState: 'connected' }], ['pair', { type: 'candidate-pair', state: 'succeeded', localCandidateId: 'local', remoteCandidateId: 'remote', currentRoundTripTime: 0.005 }],
    ['local', { type: 'local-candidate', candidateType: 'host', address: '192.0.2.1' }], ['remote', { type: 'remote-candidate', candidateType: 'srflx' }],
    ['dc', { type: 'data-channel', dataChannelIdentifier: 7, bytesSent: 0, bytesReceived: 0 }],
  ])); await settle(); clock = 1200; context.mock.timers.tick(100); await settle();
  const active = h.peer.snapshot().active; assert.equal(active.length, 1); assert.equal(active[0].alias, alias); assert.equal(active[0].roundTripMs, 5); assert.equal(active[0].dtlsState, 'connected');
  assert.equal(active[0].channelBytesSent, 0); assert.equal(monitor.snapshot().events.length, 0); assert.equal(Object.isFrozen(active[0]), true);
  assert.doesNotMatch(JSON.stringify(active), /192\.0\.2|slow-stats|private source|[a-f0-9]{64}/);
  clock = 2200; context.mock.timers.tick(1000); await settle(); assert.equal(calls, 3);
  const late = releaseStats; h.changeConnection({ connected: false, generation: 2 }); assert.equal(await pending, null);
  assert.equal(pc.closed, true); assert.equal(pc.stateListeners, 0); assert.equal(h.peer.snapshot().active.length, 0);
  monitor.update(monitor.scope('peer'), 'peer', { route: 'p2p_direct', localType: 'host', remoteType: 'host' });
  late(new Map([
    ['transport', { type: 'transport', selectedCandidatePairId: 'pair' }], ['pair', { type: 'candidate-pair', state: 'succeeded', localCandidateId: 'local', remoteCandidateId: 'remote' }],
    ['local', { type: 'local-candidate', candidateType: 'relay', address: '192.0.2.1' }], ['remote', { type: 'remote-candidate', candidateType: 'relay' }],
  ])); await settle();
  assert.equal(monitor.snapshot().cards.peer.route, 'p2p_direct'); assert.equal(monitor.snapshot().cards.peer.localType, 'host'); assert.equal(monitor.snapshot().metrics.activeOperations, 0);
});

test('a verified resume across selected direct and relay paths reports mixed logical delivery without counting its prefix twice', async () => {
  let opened = 0; const channels = new Map(); const offsets = [];
  const h = setup({ respond(message, emit) {
    if (message.type === 'peer.find') { emit({ type: 'peer.sources', requestId: message.requestId, assetId: message.assetId, peers: [{ peerId: 'source', name: 'private source' }] }); return false; }
    if (message.type === 'peer.open') {
      const id = 'resume-' + ++opened; emit({ type: 'peer.pending', requestId: message.requestId, transferId: id });
      setImmediate(() => emit({ type: 'peer.authorized', transferId: id, media: media(), requesterPeerId: 'peer', sourcePeerId: 'source', offererPeerId: 'peer', leaseMs: 10000 })); return false;
    }
    if (message.type === 'peer.signal') { emit({ type: 'peer.signalled', requestId: message.requestId, transferId: message.transferId }); setImmediate(() => { const channel = channels.get(message.transferId); channel.readyState = 'open'; channel.dispatchEvent(new Event('open')); }); return false; }
  } });
  monitor.setDetailed(true);
  class Channel extends EventTarget {
    readyState = 'connecting'; bufferedAmount = 0; id = 7;
    constructor(label) { super(); this.label = label; this.transfer = label.slice('keepsite-'.length); channels.set(this.transfer, this); }
    close() { if (this.readyState !== 'closed') { this.readyState = 'closed'; this.dispatchEvent(new Event('close')); } }
    send(raw) {
      const message = JSON.parse(raw);
      if (message.type === 'verified') { queueMicrotask(() => h.emit({ type: 'peer.cancelled', transferId: this.transfer, reason: 'complete' })); return; }
      if (message.type !== 'get') return; offsets.push(message.offset);
      queueMicrotask(() => {
        const content = this.transfer === 'resume-1' ? png.subarray(0, 10) : png.subarray(message.offset);
        const frame = new ArrayBuffer(content.length + 8); new DataView(frame).setUint32(0, message.offset); new DataView(frame).setUint32(4, content.length); new Uint8Array(frame, 8).set(content);
        this.dispatchEvent(new MessageEvent('message', { data: frame }));
        if (this.transfer === 'resume-1') this.close();
        else this.dispatchEvent(new MessageEvent('message', { data: JSON.stringify({ v: 1, type: 'end', size: png.length }) }));
      });
    }
  }
  globalThis.RTCPeerConnection = class {
    connectionState = 'connected'; iceConnectionState = 'connected'; sctp = { maxMessageSize: 16384 };
    constructor() { this.which = opened; }
    createDataChannel(label) { this.channel = new Channel(label); return this.channel; }
    async createOffer() { return { type: 'offer', sdp: 'isolated offer' }; }
    async setLocalDescription() { this.onconnectionstatechange(); }
    async getStats() { return new Map([
      ['transport', { type: 'transport', selectedCandidatePairId: 'pair', dtlsState: 'connected' }], ['pair', { type: 'candidate-pair', state: 'succeeded', localCandidateId: 'local', remoteCandidateId: 'remote' }],
      ['local', { type: 'local-candidate', candidateType: 'host' }], ['remote', { type: 'remote-candidate', candidateType: this.which === 1 ? 'srflx' : 'relay' }],
    ]); }
    close() { this.connectionState = this.iceConnectionState = 'closed'; this.channel?.close(); }
  };
  await settle(); assert.equal((await h.peer.tryDownload(media(), new AbortController().signal))?.transport, 'peer');
  assert.deepEqual(offsets, [0, 10]); assert.equal(opened, 2);
  const parent = monitor.snapshot().events.find(event => event.event === 'media.read' && event.status === 'completed');
  assert.equal(parent.route, 'p2p_mixed'); assert.equal(parent.bytes, png.length); assert.equal(parent.verifiedBytes, png.length);
  const attempts = monitor.snapshot().events.filter(event => event.event === 'media.attempt' && event.status !== 'started');
  assert.deepEqual(attempts.map(event => [event.route, event.status, event.bytes]), [['p2p_direct', 'failed', 10], ['p2p_relay', 'completed', png.length - 10]]);
  assert.equal(monitor.snapshot().metrics.activeOperations, 0);
});

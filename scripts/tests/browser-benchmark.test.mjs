import test from 'node:test';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import { checkedNewOutput, installObserver, quantiles, sampleIndices, summarizeRun, validateManifest } from '../browser-benchmark-lib.mjs';
import { parseArguments } from '../benchmark-browser.mjs';

test('fixed workset spreads all 60 tasks across 300 unique records', () => {
  const indices = sampleIndices(300, 60);
  assert.equal(indices.length, 60); assert.equal(new Set(indices).size, 60);
  assert.equal(indices[0], 0); assert.equal(indices[59], 295);
  assert.deepEqual(sampleIndices(3, 3), [0, 1, 2]);
  for (const args of [[300, 301], [0, 1], [301, 1], [3, 1.5]]) assert.throws(() => sampleIndices(...args));
});

test('median and nearest rank P95 retain every sample beyond recent-50 diagnostics', () => {
  const values = Array.from({ length: 60 }, (_, index) => 60 - index);
  assert.deepEqual(quantiles(values), { count: 60, p50: 30.5, p95: 57 });
  assert.deepEqual(quantiles([]), { count: 0, p50: null, p95: null });
  assert.throws(() => quantiles([NaN])); assert.throws(() => quantiles([-1]));
});

test('dataset manifest independently proves unique content and complete byte budget', () => {
  const hash = 'a'.repeat(64), manifest = { userId: 'fixture', dataset: { count: 1, seed: 20261022, width: 256, height: 384, totalBytes: 19 }, items: [{ ticketId: 'ticket', media: { assetId: hash, sha256: hash, size: 19, width: 256, height: 384, mimeType: 'image/png' } }] };
  validateManifest(manifest, 1, 'fixture');
  manifest.dataset.totalBytes = 18; assert.throws(() => validateManifest(manifest, 1, 'fixture'), /budget/);
  manifest.dataset.totalBytes = 19; manifest.items[0].media.assetId = 'b'.repeat(64); assert.throws(() => validateManifest(manifest, 1, 'fixture'), /descriptor/);
});

test('CLI is plan-only by default and rejects invalid smoke workloads', async () => {
  assert.equal(parseArguments(['--output', 'unused']).execute, false);
  assert.equal(parseArguments(['--output', 'unused', '--count', '3', '--sample', '3', '--execute']).execute, true);
  assert.throws(() => parseArguments(['--output', 'unused', '--count', '3']));
  assert.throws(() => parseArguments(['--unknown', 'value']));
  await assert.rejects(checkedNewOutput('.cache/relative'), /absolute/);
  await assert.rejects(checkedNewOutput(fileURLToPath(new URL('../../', import.meta.url))), /already exists/);
});

function fixture() {
  const expected = [{ assetId: 'a', sha256: 'h', size: 19, mimeType: 'image/png' }];
  const run = { tasks: [{ assetId: 'a', source: 'cache', status: 'cached', bytes: 19, elapsedMs: 12 }], diagnostics: { records: [] } };
  const verification = { ok: true, items: [{ ...expected[0], ok: true }] };
  const observation = { overflow: false, lateEvents: 0, activePCs: 0, activeChannels: 0, receivedBinaryBytes: 0, receivedFilePayloadBytes: 0, receivedFrameHeaderBytes: 0, sentFilePayloadBytes: 0 };
  return { expected, run, verification, http: { overflow: false, incomplete: false, requests: [] }, observations: { source: { ...observation }, target: { ...observation } } };
}

test('warm success requires independent content verification and zero network media', () => {
  const value = fixture(), summarize = () => summarizeRun(value.run, value.expected, value.verification, value.http, value.observations, { warm: true });
  assert.equal(summarize().cacheCompletionShare, 1);
  value.verification.items[0].sha256 = 'wrong'; assert.match(summarize().validationErrors[0], /content mismatch/);
  assert.equal(summarize().cacheVerificationPassed, false);
  value.verification.items[0].sha256 = 'h'; value.observations.target.receivedBinaryBytes = 27; assert.match(summarize().validationErrors[0], /Warm cache/);
  value.observations.target.receivedBinaryBytes = 0;
  value.http.requests.push({ label: 'target', route: '/api/media/:assetId', bodyBytes: 19, status: 200, complete: true, durationMs: 1 });
  assert.match(summarize().validationErrors[0], /Warm cache/);
});

test('failed tasks retain failure rate and successful timing even when their cache entries are absent', () => {
  const value = fixture();
  value.expected.push({ assetId: 'b', sha256: 'other', size: 17, mimeType: 'image/png' });
  value.run.tasks.push({ assetId: 'b', source: null, status: 'timeout', bytes: 0, elapsedMs: null });
  value.verification.ok = false;
  value.verification.items.push({ assetId: 'b', ok: false, size: null, sha256: null, mimeType: null, errorCode: 'missing' });
  const summary = summarizeRun(value.run, value.expected, value.verification, value.http, value.observations);
  assert.equal(summary.failedTasks, 1); assert.equal(summary.taskFailureRate, .5);
  assert.equal(summary.successfulTasks, 1); assert.equal(summary.latencyMs.count, 1);
  assert.equal(summary.cacheVerificationPassed, false); assert.deepEqual(summary.validationErrors, []);
});

test('incomplete diagnostic coverage is unknown, never zero failed attempts or wasted bytes', () => {
  const value = fixture();
  Object.assign(value.run.diagnostics, { expectedPeerTransfers: 2, collectedPeerTransfers: 1, missingPeerTransfers: 1, overflow: false });
  const summary = summarizeRun(value.run, value.expected, value.verification, value.http, value.observations);
  assert.equal(summary.successfulTasks, 1); assert.equal(summary.peerDiagnosticsComplete, false);
  assert.equal(summary.peerAttemptFailures, null); assert.equal(summary.peerDiscardedBytes, null); assert.equal(summary.diagnosticStageLatencyMs, null);
});

test('incomplete ledgers, duplicate tasks and leaked connections cannot report success', () => {
  const value = fixture(), summarize = () => summarizeRun(value.run, value.expected, value.verification, value.http, value.observations);
  value.http.incomplete = true; assert.throws(summarize, /Incomplete HTTP/); value.http.incomplete = false;
  value.observations.source.activePCs = 1; assert.throws(summarize, /unclosed connection/); value.observations.source.activePCs = 0;
  value.run.tasks.push(value.run.tasks[0]); assert.throws(summarize, /duplicate task/);
});

test('native observer counts UTF-8, frame payload/header and closes, without retaining payloads', async () => {
  class Socket extends EventTarget { send() {} }
  class Channel extends EventTarget { send() {} close() { this.dispatchEvent(new Event('close')); } }
  class Connection extends EventTarget {
    createDataChannel() { this.channel = new Channel(); return this.channel; }
    async getStats() { return new Map([['t', { type: 'transport', selectedCandidatePairId: 'p' }], ['p', { localCandidateId: 'l', remoteCandidateId: 'r' }], ['l', { candidateType: this.missingType ? undefined : 'host', address: 'sensitive-local' }], ['r', { candidateType: this.invalidType ? 'unrecognized' : 'relay', address: 'sensitive-remote' }]]); }
    close() { this.channel?.close(); }
  }
  const prior = globalThis.window;
  globalThis.window = { WebSocket: Socket, RTCPeerConnection: Connection };
  try {
    installObserver(); const observer = window.__memoVerseObserver; observer.start('measured');
    const socket = new window.WebSocket(); socket.send('{"type":"peer.signal","payload":"中文秘密"}');
    const pc = new window.RTCPeerConnection(), channel = pc.createDataChannel();
    const binary = new ArrayBuffer(27); new DataView(binary).setUint32(4, 19); channel.send(binary);
    channel.dispatchEvent(new MessageEvent('message', { data: binary }));
    await pc.getStats(); pc.missingType = true; await pc.getStats(); pc.missingType = false; pc.invalidType = true; await pc.getStats();
    pc.close(); const result = observer.stop();
    assert.equal(result.sentBinaryBytes, 27); assert.equal(result.sentFilePayloadBytes, 19); assert.equal(result.receivedFrameHeaderBytes, 8);
    assert.equal(result.pcCreated, 1); assert.equal(result.pcClosed, 1); assert.equal(result.activeChannels, 0);
    assert.equal(result.ws[0].bytes, Buffer.byteLength('{"type":"peer.signal","payload":"中文秘密"}'));
    assert.equal(result.routes[0].route, 'relay');
    assert.equal(result.routes[1].localType, 'unknown'); assert.equal(result.routes[1].route, 'unknown');
    assert.equal(result.routes[2].remoteType, 'unknown'); assert.equal(result.routes[2].route, 'unknown');
    assert.doesNotMatch(JSON.stringify(result), /sensitive|秘密/);
  } finally { if (prior === undefined) delete globalThis.window; else globalThis.window = prior; }
});

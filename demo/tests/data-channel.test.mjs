import { test } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
const hook = registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL?.includes('/src/') && specifier.startsWith('.') && !specifier.endsWith('.ts')) return { url: new URL(specifier + '.ts', context.parentURL).href, shortCircuit: true };
  return nextResolve(specifier, context);
} });
const { frameSize, encodeChunk, decodeChunk, waitForBuffer, receiveFile, sendFile, selectedRoute, mergeRoute, FRAME_LIMIT, BUFFER_HIGH } = await import('../src/media/dataChannel.ts'); hook.deregister();
class Channel extends EventTarget {
  readyState = 'open'; bufferedAmount = 0; bufferedAmountLowThreshold = 0; binaryType = 'arraybuffer'; frames = []; other;
  send(data) { this.frames.push(data); queueMicrotask(() => this.other?.dispatchEvent(new MessageEvent('message', { data }))); }
}
function pair() { const a = new Channel(), b = new Channel(); a.other = b; b.other = a; return [a, b]; }
const options = (signal = new AbortController().signal) => ({ signal, guard() {}, progress() {}, observe() {} });
const asset = size => ({ assetId: 'a'.repeat(64), sha256: 'a'.repeat(64), size, mimeType: 'image/png' });

test('framing includes the header within negotiated limits and rejects gaps, repeats and overflow', () => {
  assert.equal(frameSize(Infinity), 16384); assert.equal(frameSize(1024), 1024);
  for (const invalid of [0, 8, NaN, undefined]) assert.throws(() => frameSize(invalid));
  const frame = encodeChunk(12, new Uint8Array([1, 2, 3]).buffer);
  assert.deepEqual(new Uint8Array(decodeChunk(frame, 12, 15, FRAME_LIMIT)), new Uint8Array([1, 2, 3]));
  assert.throws(() => decodeChunk(frame, 0, 15, FRAME_LIMIT)); assert.throws(() => decodeChunk(frame, 12, 14, FRAME_LIMIT));
  assert.throws(() => decodeChunk(frame, 12, 15, 10));
});

test('a real asynchronous channel pair transfers and acknowledges every byte before final verification', async () => {
  const [source, target] = pair(), bytes = new Uint8Array(70000).map((_, index) => index % 251), media = asset(bytes.length);
  let release; const verification = new Promise(resolve => { release = resolve; }); let received = false;
  const send = sendFile(source, media, new Blob([bytes], { type: media.mimeType }), () => FRAME_LIMIT, options());
  const receive = receiveFile(target, media, FRAME_LIMIT, { chunks: [], offset: 0 }, async blob => {
    assert.deepEqual(new Uint8Array(await blob.arrayBuffer()), bytes); received = true; await verification;
  }, options());
  while (!received) await new Promise(resolve => setImmediate(resolve));
  assert.equal(target.frames.some(frame => typeof frame === 'string' && JSON.parse(frame).type === 'verified'), false);
  release(); await Promise.all([send, receive]);
  assert.ok(source.frames.filter(frame => frame instanceof ArrayBuffer).every(frame => frame.byteLength <= FRAME_LIMIT));
});

test('a retained contiguous prefix resumes once without resending that prefix', async () => {
  const [source, target] = pair(), bytes = new Uint8Array(48000).fill(7), media = asset(bytes.length), prefixSize = 16376;
  const prefix = { chunks: [bytes.slice(0, prefixSize).buffer], offset: prefixSize };
  const send = sendFile(source, media, new Blob([bytes], { type: media.mimeType }), () => FRAME_LIMIT, options());
  const receive = receiveFile(target, media, FRAME_LIMIT, prefix, async blob => assert.equal(blob.size, bytes.length), options());
  await Promise.all([send, receive]);
  assert.equal(source.frames.filter(frame => frame instanceof ArrayBuffer).reduce((sum, frame) => sum + frame.byteLength - 8, 0), bytes.length - prefixSize);
});

test('backpressure subscribes before rechecking and cancellation releases a blocked sender', async () => {
  const channel = new Channel(); channel.bufferedAmount = BUFFER_HIGH + 1;
  const original = channel.addEventListener.bind(channel);
  channel.addEventListener = (name, listener, opts) => { original(name, listener, opts); if (name === 'bufferedamountlow') channel.bufferedAmount = 0; };
  await waitForBuffer(channel, FRAME_LIMIT, options());
  const stalled = new Channel(); stalled.bufferedAmount = BUFFER_HIGH + 1; const controller = new AbortController();
  const pending = waitForBuffer(stalled, FRAME_LIMIT, options(controller.signal)); controller.abort(); await assert.rejects(pending, error => error.name === 'AbortError');
});

test('the next complete frame must fit below the high watermark before sending', async () => {
  const channel = new Channel(); channel.bufferedAmount = BUFFER_HIGH - FRAME_LIMIT + 1;
  let ready = false; const waiting = waitForBuffer(channel, FRAME_LIMIT, options()).then(() => { ready = true; });
  await Promise.resolve(); assert.equal(ready, false);
  channel.bufferedAmount--; channel.dispatchEvent(new Event('bufferedamountlow')); await waiting; assert.equal(ready, true);
});

test('out-of-order bytes are counted as discarded and cannot finish a transfer', async () => {
  const target = new Channel(); let discarded = 0;
  const receive = receiveFile(target, asset(5), FRAME_LIMIT, { chunks: [], offset: 0 }, async () => assert.fail('must not verify'), { ...options(), observe(_payload, _control, count) { discarded += count; } });
  target.dispatchEvent(new MessageEvent('message', { data: encodeChunk(2, new Uint8Array([1, 2, 3]).buffer) }));
  await assert.rejects(receive, /位置或长度/); assert.equal(discarded, 3);
});

test('route classification uses the selected pair and never infers a route from configuration', async () => {
  const stats = new Map([['transport', { type: 'transport', selectedCandidatePairId: 'pair' }], ['pair', { localCandidateId: 'local', remoteCandidateId: 'remote' }], ['local', { candidateType: 'host' }], ['remote', { candidateType: 'srflx' }]]);
  assert.equal(await selectedRoute({ getStats: async () => stats }), 'direct');
  stats.set('remote', { candidateType: 'relay' }); assert.equal(await selectedRoute({ getStats: async () => stats }), 'relay');
  stats.delete('remote'); assert.equal(await selectedRoute({ getStats: async () => stats }), 'unknown');
});

test('missing route statistics do not become evidence of a path change and a cancelled stats call is bounded', async () => {
  assert.equal(mergeRoute('unknown', 'direct'), 'direct'); assert.equal(mergeRoute('direct', 'unknown'), 'direct');
  assert.equal(mergeRoute('direct', 'relay'), 'mixed'); assert.equal(mergeRoute('mixed', 'direct'), 'mixed');
  const controller = new AbortController(); const pending = selectedRoute({ getStats: () => new Promise(() => {}) }, controller.signal);
  controller.abort(); assert.equal(await pending, 'unknown');
});

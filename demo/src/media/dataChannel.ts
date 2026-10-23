import type { PeerAsset, PeerRoute } from './peerProtocol';
import { bytesOf } from './peerProtocol';

export const FRAME_LIMIT = 16 * 1024;
export const FRAME_HEADER = 8;
export const BUFFER_HIGH = 256 * 1024;
export const BUFFER_LOW = 64 * 1024;
export interface ReceivePrefix { chunks: ArrayBuffer[]; offset: number }
export interface ChannelOptions {
  signal: AbortSignal;
  guard(): void;
  progress(): void;
  observe(payload: number, control: number, discarded: number): void;
}
const cancelled = () => new DOMException('设备传输已取消。', 'AbortError');
function check(options: ChannelOptions) { if (options.signal.aborted) throw cancelled(); options.guard(); }
export function frameSize(maxMessageSize: number | undefined) {
  if (maxMessageSize === undefined || Number.isNaN(maxMessageSize) || maxMessageSize <= FRAME_HEADER) throw new Error('数据通道消息容量不可用。');
  return Math.floor(Math.min(FRAME_LIMIT, maxMessageSize));
}
export function encodeChunk(offset: number, bytes: ArrayBuffer): ArrayBuffer {
  const frame = new ArrayBuffer(FRAME_HEADER + bytes.byteLength), header = new DataView(frame);
  header.setUint32(0, offset); header.setUint32(4, bytes.byteLength); new Uint8Array(frame, FRAME_HEADER).set(new Uint8Array(bytes));
  return frame;
}
export function decodeChunk(frame: ArrayBuffer, offset: number, size: number, limit: number) {
  if (frame.byteLength < FRAME_HEADER || frame.byteLength > limit) throw new Error('设备分块大小无效。');
  const header = new DataView(frame), position = header.getUint32(0), length = header.getUint32(4);
  if (!length || position !== offset || length !== frame.byteLength - FRAME_HEADER || position + length > size) throw new Error('设备分块位置或长度不一致。');
  return frame.slice(FRAME_HEADER);
}
export function waitForBuffer(channel: RTCDataChannel, nextFrameBytes: number, options: ChannelOptions): Promise<void> {
  check(options);
  if (!Number.isSafeInteger(nextFrameBytes) || nextFrameBytes <= 0 || nextFrameBytes > FRAME_LIMIT) return Promise.reject(new Error('待发送分块大小无效。'));
  if (channel.readyState !== 'open') return Promise.reject(new Error('设备数据通道未连接。'));
  if (channel.bufferedAmount + nextFrameBytes <= BUFFER_HIGH) return Promise.resolve();
  return new Promise((resolve, reject) => {
    let settled = false;
    const cleanup = () => { channel.removeEventListener('bufferedamountlow', low); channel.removeEventListener('close', close); channel.removeEventListener('error', close); options.signal.removeEventListener('abort', abort); };
    const done = (error?: unknown) => { if (settled) return; settled = true; cleanup(); error ? reject(error) : resolve(); };
    const low = () => { try { check(options); if (channel.bufferedAmount + nextFrameBytes <= BUFFER_HIGH) done(); } catch (error) { done(error); } };
    const close = () => done(new Error('设备数据通道已关闭。'));
    const abort = () => done(cancelled());
    channel.addEventListener('bufferedamountlow', low); channel.addEventListener('close', close); channel.addEventListener('error', close);
    options.signal.addEventListener('abort', abort, { once: true });
    // Subscribe before checking again: draining between the two must not lose a wakeup.
    low(); if (options.signal.aborted) abort(); else if (channel.readyState !== 'open') close();
  });
}
function control(channel: RTCDataChannel, value: unknown, options: ChannelOptions) {
  check(options); const text = JSON.stringify(value);
  if (bytesOf(text) > 1024 || channel.readyState !== 'open') throw new Error('设备控制消息不可发送。');
  channel.send(text); options.observe(0, bytesOf(text), 0);
}
function parseControl(value: unknown, options: ChannelOptions): Record<string, unknown> {
  if (typeof value !== 'string' || bytesOf(value) > 1024) throw new Error('设备控制消息无效。');
  options.observe(0, bytesOf(value), 0);
  const parsed = JSON.parse(value);
  if (!parsed || parsed.v !== 1 || typeof parsed.type !== 'string') throw new Error('设备控制消息无效。');
  return parsed;
}

export function receiveFile(channel: RTCDataChannel, media: PeerAsset, maxSize: number, prefix: ReceivePrefix,
  verify: (blob: Blob) => Promise<void>, options: ChannelOptions): Promise<Blob> {
  const limit = frameSize(maxSize);
  return new Promise((resolve, reject) => {
    let settled = false, receiving = true, opened = false;
    const cleanup = () => { channel.removeEventListener('open', open); channel.removeEventListener('message', message); channel.removeEventListener('close', close); channel.removeEventListener('error', close); options.signal.removeEventListener('abort', abort); };
    const fail = (error: unknown) => { if (settled) return; settled = true; cleanup(); reject(error); };
    const close = () => fail(new Error('设备传输中断。'));
    const abort = () => fail(cancelled());
    const open = () => {
      if (opened) return; opened = true;
      try { check(options); control(channel, { v: 1, type: 'get', assetId: media.assetId, offset: prefix.offset }, options); } catch (error) { fail(error); }
    };
    const message = (event: MessageEvent) => {
      try {
        check(options);
        if (event.data instanceof ArrayBuffer) {
          options.observe(Math.max(0, event.data.byteLength - FRAME_HEADER), Math.min(FRAME_HEADER, event.data.byteLength), 0);
          if (!receiving) { options.observe(0, 0, Math.max(0, event.data.byteLength - FRAME_HEADER)); throw new Error('完整图片之后收到多余分块。'); }
          let chunk: ArrayBuffer;
          try { chunk = decodeChunk(event.data, prefix.offset, media.size, limit); }
          catch (error) { options.observe(0, 0, Math.max(0, event.data.byteLength - FRAME_HEADER)); throw error; }
          prefix.chunks.push(chunk); prefix.offset += chunk.byteLength; options.progress();
          control(channel, { v: 1, type: 'ack', offset: prefix.offset }, options);
          return;
        }
        const value = parseControl(event.data, options);
        if (value.type !== 'end' || value.size !== media.size || prefix.offset !== media.size || !receiving) throw new Error('设备图片传输未完整结束。');
        receiving = false;
        const blob = new Blob(prefix.chunks, { type: media.mimeType });
        void verify(blob).then(() => {
          if (settled) return;
          check(options); control(channel, { v: 1, type: 'verified', size: media.size }, options);
          settled = true; cleanup(); resolve(blob);
        }).catch(fail);
      } catch (error) { fail(error); }
    };
    channel.binaryType = 'arraybuffer';
    channel.addEventListener('open', open); channel.addEventListener('message', message); channel.addEventListener('close', close); channel.addEventListener('error', close);
    options.signal.addEventListener('abort', abort, { once: true });
    if (options.signal.aborted) abort(); else if (channel.readyState === 'open') open();
  });
}

export function sendFile(channel: RTCDataChannel, media: PeerAsset, blob: Blob, maxSize: () => number | undefined, options: ChannelOptions): Promise<void> {
  return new Promise((resolve, reject) => {
    let settled = false, sending = false, sent = 0, acknowledged = 0, ended = false;
    const cleanup = () => { channel.removeEventListener('message', message); channel.removeEventListener('close', close); channel.removeEventListener('error', close); options.signal.removeEventListener('abort', abort); };
    const fail = (error: unknown) => { if (settled) return; settled = true; cleanup(); reject(error); };
    const close = () => fail(new Error('设备传输中断。'));
    const abort = () => fail(cancelled());
    const send = async (offset: number) => {
      const limit = frameSize(maxSize()); sent = acknowledged = offset;
      while (sent < media.size) {
        check(options); if (settled) return;
        const nextFrameBytes = Math.min(media.size - sent, limit - FRAME_HEADER) + FRAME_HEADER;
        await waitForBuffer(channel, nextFrameBytes, options);
        const chunk = await blob.slice(sent, Math.min(media.size, sent + limit - FRAME_HEADER)).arrayBuffer();
        check(options); if (settled) return;
        await waitForBuffer(channel, nextFrameBytes, options); check(options); if (settled) return;
        channel.send(encodeChunk(sent, chunk)); sent += chunk.byteLength; options.observe(chunk.byteLength, FRAME_HEADER, 0);
      }
      ended = true; control(channel, { v: 1, type: 'end', size: media.size }, options);
    };
    const message = (event: MessageEvent) => {
      try {
        check(options); const value = parseControl(event.data, options);
        if (value.type === 'get' && !sending && value.assetId === media.assetId && Number.isSafeInteger(value.offset) && Number(value.offset) >= 0 && Number(value.offset) <= media.size) {
          sending = true; void send(Number(value.offset)).catch(fail); return;
        }
        if (value.type === 'ack' && sending && Number.isSafeInteger(value.offset) && Number(value.offset) > acknowledged && Number(value.offset) <= sent) {
          acknowledged = Number(value.offset); options.progress(); return;
        }
        if (value.type === 'verified' && ended && acknowledged === media.size && value.size === media.size) {
          options.progress(); settled = true; cleanup(); resolve(); return;
        }
        throw new Error('设备确认消息无效。');
      } catch (error) { fail(error); }
    };
    channel.binaryType = 'arraybuffer'; channel.bufferedAmountLowThreshold = BUFFER_LOW;
    channel.addEventListener('message', message); channel.addEventListener('close', close); channel.addEventListener('error', close);
    options.signal.addEventListener('abort', abort, { once: true });
    if (options.signal.aborted) abort();
  });
}

export function mergeRoute(previous: PeerRoute | undefined, next: Exclude<PeerRoute, 'mixed'>): PeerRoute {
  // Missing statistics are not evidence that the actual route changed.
  if (next === 'unknown') return previous ?? 'unknown';
  if (!previous || previous === 'unknown') return next;
  return previous === next ? previous : 'mixed';
}
export async function selectedRoute(connection: RTCPeerConnection, signal?: AbortSignal): Promise<Exclude<PeerRoute, 'mixed'>> {
  let timer: ReturnType<typeof setTimeout> | undefined, abort = () => {};
  try {
    if (signal?.aborted) return 'unknown';
    const deadline = new Promise<null>(resolve => { abort = () => resolve(null); timer = setTimeout(abort, 1000); signal?.addEventListener('abort', abort, { once: true }); });
    const stats = await Promise.race([connection.getStats(), deadline]); if (!stats) return 'unknown';
    let pair: RTCStats | undefined;
    stats.forEach(report => { if (report.type === 'transport' && report.selectedCandidatePairId) pair = stats.get(report.selectedCandidatePairId); });
    const candidatePair = pair as (RTCStats & { localCandidateId?: string; remoteCandidateId?: string }) | undefined;
    const local = candidatePair?.localCandidateId && stats.get(candidatePair.localCandidateId);
    const remote = candidatePair?.remoteCandidateId && stats.get(candidatePair.remoteCandidateId);
    if (local?.candidateType === 'relay' || remote?.candidateType === 'relay') return 'relay';
    if (local?.candidateType && remote?.candidateType) return 'direct';
  } catch { /* A closed connection may no longer expose statistics. */ }
  finally { clearTimeout(timer); signal?.removeEventListener('abort', abort); }
  return 'unknown';
}

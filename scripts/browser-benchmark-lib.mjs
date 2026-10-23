import { lstat } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import path from 'node:path';

export function sampleIndices(count, sample) {
  if (!Number.isInteger(count) || count < 1 || count > 300 || !Number.isInteger(sample) || sample < 1 || sample > count) throw new Error('Expected 1 <= sample <= count <= 300');
  return Array.from({ length: sample }, (_, index) => Math.floor(index * count / sample));
}

export function quantiles(values) {
  if (!values.length) return { count: 0, p50: null, p95: null };
  if (values.some(value => !Number.isFinite(value) || value < 0)) throw new Error('Invalid latency sample');
  const sorted = [...values].sort((a, b) => a - b), middle = Math.floor(sorted.length / 2);
  return { count: sorted.length, p50: sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2, p95: sorted[Math.ceil(sorted.length * .95) - 1] };
}

export function validateManifest(manifest, count, userId) {
  const { dataset, items } = manifest;
  if (manifest.userId !== userId || !Array.isArray(items) || items.length !== count || dataset?.count !== count || dataset.seed !== 20261022 || dataset.width !== 256 || dataset.height !== 384) throw new Error('Invalid deterministic dataset');
  if (new Set(items.map(item => item.ticketId)).size !== count || new Set(items.map(item => item.media?.sha256)).size !== count) throw new Error('Duplicate dataset ticket or content');
  for (const { ticketId, media } of items) {
    if (typeof ticketId !== 'string' || !media || !/^[a-f0-9]{64}$/.test(media.sha256) || media.assetId !== media.sha256 || !Number.isSafeInteger(media.size) || media.size < 1 || media.mimeType !== 'image/png' || media.width !== 256 || media.height !== 384) throw new Error('Invalid synthetic media descriptor');
  }
  if (dataset.totalBytes !== items.reduce((sum, item) => sum + item.media.size, 0) || dataset.totalBytes > 96 * 1024 * 1024) throw new Error('Dataset byte budget mismatch');
}

export async function checkedNewOutput(value) {
  if (!path.isAbsolute(value) || (process.platform === 'win32' && path.parse(path.resolve(value)).root.toUpperCase() !== 'D:\\')) throw new Error('Output must be a new absolute directory on D:');
  const target = path.resolve(value);
  for (let cursor = target; ; cursor = path.dirname(cursor)) {
    try {
      const info = await lstat(cursor);
      if (cursor === target) throw new Error('Output already exists');
      if (!info.isDirectory() || info.isSymbolicLink() || path.resolve(realpathSync(cursor)).toLowerCase() !== cursor.toLowerCase()) throw new Error('Output must not traverse links');
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (cursor === path.dirname(cursor)) break;
  }
  return target;
}

export function summarizeRun(run, expected, verification, http, observations, { warm = false, httpOnly = false } = {}) {
  if (http.overflow || http.incomplete || http.requests.some(row => !row.complete)) throw new Error('Incomplete HTTP phase ledger');
  const tasks = run.tasks, ids = expected.map(item => item.assetId);
  if (tasks.length !== ids.length || new Set(tasks.map(task => task.assetId)).size !== ids.length || tasks.some(task => !ids.includes(task.assetId))) throw new Error('Missing or duplicate task samples');
  const successes = tasks.filter(task => task.status === 'cached'), validationErrors = [];
  const verificationComplete = verification.items.length === ids.length && new Set(verification.items.map(item => item.assetId)).size === ids.length && verification.items.every(item => ids.includes(item.assetId));
  if (!verificationComplete) validationErrors.push('Independent cache verification ledger incomplete');
  for (const task of successes) {
    const item = expected.find(item => item.assetId === task.assetId);
    const checked = verification.items.find(row => row.assetId === item.assetId);
    if (!checked?.ok || checked.sha256 !== item.sha256 || checked.size !== item.size || checked.mimeType !== item.mimeType) validationErrors.push(`Independent cache content mismatch: ${task.assetId}`);
  }
  for (const observer of Object.values(observations)) {
    if (observer.overflow || observer.lateEvents || observer.activePCs || observer.activeChannels) throw new Error('Incomplete native transport ledger or unclosed connection');
  }
  if (successes.some(task => task.bytes !== expected.find(item => item.assetId === task.assetId)?.size || !Number.isFinite(task.elapsedMs))) throw new Error('Invalid successful task sample');
  const reads = http.requests.filter(row => row.label === 'target' && row.route === '/api/media/:assetId');
  const targetBody = reads.reduce((sum, row) => sum + row.bodyBytes, 0);
  if (warm && (targetBody !== 0 || observations.target.receivedBinaryBytes !== 0 || successes.some(task => task.source !== 'cache'))) validationErrors.push('Warm cache performed a media transfer');
  if (httpOnly && (observations.target.receivedBinaryBytes !== 0 || tasks.some(task => task.source === 'peer'))) validationErrors.push('HTTP-only phase used peer media');
  const records = run.diagnostics.records;
  const peerDiagnosticsComplete = run.diagnostics.overflow === false && run.diagnostics.missingPeerTransfers === 0
    && Number.isSafeInteger(run.diagnostics.expectedPeerTransfers) && run.diagnostics.expectedPeerTransfers === run.diagnostics.collectedPeerTransfers;
  const receiveRecords = records.filter(record => record.direction === 'receive' && record.path === 'peer');
  const stages = {};
  for (const stage of ['discoveryMs', 'authorizationMs', 'connectionMs', 'firstByteMs', 'verificationMs', 'sourceReadMs']) stages[stage] = quantiles(receiveRecords.filter(record => record.status === 'complete' && record[stage] !== undefined).map(record => record[stage]));
  const ws = {};
  for (const [endpoint, observer] of Object.entries(observations)) for (const frame of observer.ws ?? []) {
    const key = `${endpoint}:${frame.direction}:${frame.type}`;
    ws[key] ??= { frames: 0, applicationPayloadBytes: 0 };
    ws[key].frames++; ws[key].applicationPayloadBytes += frame.bytes;
  }
  return {
    tasks: tasks.length, successfulTasks: successes.length, failedTasks: tasks.length - successes.length,
    taskFailureRate: (tasks.length - successes.length) / tasks.length,
    validationErrors, cacheVerificationPassed: verification.ok && verificationComplete && validationErrors.length === 0,
    elapsedMs: run.elapsedMs, peakTaskConcurrency: run.peakActive,
    latencyMs: quantiles(successes.map(task => task.elapsedMs)),
    peerDiagnosticsComplete, diagnosticStageLatencyMs: peerDiagnosticsComplete ? stages : null,
    diagnosticCoverage: Object.fromEntries(['recordCount', 'expectedPeerTransfers', 'collectedPeerTransfers', 'missingPeerTransfers', 'snapshotWindow', 'overflow'].map(key => [key, run.diagnostics[key] ?? null])),
    p2pCompletionShare: successes.filter(task => task.source === 'peer').length / tasks.length,
    serverCompletionShare: successes.filter(task => ['server', 's3'].includes(task.source)).length / tasks.length,
    cacheCompletionShare: successes.filter(task => task.source === 'cache').length / tasks.length,
    peerAttemptFailures: peerDiagnosticsComplete ? receiveRecords.filter(record => record.status === 'failed').length : null,
    peerDiscardedBytes: peerDiagnosticsComplete ? receiveRecords.reduce((sum, record) => sum + record.discardedBytes + (record.discardedPrefixBytes ?? 0), 0) : null,
    targetMediaHttpBodyBytes: targetBody,
    targetMediaPayloadBytes: observations.target.receivedFilePayloadBytes,
    targetMediaFrameHeaderBytes: observations.target.receivedFrameHeaderBytes,
    sourceMediaPayloadBytes: observations.source.sentFilePayloadBytes,
    targetHttpRequests: http.requests.filter(row => row.label === 'target').length,
    targetHttpBodyBytes: http.requests.filter(row => row.label === 'target').reduce((sum, row) => sum + row.bodyBytes, 0),
    targetServerResponseDurationMs: quantiles(http.requests.filter(row => row.label === 'target').map(row => row.durationMs)),
    webSocketByEndpointDirectionType: ws,
    httpFailures: http.requests.filter(row => row.status < 200 || row.status >= 400).length,
  };
}

// Runs inside each isolated browser. Only native event sizes and safe protocol
// metadata are retained; no SDP, candidate address, cookie or request payload.
export function installObserver() {
  const encoder = new TextEncoder(), limit = 20000;
  const connections = new Set(), channels = new Set();
  let phase = null, lateEvents = 0;
  const fresh = id => ({ id, overflow: false, startedAt: Date.now(), stoppedAt: null, ws: [], rtc: [], routes: [], pcCreated: 0, pcClosed: 0, channelsCreated: 0, channelsClosed: 0, sentBinaryBytes: 0, receivedBinaryBytes: 0, sentFilePayloadBytes: 0, receivedFilePayloadBytes: 0, sentFrameHeaderBytes: 0, receivedFrameHeaderBytes: 0, sentControlBytes: 0, receivedControlBytes: 0 });
  const push = (state, key, value) => { if (!state) return; if (state.stoppedAt) { lateEvents++; return; } if (state[key].length >= limit) state.overflow = true; else state[key].push(value); };
  const safeType = data => { try { const type = JSON.parse(data).type; return typeof type === 'string' && /^[a-z][a-z0-9_.-]{0,60}$/.test(type) ? type : 'unknown'; } catch { return 'unknown'; } };
  const size = data => typeof data === 'string' ? encoder.encode(data).byteLength : data instanceof Blob ? data.size : data.byteLength;
  const wsFrame = (direction, data) => { const state = phase; push(state, 'ws', { direction, bytes: size(data), type: typeof data === 'string' ? safeType(data) : 'binary', at: performance.now() }); };
  const NativeSocket = window.WebSocket;
  window.WebSocket = class extends NativeSocket {
    constructor(url, protocols) { super(url, protocols); this.addEventListener('message', event => wsFrame('received', event.data)); }
    send(data) { super.send(data); wsFrame('sent', data); }
  };
  const observed = new WeakSet();
  let channelSequence = 0, connectionSequence = 0;
  const observeChannel = channel => {
    if (observed.has(channel)) return;
    observed.add(channel); channels.add(channel); const state = phase, id = ++channelSequence;
    if (state) state.channelsCreated++;
    const frame = (direction, data) => {
      if (!state) return;
      if (state.stoppedAt) { lateEvents++; return; }
      const bytes = size(data), row = { channel: id, direction, bytes, at: performance.now() };
      if (typeof data === 'string') {
        row.type = safeType(data); state[direction + 'ControlBytes'] += bytes;
        try { const parsed = JSON.parse(data); if (row.type === 'get' && Number.isSafeInteger(parsed.offset)) { row.offset = parsed.offset; if (/^[a-f0-9]{64}$/.test(parsed.assetId)) row.assetId = parsed.assetId; } } catch { /* Sizes remain measured. */ }
      } else if (data instanceof ArrayBuffer || ArrayBuffer.isView(data)) {
        const buffer = data instanceof ArrayBuffer ? data : data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength);
        row.type = 'binary'; state[direction + 'BinaryBytes'] += bytes;
        if (bytes >= 8) {
          const header = new DataView(buffer); row.offset = header.getUint32(0); row.payloadBytes = header.getUint32(4);
          if (row.payloadBytes !== bytes - 8) state.overflow = true;
          state[direction + 'FilePayloadBytes'] += bytes - 8; state[direction + 'FrameHeaderBytes'] += 8;
        } else state.overflow = true;
      } else { row.type = 'unmeasured-blob'; state.overflow = true; }
      push(state, 'rtc', row);
    };
    const nativeSend = channel.send.bind(channel);
    channel.send = data => { nativeSend(data); frame('sent', data); };
    channel.addEventListener('message', event => frame('received', event.data));
    channel.addEventListener('close', () => { channels.delete(channel); if (state) state.channelsClosed++; });
  };
  const NativeConnection = window.RTCPeerConnection;
  window.RTCPeerConnection = class extends NativeConnection {
    constructor(configuration) {
      super(configuration); connections.add(this); this.__benchState = phase; this.__benchId = ++connectionSequence;
      if (phase) phase.pcCreated++;
      this.addEventListener('datachannel', event => observeChannel(event.channel));
    }
    createDataChannel(...args) { const channel = super.createDataChannel(...args); observeChannel(channel); return channel; }
    async getStats(...args) {
      const stats = await super.getStats(...args); const state = this.__benchState;
      let selected;
      stats.forEach(row => { if (row.type === 'transport' && row.selectedCandidatePairId) selected = stats.get(row.selectedCandidatePairId); });
      const local = selected && stats.get(selected.localCandidateId), remote = selected && stats.get(selected.remoteCandidateId);
      const allowed = type => ['host', 'srflx', 'prflx', 'relay'].includes(type) ? type : 'unknown';
      const localType = allowed(local?.candidateType), remoteType = allowed(remote?.candidateType);
      const route = localType === 'unknown' || remoteType === 'unknown' ? 'unknown' : localType === 'relay' || remoteType === 'relay' ? 'relay' : 'direct';
      if (state && !state.stoppedAt) push(state, 'routes', { connection: this.__benchId, localType, remoteType, route });
      return stats;
    }
    close() { if (connections.delete(this) && this.__benchState) this.__benchState.pcClosed++; super.close(); }
  };
  window.__memoVerseObserver = {
    start(id) { if (phase && !phase.stoppedAt || connections.size || channels.size) throw new Error('Transport observation phase overlap'); phase = fresh(id); lateEvents = 0; },
    status() { return { activePCs: connections.size, activeChannels: channels.size, lateEvents }; },
    stop() { if (!phase || phase.stoppedAt) throw new Error('No active observation phase'); phase.stoppedAt = Date.now(); return { ...phase, activePCs: connections.size, activeChannels: channels.size, lateEvents }; },
  };
}

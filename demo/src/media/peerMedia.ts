import { assertSessionEpoch, sessionEpoch } from '../api';
import { verifyMediaBlob } from '../mediaCache';
import type { MediaRef } from '../data';
import { receiveFile, sendFile, selectedRoute, mergeRoute, type ChannelOptions, type ReceivePrefix } from './dataChannel';
import { bytesOf, safeId, sameAsset, validPeerAsset, type AccountEventPort, type ClientPeerMessage, type PeerAsset, type PeerCachePort, type PeerIdentity, type PeerMessage,
  type PeerView, type SignalKind, type SignalPayload, type TransferRecord, type PeerMediaPort } from './peerProtocol';

const abortError = () => new DOMException('设备传输已取消。', 'AbortError');
class PeerRPCError extends Error { code: string; constructor(code: string, message: string) { super(message); this.code = code; } }
const now = () => performance.now();
const transportFailure = (error: unknown) => error instanceof Error && /设备传输中断|设备数据通道已关闭|设备连接超时|设备连接中断|设备传输停滞/.test(error.message);
const validLease = (value: unknown, maximum: number) => Number.isSafeInteger(value) && Number(value) > 0 && Number(value) <= maximum;
const peerList = (value: unknown): PeerIdentity[] => {
  if (!Array.isArray(value) || value.length > 8 || value.some(peer => !peer || !safeId(peer.peerId) || typeof peer.name !== 'string' || peer.name.length > 32)) throw new Error('在线页面信息不完整。');
  return value;
};
interface PendingRPC { resolve(value: PeerMessage): void; reject(error: unknown): void; expected: string; cleanup(): void }
interface Transfer {
  id: string; media: PeerAsset; role: 'receive' | 'send'; peerId: string; controller: AbortController;
  pc?: RTCPeerConnection; channel?: RTCDataChannel; grant?: PeerMessage; leaseUntil: number; deadline: number; progressAt: number;
  grantResolve(value: PeerMessage): void; grantReject(error: unknown): void; authorized: Promise<PeerMessage>;
  candidates: (RTCIceCandidateInit | null)[]; localCandidates: (RTCIceCandidateInit | null)[]; localDescriptionQueued: boolean;
  iceCount: number; remoteIceCount: number; signalBytes: number; signals: Promise<void>; outgoing: Promise<void>;
  tick?: ReturnType<typeof setInterval>; renewAt: number; renewing: boolean; closed: boolean; noRetry: boolean; retryable: boolean; started: number; record: TransferRecord;
  routeAt: number; routeTask?: Promise<void>; verified: boolean; sourceCompleted?: boolean; sourceComplete?: () => void;
}
export class PeerMedia implements PeerMediaPort {
  private port: AccountEventPort;
  private cache: PeerCachePort;
  private scope = sessionEpoch();
  private generation = 0;
  private socketGeneration = -1;
  private serverId = '';
  private running = false;
  private supported = false;
  private foreground = true;
  private registering = false;
  private state: PeerView = { wanted: true, connected: false, enabled: false, peerId: null, reason: '正在连接设备服务。', peers: [], inventoryCount: 0, receiving: 0, sending: 0,
    recent: [], totals: { peer: 0, direct: 0, relay: 0, unknown: 0, mixed: 0, s3: 0, server: 0, cache: 0, discarded: 0, control: 0 } };
  private listeners = new Set<() => void>();
  private subscriptions: (() => void)[] = [];
  private pending = new Map<string, PendingRPC>();
  private transfers = new Map<string, Transfer>();
  private early = new Map<string, PeerMessage>();
  private openings = 0;
  private downloads = new Map<AbortController, string>();
  private inventoryRevision = 0;
  private replaceNeeded = true;
  private advertised = new Set<string>();
  private inventoryBusy = false;
  private inventoryDirty = false;
  private lifetime = new AbortController();
  private heartbeat?: ReturnType<typeof setInterval>;
  private inventoryTimer?: ReturnType<typeof setTimeout>;
  private paintTimer?: ReturnType<typeof setTimeout>;
  private records = new Map<string, { record: TransferRecord; started: number }>();
  constructor(port: AccountEventPort, cache: PeerCachePort) { this.port = port; this.cache = cache; }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  snapshot = () => this.state;
  private publish(update: Partial<PeerView> = {}) { this.state = { ...this.state, ...update, totals: { ...this.state.totals } }; for (const listener of this.listeners) listener(); }
  private paint() {
    if (this.paintTimer) return;
    this.paintTimer = setTimeout(() => { this.paintTimer = undefined; this.publish({ recent: [...this.records.values()].slice(-50).reverse().map(({ record, started }) => ({ ...record, milliseconds: record.status === 'active' ? now() - started : record.milliseconds })) }); }, 100);
  }
  private record(record: TransferRecord, started = now()) {
    this.records.set(record.id, { record, started });
    while (this.records.size > 50) { const oldest = [...this.records].find(([, value]) => value.record.status !== 'active'); if (!oldest) break; this.records.delete(oldest[0]); }
    this.paint();
  }
  recordCache(bytes: number) { this.state.totals.cache += bytes; this.paint(); }
  recordCloud(id: string, path: 's3' | 'server', event: 'start' | 'bytes' | 'complete' | 'failed', bytes = 0) {
    if (event === 'start') this.record({ id, path, direction: 'receive', status: 'active', bytes: 0, controlBytes: 0, discardedBytes: 0, milliseconds: 0, reason: '' });
    const item = this.records.get(id); if (!item) return;
    if (event === 'bytes') { item.record.bytes += bytes; this.state.totals[path] += bytes; }
    if (event === 'complete' || event === 'failed') { item.record.status = event; item.record.milliseconds = now() - item.started; if (event === 'failed') item.record.reason = '云端读取或完整性校验失败'; }
    this.paint();
  }
  setSupported(value: boolean) { this.supported = value; this.connectionChanged(); }
  setForeground(value: boolean) { if (this.foreground === value) return; this.foreground = value; this.connectionChanged(); }
  setWanted = (value: boolean) => { if (this.state.wanted === value) return; this.publish({ wanted: value }); this.connectionChanged(); };
  start() {
    if (this.running) return; this.running = true; this.scope = sessionEpoch();
    this.subscriptions = [this.port.subscribeConnection(() => this.connectionChanged()), this.port.subscribeEvents(message => this.message(message)), this.cache.subscribeInventory(() => this.queueInventory())];
    this.heartbeat = setInterval(() => { void this.maintain(); }, 15000); this.connectionChanged();
  }
  stop() {
    this.running = false; for (const stop of this.subscriptions.splice(0)) stop(); clearInterval(this.heartbeat); this.heartbeat = undefined;
    this.reset('设备传输已停止。', true); clearTimeout(this.paintTimer); this.paintTimer = undefined;
  }
  private allowed() { return this.running && this.supported && this.state.wanted && this.foreground && this.port.eventState().connected && typeof RTCPeerConnection !== 'undefined'; }
  private check(generation = this.generation) { assertSessionEpoch(this.scope); if (!this.running || generation !== this.generation || this.lifetime.signal.aborted) throw abortError(); }
  private sendPeer(message: ClientPeerMessage) {
    if (!this.port.sendEvent(message)) return false;
    const bytes = bytesOf(message); this.state.totals.control += bytes;
    const transfer = typeof message.transferId === 'string' && this.transfers.get(message.transferId); if (transfer) transfer.record.controlBytes += bytes;
    this.paint(); return true;
  }
  private reset(reason: string, unregister = false) {
    if (unregister && this.state.peerId) this.sendPeer({ v: 1, type: 'device.unregister', requestId: crypto.randomUUID() });
    this.generation++; this.lifetime.abort(); this.lifetime = new AbortController();
    for (const pending of this.pending.values()) { pending.cleanup(); pending.reject(abortError()); } this.pending.clear();
    this.invalidate(); this.early.clear(); this.advertised.clear(); this.inventoryRevision = 0; this.replaceNeeded = true;
    clearTimeout(this.inventoryTimer); this.inventoryTimer = undefined; this.inventoryDirty = false; this.inventoryBusy = false; this.registering = false;
    this.publish({ enabled: false, peerId: null, peers: [], inventoryCount: 0, receiving: 0, sending: 0, reason });
  }
  private connectionChanged() {
    if (!this.running) return;
    const connection = this.port.eventState();
    if (connection.generation !== this.socketGeneration || connection.serverId !== this.serverId || !connection.connected) {
      this.reset('状态连接恢复后会重新登记。'); this.socketGeneration = connection.generation; this.serverId = connection.serverId;
    }
    this.publish({ connected: connection.connected });
    if (!this.allowed()) {
      const reason = !this.state.wanted ? '本页已停用 P2P，图片仍可从云端获取。' : !this.foreground ? '页面不在前台，设备传输已暂停。' : !this.supported ? '当前服务尚未启用设备传输。' : '设备连接尚未就绪，使用云端读取。';
      if (this.state.peerId || this.registering) this.reset(reason, true); else this.publish({ reason });
      return;
    }
    if (!this.state.enabled && !this.registering) void this.register();
  }
  private rpc(type: string, fields: Record<string, unknown>, expected: string, signal = this.lifetime.signal, timeout = 1000): Promise<PeerMessage> {
    this.check(); if (signal.aborted) return Promise.reject(abortError());
    if (this.pending.size >= 64) return Promise.reject(new Error('设备信令繁忙。'));
    const requestId = crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const abort = () => finish(abortError());
      const timer = setTimeout(() => finish(new PeerRPCError('unavailable', '设备响应超时。')), Math.max(1, timeout));
      const cleanup = () => { clearTimeout(timer); signal.removeEventListener('abort', abort); };
      const finish = (error: unknown) => { if (!this.pending.delete(requestId)) return; cleanup(); reject(error); };
      this.pending.set(requestId, { resolve, reject, expected, cleanup }); signal.addEventListener('abort', abort, { once: true });
      if (!this.sendPeer({ v: 1, type, requestId, ...fields })) finish(new PeerRPCError('unavailable', '设备信令不可用。'));
    });
  }
  private async register() {
    this.registering = true; const generation = this.generation;
    try {
      const result = await this.rpc('device.register', { name: '浏览器页面' }, 'device.registered', this.lifetime.signal, 3000); this.check(generation);
      if (typeof result.enabled !== 'boolean' || result.heartbeatMs !== 15000 || result.leaseMs !== 45000 || !Number.isSafeInteger(result.inventoryRevision) || Number(result.inventoryRevision) < 0 || result.turnEnabled !== false || !Array.isArray(result.iceServers) || result.iceServers.length) throw new Error('设备服务配置不完整。');
      if (!result.enabled) { this.publish({ enabled: false, reason: result.reason || '设备加速暂不可用，使用云端读取。' }); return; }
      if (!safeId(result.peerId) || !result.limits || result.limits.maxInventory > 1024 || result.limits.maxBatchAssets > 32) throw new Error('设备登记结果不完整。');
      this.inventoryRevision = Number(result.inventoryRevision); this.advertised.clear(); this.replaceNeeded = true;
      this.publish({ enabled: true, peerId: result.peerId, reason: '仅尝试直连；TURN 未启用，失败时转云端。' });
      this.queueInventory(); await this.refreshPeers();
    } catch (error) { if (generation === this.generation) this.publish({ enabled: false, peerId: null, reason: error instanceof Error ? error.message : '设备登记失败，使用云端读取。' }); }
    finally { if (generation === this.generation) { this.registering = false; this.publish(); } }
  }
  private async maintain() {
    if (!this.allowed()) return;
    if (!this.state.enabled) { if (!this.registering) await this.register(); return; }
    const generation = this.generation;
    try {
      const alive = await this.rpc('device.heartbeat', {}, 'device.alive'); this.check(generation);
      if (alive.leaseMs !== 45000 || !Number.isSafeInteger(alive.inventoryRevision)) throw new Error('设备在线租期无效。');
      if (!this.inventoryBusy && alive.inventoryRevision !== this.inventoryRevision) throw new Error('设备清单已失效。');
      this.queueInventory(); await this.refreshPeers();
    } catch { if (generation === this.generation) this.reset('设备服务暂不可用，图片转云端；稍后重新登记。', true); }
  }
  refreshPeers = async () => {
    if (!this.state.enabled) return; const generation = this.generation;
    try { const result = await this.rpc('device.list', {}, 'device.peers'); this.check(generation); this.publish({ peers: peerList(result.peers) }); }
    catch { if (generation === this.generation) this.publish({ peers: [] }); }
  };
  private queueInventory() {
    this.inventoryDirty = true;
    if (!this.state.enabled || this.inventoryBusy || this.inventoryTimer) return;
    this.inventoryTimer = setTimeout(() => { this.inventoryTimer = undefined; void this.syncInventory(); }, 50);
  }
  private async syncInventory() {
    if (this.inventoryBusy || !this.state.enabled) return; this.inventoryBusy = true; const generation = this.generation;
    try {
      while (this.inventoryDirty) {
        this.inventoryDirty = false; const assets = await this.cache.listShareable(this.lifetime.signal); this.check(generation);
        const next = new Set(assets.slice(0, 1024).map(asset => asset.assetId));
        const add = [...next].filter(id => !this.advertised.has(id)), remove = [...this.advertised].filter(id => !next.has(id));
        if (!this.replaceNeeded && !add.length && !remove.length && this.inventoryRevision > 0) continue;
        for (const transfer of this.transfers.values()) if (transfer.role === 'send' && !next.has(transfer.media.assetId)) this.closeTransfer(transfer, '缓存不再可用');
        const revision = this.inventoryRevision + 1;
        if (!this.replaceNeeded && this.inventoryRevision > 0 && add.length + remove.length <= 32) {
          const ack = await this.rpc('cache.patch', { revision, add, remove }, 'cache.ack'); this.check(generation);
          if (ack.revision !== revision || ack.complete !== true || ack.nextBatchIndex !== null || ack.count !== next.size) throw new Error('设备缓存清单确认不一致。');
        } else {
          const ids = [...next], batchCount = Math.max(1, Math.ceil(ids.length / 32));
          for (let batchIndex = 0; batchIndex < batchCount; batchIndex++) {
            const ack = await this.rpc('cache.replace', { revision, batchIndex, batchCount, assetIds: ids.slice(batchIndex * 32, (batchIndex + 1) * 32) }, 'cache.ack'); this.check(generation);
            const complete = batchIndex === batchCount - 1;
            if (ack.revision !== revision || ack.complete !== complete || ack.nextBatchIndex !== (complete ? null : batchIndex + 1) || ack.count !== Math.min(ids.length, (batchIndex + 1) * 32)) throw new Error('设备缓存分页确认不一致。');
          }
        }
        this.inventoryRevision = revision; this.advertised = next; this.replaceNeeded = false; this.publish({ inventoryCount: next.size });
      }
    } catch { if (generation === this.generation) this.reset('设备缓存清单需要重新登记，当前使用云端读取。', true); }
    finally { if (generation === this.generation) this.inventoryBusy = false; }
  }
  private message(message: PeerMessage) {
    if (!this.running || message.serverId !== this.serverId) return;
    const wireBytes = message.wireBytes ?? bytesOf(message); this.state.totals.control += wireBytes;
    const observedTransfer = message.transferId && this.transfers.get(message.transferId); if (observedTransfer) observedTransfer.record.controlBytes += wireBytes;
    this.paint();
    const pending = message.requestId && this.pending.get(message.requestId);
    if (pending) {
      this.pending.delete(message.requestId!); pending.cleanup();
      if (message.type === 'peer.error') pending.reject(new PeerRPCError(message.code ?? 'invalid', message.message || '设备请求失败。'));
      else if (message.type !== pending.expected) pending.reject(new PeerRPCError('invalid', '设备响应类型不一致。'));
      else pending.resolve(message);
    }
    if (!pending && message.type === 'device.registered' && message.enabled === false) { this.reset(message.reason || '设备服务已停用，转为云端读取。'); return; }
    if (message.type === 'peer.request') { void this.serve(message); return; }
    const transfer = message.transferId && this.transfers.get(message.transferId);
    if (message.type === 'peer.authorized') {
      if (transfer) this.authorize(transfer, message);
      else if (this.openings && safeId(message.transferId) && this.early.size < 4) this.early.set(message.transferId, message);
      return;
    }
    if (message.type === 'peer.signal' && transfer) {
      transfer.signals = transfer.signals.then(() => this.signal(transfer, message)).catch(error => { transfer.noRetry = true; this.closeTransfer(transfer, error instanceof Error ? error.message : '信令失败'); });
    } else if (message.type === 'peer.cancelled' && transfer) {
      // Only the source can confirm it consumed our DataChannel verified message.
      if (message.reason === 'complete' && transfer.role === 'receive' && transfer.verified) {
        transfer.sourceCompleted = true; transfer.sourceComplete?.(); return;
      }
      transfer.noRetry = message.reason !== 'transport_error';
      this.closeTransfer(transfer, message.reason === 'transport_error' ? '设备连接中断' : message.reason || '对端取消传输', false, message.reason === 'transport_error');
    }
  }
  private createTransfer(id: string, media: PeerAsset, role: Transfer['role'], peerId: string, deadline: number, signal: AbortSignal, started = now()) {
    let grantResolve!: Transfer['grantResolve'], grantReject!: Transfer['grantReject'];
    const authorized = new Promise<PeerMessage>((resolve, reject) => { grantResolve = resolve; grantReject = reject; }); void authorized.catch(() => {});
    const record: TransferRecord = { id, direction: role, path: 'peer', status: 'active', bytes: 0, controlBytes: 0, discardedBytes: 0, milliseconds: 0, reason: '' };
    const transfer: Transfer = { id, media, role, peerId, controller: new AbortController(), leaseUntil: now() + 5000, deadline, progressAt: now(),
      authorized, grantResolve, grantReject, candidates: [], localCandidates: [], localDescriptionQueued: false, iceCount: 0, remoteIceCount: 0, signalBytes: 0, signals: Promise.resolve(), outgoing: Promise.resolve(), renewAt: Infinity, renewing: false, closed: false, noRetry: false, retryable: false, routeAt: 0, verified: false, started, record };
    const abort = () => this.closeTransfer(transfer, '传输已取消'); signal.addEventListener('abort', abort, { once: true });
    transfer.controller.signal.addEventListener('abort', () => signal.removeEventListener('abort', abort), { once: true });
    this.transfers.set(id, transfer); this.record(record, started);
    transfer.tick = setInterval(() => {
      if (now() >= transfer.deadline || now() >= transfer.leaseUntil) { transfer.noRetry = true; this.closeTransfer(transfer, '设备传输预算或授权已过期'); return; }
      if (transfer.channel?.readyState === 'open' && now() - transfer.progressAt > 2000) { this.closeTransfer(transfer, '设备传输停滞', true, true); return; }
      if (transfer.pc?.connectionState === 'connected' && now() >= transfer.routeAt && !transfer.routeTask) void this.route(transfer);
      if (transfer.grant && now() >= transfer.renewAt && !transfer.renewing) { transfer.renewing = true; void this.renew(transfer); }
    }, 100);
    if (signal.aborted) abort(); return transfer;
  }
  private authorize(transfer: Transfer, message: PeerMessage) {
    if (!validPeerAsset(message.media) || !sameAsset(transfer.media, message.media) || message.offererPeerId !== message.requesterPeerId || !validLease(message.leaseMs, 10000) || Number(message.leaseMs) < 300
      || (transfer.role === 'receive' ? message.requesterPeerId !== this.state.peerId || message.sourcePeerId !== transfer.peerId : message.sourcePeerId !== this.state.peerId || message.requesterPeerId !== transfer.peerId)) {
      this.closeTransfer(transfer, '设备传输授权不一致'); return;
    }
    if (transfer.grant) { this.closeTransfer(transfer, '重复设备授权'); return; }
    transfer.grant = message; transfer.leaseUntil = now() + Number(message.leaseMs); transfer.renewAt = now() + Math.min(5000, Number(message.leaseMs) / 2); transfer.grantResolve(message);
  }
  private guardTransfer(transfer: Transfer) {
    this.check();
    if (transfer.closed || transfer.controller.signal.aborted || now() >= transfer.deadline || now() >= transfer.leaseUntil || !this.state.enabled
      || transfer.role === 'send' && !this.cache.canShare(transfer.media.assetId)) throw abortError();
  }
  private options(transfer: Transfer): ChannelOptions {
    return { signal: transfer.controller.signal, guard: () => this.guardTransfer(transfer), progress: () => { transfer.progressAt = now(); }, observe: (payload, control, discarded) => {
      if (payload > 0 && transfer.record.firstByteMs === undefined) transfer.record.firstByteMs = now() - transfer.started;
      transfer.record.bytes += payload; transfer.record.controlBytes += control; transfer.record.discardedBytes += discarded;
      this.state.totals.peer += payload; this.state.totals.control += control; this.state.totals.discarded += discarded; this.paint();
    } };
  }
  private async renew(transfer: Transfer) {
    try {
      const lease = await this.rpc('peer.renew', { transferId: transfer.id }, 'peer.lease', transfer.controller.signal); this.guardTransfer(transfer);
      if (lease.transferId !== transfer.id || !validLease(lease.leaseMs, 10000) || Number(lease.leaseMs) < 300) throw new Error('设备授权剩余时间不足。');
      transfer.leaseUntil = now() + Number(lease.leaseMs); transfer.renewAt = now() + Math.min(5000, Number(lease.leaseMs) / 2);
    } catch { transfer.noRetry = true; this.closeTransfer(transfer, '设备授权已过期'); }
    finally { transfer.renewing = false; }
  }
  private connection(transfer: Transfer) {
    this.guardTransfer(transfer);
    const pc = new RTCPeerConnection({ iceServers: [] }); transfer.pc = pc;
    pc.onicecandidate = event => {
      if (transfer.closed) return;
      if (++transfer.iceCount + transfer.remoteIceCount > 128) { transfer.noRetry = true; this.closeTransfer(transfer, '候选数量超限'); return; }
      const payload = event.candidate?.toJSON() ?? null;
      if (bytesOf(payload) > 2048) { transfer.noRetry = true; this.closeTransfer(transfer, '候选消息超限'); return; }
      if (!transfer.localDescriptionQueued) transfer.localCandidates.push(payload); else this.sendSignal(transfer, 'ice', payload);
    };
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'failed' || pc.connectionState === 'closed' || pc.connectionState === 'disconnected') {
        // The source may close SCTP before its WebSocket completion arrives.
        // After local verification, retain the original stall/lease/deadline bounds while awaiting that signal.
        if (transfer.role === 'receive' && transfer.verified) return;
        this.closeTransfer(transfer, '设备连接中断', true, true);
      }
      else if (pc.connectionState === 'connected') void this.route(transfer);
    };
    return pc;
  }
  private route(transfer: Transfer): Promise<void> {
    if (transfer.routeTask) return transfer.routeTask;
    if (!transfer.pc) return Promise.resolve();
    transfer.routeAt = now() + 1000;
    const task = selectedRoute(transfer.pc, transfer.controller.signal).then(route => {
      if (transfer.closed) return;
      transfer.record.route = mergeRoute(transfer.record.route, route);
      this.paint();
    });
    transfer.routeTask = task; void task.finally(() => { if (transfer.routeTask === task) transfer.routeTask = undefined; }); return task;
  }
  private sendSignal(transfer: Transfer, kind: SignalKind, payload: SignalPayload) {
    transfer.signalBytes += bytesOf(payload);
    if (transfer.signalBytes > 512 * 1024) { transfer.noRetry = true; this.closeTransfer(transfer, '信令总量超限'); return Promise.resolve(); }
    transfer.outgoing = transfer.outgoing.then(async () => {
      this.guardTransfer(transfer);
      const response = await this.rpc('peer.signal', { transferId: transfer.id, kind, payload }, 'peer.signalled', transfer.controller.signal);
      if (response.transferId !== transfer.id) throw new Error('信令确认范围不一致。');
    }).catch(error => { transfer.noRetry = true; this.closeTransfer(transfer, error instanceof Error ? error.message : '信令发送失败'); });
    return transfer.outgoing;
  }
  private sendDescription(transfer: Transfer, kind: 'offer' | 'answer', payload: RTCSessionDescriptionInit) {
    const sent = this.sendSignal(transfer, kind, payload); transfer.localDescriptionQueued = true;
    for (const candidate of transfer.localCandidates.splice(0)) this.sendSignal(transfer, 'ice', candidate);
    return sent;
  }
  private async signal(transfer: Transfer, message: PeerMessage) {
    this.guardTransfer(transfer);
    if (message.fromPeerId !== transfer.peerId || !transfer.grant || !transfer.pc) throw new Error('信令不属于当前传输。');
    const pc = transfer.pc;
    transfer.signalBytes += bytesOf(message.payload);
    if (transfer.signalBytes > 512 * 1024) throw new Error('设备信令总量超限。');
    if (message.kind === 'ice') {
      if (++transfer.remoteIceCount + transfer.iceCount > 128 || bytesOf(message.payload) > 2048) throw new Error('设备候选数量或大小超限。');
      const candidate = message.payload as RTCIceCandidateInit | null;
      if (pc.remoteDescription) await pc.addIceCandidate(candidate ?? undefined); else transfer.candidates.push(candidate);
      return;
    }
    const description = message.payload as RTCSessionDescriptionInit;
    if (!description || !['offer', 'answer'].includes(message.kind ?? '') || description.type !== message.kind || typeof description.sdp !== 'string' || bytesOf(description.sdp) > 49152
      || (transfer.role === 'send' ? message.kind !== 'offer' : message.kind !== 'answer') || pc.remoteDescription) throw new Error('设备会话描述无效。');
    await pc.setRemoteDescription(description); this.guardTransfer(transfer);
    for (const candidate of transfer.candidates.splice(0)) { await pc.addIceCandidate(candidate ?? undefined); this.guardTransfer(transfer); }
    if (transfer.role === 'send') { const answer = await pc.createAnswer(); this.guardTransfer(transfer); await pc.setLocalDescription(answer); this.guardTransfer(transfer); await this.sendDescription(transfer, 'answer', { type: 'answer', sdp: answer.sdp }); }
  }
  private waitForSourceCompletion(transfer: Transfer): Promise<void> {
    this.guardTransfer(transfer);
    if (transfer.sourceCompleted) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const cleanup = () => { transfer.sourceComplete = undefined; transfer.controller.signal.removeEventListener('abort', abort); };
      const abort = () => { cleanup(); reject(abortError()); };
      transfer.sourceComplete = () => { cleanup(); resolve(); };
      transfer.controller.signal.addEventListener('abort', abort, { once: true });
      if (transfer.controller.signal.aborted) abort();
    });
  }
  private closeTransfer(transfer: Transfer, reason: string, notify = true, retryable = false) {
    if (transfer.closed) return; transfer.closed = true; transfer.retryable = retryable; clearInterval(transfer.tick);
    if (notify && this.port.eventState().connected) this.sendPeer({ v: 1, type: 'peer.cancel', requestId: crypto.randomUUID(), transferId: transfer.id, reason: reason === 'complete' ? 'complete' : retryable ? 'transport_error' : 'cancelled' });
    transfer.controller.abort(); transfer.grantReject(new Error(reason));
    transfer.candidates = []; transfer.localCandidates = [];
    if (transfer.pc) { transfer.pc.onicecandidate = null; transfer.pc.onconnectionstatechange = null; transfer.pc.ondatachannel = null; transfer.pc.close(); }
    if (transfer.record.status === 'active') { transfer.record.status = reason === 'complete' ? 'complete' : /取消|cancelled/.test(reason) ? 'cancelled' : 'failed'; transfer.record.reason = reason === 'complete' ? '' : reason; transfer.record.milliseconds = now() - transfer.started; }
    const route = transfer.record.route ?? 'unknown'; transfer.record.route = route; this.state.totals[route] += transfer.record.bytes;
    this.transfers.delete(transfer.id); this.paint();
  }
  private observeLate(channel: RTCDataChannel, transfer: Transfer) {
    const message = (event: MessageEvent) => {
      if (!transfer.closed) return;
      const payload = event.data instanceof ArrayBuffer ? Math.max(0, event.data.byteLength - 8) : 0;
      const control = event.data instanceof ArrayBuffer ? Math.min(8, event.data.byteLength) : typeof event.data === 'string' ? bytesOf(event.data) : 0;
      transfer.record.bytes += payload; transfer.record.discardedBytes += payload; transfer.record.controlBytes += control;
      this.state.totals.peer += payload; this.state.totals[transfer.record.route ?? 'unknown'] += payload; this.state.totals.discarded += payload; this.state.totals.control += control; this.paint();
    };
    channel.addEventListener('message', message); channel.addEventListener('close', () => channel.removeEventListener('message', message), { once: true });
  }
  invalidate(assetIds?: readonly string[]) {
    const selected = assetIds && new Set(assetIds);
    for (const [controller, assetId] of this.downloads) if (!selected || selected.has(assetId)) controller.abort();
    for (const transfer of this.transfers.values()) if (!selected || selected.has(transfer.media.assetId)) this.closeTransfer(transfer, '缓存或会话已变化');
    this.queueInventory();
  }
  private async serve(message: PeerMessage) {
    if (!this.state.enabled || !safeId(message.transferId) || !safeId(message.requesterPeerId) || !validPeerAsset(message.media) || !validLease(message.leaseMs, 5000)) return;
    if (this.state.sending + this.state.receiving >= 2 || this.transfers.has(message.transferId)) { this.sendPeer({ v: 1, type: 'peer.cancel', requestId: crypto.randomUUID(), transferId: message.transferId, reason: 'capacity' }); return; }
    const generation = this.generation, transfer = this.createTransfer(message.transferId, message.media, 'send', message.requesterPeerId, now() + 12000, this.lifetime.signal);
    transfer.leaseUntil = now() + Number(message.leaseMs);
    this.publish({ sending: this.state.sending + 1 });
    try {
      const readAt = now(); const blob = await this.cache.readForPeer(transfer.media, transfer.controller.signal); transfer.record.sourceReadMs = now() - readAt; this.guardTransfer(transfer);
      if (!blob) throw new Error('本页缓存已回收。');
      // Install the peer connection before accept: requester signalling can arrive immediately after authorization.
      const connectedAt = now(), pc = this.connection(transfer);
      const sending = new Promise<void>((resolve, reject) => {
        let attached = false;
        const abort = () => reject(abortError()); transfer.controller.signal.addEventListener('abort', abort, { once: true });
        pc.ondatachannel = event => {
          if (attached || event.channel.label !== 'memoverse-' + transfer.id || !event.channel.ordered || event.channel.maxRetransmits !== null || event.channel.maxPacketLifeTime !== null) { event.channel.close(); reject(new Error('设备数据通道配置无效。')); return; }
          attached = true; transfer.channel = event.channel; this.observeLate(event.channel, transfer);
          event.channel.addEventListener('open', () => { transfer.progressAt = now(); transfer.record.connectionMs = now() - connectedAt; }, { once: true });
          if (event.channel.readyState === 'open') { transfer.progressAt = now(); transfer.record.connectionMs = now() - connectedAt; }
          void sendFile(event.channel, transfer.media, blob, () => pc.sctp?.maxMessageSize, this.options(transfer)).then(resolve, reject).finally(() => transfer.controller.signal.removeEventListener('abort', abort));
        };
      }); void sending.catch(() => {});
      const authorizationAt = now(); await this.rpc('peer.accept', { transferId: transfer.id }, 'peer.authorized', transfer.controller.signal, 4000); await transfer.authorized; transfer.record.authorizationMs = now() - authorizationAt;
      await sending; transfer.verified = true; this.guardTransfer(transfer); await this.route(transfer); this.guardTransfer(transfer); this.closeTransfer(transfer, 'complete');
    } catch (error) { this.closeTransfer(transfer, error instanceof Error ? error.message : '设备发送失败', true, transportFailure(error)); }
    finally { if (generation === this.generation) this.publish({ sending: Math.max(0, this.state.sending - 1) }); }
  }
  private waitForRegistration(signal: AbortSignal) {
    if (this.state.enabled || !this.registering || signal.aborted) return Promise.resolve();
    return new Promise<void>(resolve => {
      const done = () => { clearTimeout(timer); this.listeners.delete(check); signal.removeEventListener('abort', done); resolve(); };
      const check = () => { if (this.state.enabled || !this.registering) done(); };
      const timer = setTimeout(done, 1000); this.listeners.add(check); signal.addEventListener('abort', done, { once: true }); check();
    });
  }
  async tryDownload(media: MediaRef, signal: AbortSignal): Promise<{ blob: Blob; transport: 'peer' } | null> {
    if (!this.allowed() || this.state.receiving + this.state.sending >= 2 || !validPeerAsset(media)) return null;
    const generation = this.generation, controller = new AbortController(), deadline = now() + 12000;
    const abort = () => controller.abort(); signal.addEventListener('abort', abort, { once: true }); if (signal.aborted) abort();
    const overall = setTimeout(abort, 12000); this.downloads.set(controller, media.assetId); this.publish({ receiving: this.state.receiving + 1 });
    const prefix: ReceivePrefix = { chunks: [], offset: 0 }; let succeeded = false, lastTransfer: Transfer | undefined;
    try {
      await this.waitForRegistration(controller.signal);
      if (!this.state.enabled) return null;
      for (let attempt = 0; attempt < 2; attempt++) {
        let transfer: Transfer | undefined;
        try {
          this.check(generation); if (controller.signal.aborted) throw abortError();
          const discoveryAt = now(), discoveryDeadline = Math.min(deadline, discoveryAt + 1000);
          const found = await this.rpc('peer.find', { assetId: media.assetId }, 'peer.sources', controller.signal, discoveryDeadline - now()); this.check(generation);
          if (found.assetId !== media.assetId) throw new Error('设备查找结果不一致。');
          const sources = peerList(found.peers).filter(peer => peer.peerId !== this.state.peerId); if (!sources.length) return null;
          const source = sources[Math.min(attempt, sources.length - 1)]; this.openings++;
          let opened: PeerMessage;
          try { opened = await this.rpc('peer.open', { assetId: media.assetId, targetPeerId: source.peerId }, 'peer.pending', controller.signal, discoveryDeadline - now()); }
          finally { this.openings--; }
          if (!safeId(opened.transferId)) throw new Error('设备传输编号无效。');
          transfer = this.createTransfer(opened.transferId, media, 'receive', source.peerId, deadline, controller.signal, deadline - 12000); lastTransfer = transfer;
          transfer.record.discoveryMs = now() - discoveryAt; transfer.record.resumeOffset = prefix.offset;
          const early = this.early.get(transfer.id); if (early) { this.early.delete(transfer.id); this.authorize(transfer, early); }
          const authorizationAt = now(); await transfer.authorized; transfer.record.authorizationMs = now() - authorizationAt; this.guardTransfer(transfer);
          const connectedAt = now(), pc = this.connection(transfer), channel = pc.createDataChannel('memoverse-' + transfer.id, { ordered: true }); transfer.channel = channel; this.observeLate(channel, transfer);
          const receiving = new Promise<Blob>((resolve, reject) => {
            const current = transfer!; let attached = false;
            const abortReceive = () => reject(abortError()); current.controller.signal.addEventListener('abort', abortReceive, { once: true });
            const timer = setTimeout(() => this.closeTransfer(current, '设备连接超时', true, true), Math.min(4000, Math.max(1, deadline - now())));
            const open = () => {
              if (attached) return; attached = true; clearTimeout(timer); current.progressAt = now(); current.record.connectionMs = now() - connectedAt;
              try {
                void receiveFile(channel, media, pc.sctp?.maxMessageSize ?? 0, prefix, async blob => { const start = now(); try { await verifyMediaBlob(blob, media, () => this.guardTransfer(current)); current.verified = true; } finally { current.record.verificationMs = now() - start; } }, this.options(current))
                  .then(resolve, reject).finally(() => { current.controller.signal.removeEventListener('abort', abortReceive); channel.removeEventListener('open', open); });
              } catch (error) { reject(error); }
            };
            channel.addEventListener('open', open); current.controller.signal.addEventListener('abort', () => { clearTimeout(timer); channel.removeEventListener('open', open); }, { once: true });
          }); void receiving.catch(() => {});
          const offer = await pc.createOffer(); this.guardTransfer(transfer); await pc.setLocalDescription(offer); this.guardTransfer(transfer);
          await this.sendDescription(transfer, 'offer', { type: 'offer', sdp: offer.sdp });
          const blob = await receiving; this.guardTransfer(transfer); await this.route(transfer); await this.waitForSourceCompletion(transfer);
          this.guardTransfer(transfer); this.closeTransfer(transfer, 'complete', false);
          succeeded = true; return { blob, transport: 'peer' };
        } catch (error) {
          const retryable = transfer?.retryable || transportFailure(error);
          if (transfer) this.closeTransfer(transfer, error instanceof Error ? error.message : '设备读取失败', true, retryable);
          if (controller.signal.aborted || transfer?.noRetry || !retryable || error instanceof PeerRPCError || /校验|分块|无效|不一致|超限/.test(error instanceof Error ? error.message : '') || now() >= deadline || attempt === 1) break;
        }
      }
      return null;
    } finally {
      if (prefix.offset && !succeeded) { this.state.totals.discarded += prefix.offset; if (lastTransfer) lastTransfer.record.discardedPrefixBytes = prefix.offset; }
      prefix.chunks = []; prefix.offset = 0; clearTimeout(overall); signal.removeEventListener('abort', abort); this.downloads.delete(controller);
      if (generation === this.generation) this.publish({ receiving: Math.max(0, this.state.receiving - 1) });
    }
  }
}

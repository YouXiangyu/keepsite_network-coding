import { LIMITS, allowedCard, channels, emptyCards, hexId, member, number, operations, sanitizeCard, sanitizeEvent, sanitizeMetadata,
  own, type Card, type Cards, type Channel, type DiagnosticEvent, type Operation } from './schema';
import { SERVER_STATES, parseServerTrace, sanitizeServerEvent, sanitizeServerState,
  type ServerEvent, type ServerState, type ServerStatus } from './serverTrace';

function randomId() {
  const bytes = new Uint8Array(16); globalThis.crypto.getRandomValues(bytes);
  return [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
}
// One browser execution domain, independent of account/capture rotations.
let pageClockId: string | null = null;
try { pageClockId = randomId(); } catch { /* Unknown is safer than inventing a clock identity. */ }
export interface MonitorScope { readonly sessionId: string; readonly channel: Channel; readonly generation: number }
export interface OperationHandle { readonly id: string }
export interface DeviceHandle { readonly id: string }
export interface ExportLease { readonly sessionId: string; readonly history: number; readonly captureId: string | null }
interface OperationEntry { scope: MonitorScope; history: number; started: number | null; traceId: string | null; captureId: string | null; kind: Operation }
// monotonic() already uses the origin of clockId; instances sharing that ID must share its origin.
interface Dependencies { monotonic(): number; wall(): number; id(): string; clockId: string | null }
export type MonitorEvent = DiagnosticEvent | ServerEvent;
export interface MonitorSnapshot {
  readonly active: boolean; readonly detailed: boolean; readonly paused: boolean;
  readonly sessionId: string | null; readonly clockId: string | null; readonly captureId: string | null;
  readonly cards: Cards; readonly events: readonly Readonly<MonitorEvent>[]; readonly server: Readonly<ServerState>;
  readonly limits: typeof LIMITS;
  readonly metrics: Readonly<{ recorded: number; dropped: number; rejected: number; retainedBytes: number; activeOperations: number; deviceAliases: number; serverClocks: number; captureCostMs: number | null }>;
}
export class NetworkMonitor {
  private dependencies: Dependencies;
  private sessionId: string | null = null;
  private generations: Record<Channel, number> = { http: 0, ws: 0, sync: 0, peer: 0, media: 0, server: 0 };
  private detailed = false;
  private paused = false;
  private captureId: string | null = null;
  private history = 0;
  private sequence = 0;
  private nextOperation = 0;
  private nextDevice = 0;
  private cards = emptyCards();
  private records: { event: MonitorEvent; bytes: number }[] = [];
  private serverState: Readonly<ServerState> = Object.freeze({ state: 'idle' });
  private serverClocks = new Map<string, number>();
  private retainedBytes = 0;
  private recorded = 0;
  private dropped = 0;
  private rejected = 0;
  private captureCostMs: number | null = 0;
  private activeOperations = new Map<OperationHandle, OperationEntry>();
  private devices = new Map<DeviceHandle, MonitorScope>();
  private listeners = new Set<() => void>();
  private invalidationListeners = new Set<() => void>();
  private notificationPending = false;
  private cached: MonitorSnapshot | undefined;
  constructor(dependencies: Partial<Dependencies> = {}) {
    this.dependencies = { monotonic: () => performance.now(), wall: () => Date.now(), id: randomId, clockId: pageClockId, ...dependencies };
  }
  private clock(kind: 'monotonic' | 'wall') {
    try { const value = this.dependencies[kind](); return number(value) ? value : null; } catch { return null; }
  }
  private id() {
    try { const value = this.dependencies.id(); return hexId(value) ? value : null; } catch { return null; }
  }
  private changed() {
    this.cached = undefined;
    if (this.notificationPending) return;
    this.notificationPending = true;
    queueMicrotask(() => {
      this.notificationPending = false;
      for (const listener of [...this.listeners]) { try { listener(); } catch { /* Observers never change business outcomes. */ } }
    });
  }
  private measured<T>(action: () => T): T {
    const before = this.clock('monotonic');
    try { return action(); }
    finally {
      const after = this.clock('monotonic');
      this.captureCostMs = this.captureCostMs !== null && before !== null && after !== null && after >= before
        ? this.captureCostMs + after - before : null;
      this.changed();
    }
  }
  subscribe = (listener: () => void) => {
    if (this.listeners.size >= LIMITS.active) return () => {};
    this.listeners.add(listener); return () => { this.listeners.delete(listener); };
  };
  onInvalidated(listener: () => void): (() => void) | undefined {
    if (this.invalidationListeners.size >= LIMITS.active) return;
    this.invalidationListeners.add(listener); return () => { this.invalidationListeners.delete(listener); };
  }
  private invalidated() {
    for (const listener of [...this.invalidationListeners]) { try { listener(); } catch { /* Cancellation cannot affect business state. */ } }
  }
  startAccount() {
    this.stopAccount();
    this.sessionId = this.id();
    this.changed();
    return this.sessionId !== null;
  }
  stopAccount() {
    this.sessionId = null; this.captureId = null; this.detailed = false; this.paused = false; this.history++;
    this.cards = emptyCards(); this.records = []; this.retainedBytes = 0; this.activeOperations.clear(); this.devices.clear();
    this.recorded = this.dropped = this.rejected = this.sequence = this.nextOperation = this.nextDevice = 0;
    this.captureCostMs = 0;
    this.serverState = Object.freeze({ state: 'idle' }); this.serverClocks.clear();
    for (const channel of channels) this.generations[channel]++;
    this.changed(); this.invalidated();
  }
  scope(channel: Channel): MonitorScope | undefined {
    return this.sessionId && member(channel, channels) ? Object.freeze({ sessionId: this.sessionId, channel, generation: this.generations[channel] }) : undefined;
  }
  valid(scope: MonitorScope | undefined): scope is MonitorScope {
    const channel = own(scope, 'channel');
    return !!this.sessionId && own(scope, 'sessionId') === this.sessionId && member(channel, channels) && own(scope, 'generation') === this.generations[channel];
  }
  replaceConnection(channel: Channel) {
    if (!member(channel, channels) || !this.sessionId) return undefined;
    this.generations[channel]++;
    for (const [handle, entry] of this.activeOperations) if (entry.scope.channel === channel) this.activeOperations.delete(handle);
    for (const [handle, scope] of this.devices) if (scope.channel === channel) this.devices.delete(handle);
    const card = ({ http: 'api', ws: 'websocket', sync: 'sync', peer: 'peer' } as Partial<Record<Channel, Card>>)[channel];
    if (card) this.cards[card] = emptyCards()[card];
    if (channel === 'server') this.serverState = sanitizeServerState({ state: 'idle', source: this.serverState.source });
    this.changed(); this.invalidated(); return this.scope(channel);
  }
  private rotateHistory(clear: boolean) {
    this.history++; this.activeOperations.clear();
    if (clear) { this.records = []; this.retainedBytes = 0; }
    this.captureId = this.sessionId && this.detailed && !this.paused ? this.id() : null;
    // If randomness fails, do not collect or emit headers for an invalid capture.
    if (this.detailed && !this.paused && !this.captureId) this.detailed = false;
    this.serverState = Object.freeze({ state: 'idle' }); this.serverClocks.clear();
    this.changed(); this.invalidated();
  }
  setDetailed(value: boolean) {
    if (!this.sessionId || typeof value !== 'boolean' || this.detailed === value) return;
    this.detailed = value; this.rotateHistory(false);
  }
  setPaused(value: boolean) {
    if (!this.sessionId || typeof value !== 'boolean' || this.paused === value) return;
    this.paused = value; this.rotateHistory(false);
  }
  clearHistory() { if (this.sessionId) this.rotateHistory(true); }
  update(scope: MonitorScope | undefined, card: Card, values: unknown) {
    if (!this.valid(scope) || !allowedCard(scope.channel, card)) return false;
    return this.measured(() => { Object.assign(this.cards[card], sanitizeCard(card, values)); return true; });
  }
  beginOperation(scope: MonitorScope | undefined, kind: Operation): OperationHandle | undefined {
    if (!this.valid(scope) || !member(kind, operations)) return undefined;
    if (this.activeOperations.size >= LIMITS.active) { this.dropped++; this.changed(); return undefined; }
    const handle = Object.freeze({ id: 'op-' + ++this.nextOperation });
    this.activeOperations.set(handle, { scope, kind, history: this.history, started: this.clock('monotonic'),
      captureId: this.captureId, traceId: this.detailed && !this.paused ? this.id() : null });
    this.changed(); return handle;
  }
  device(scope: MonitorScope | undefined): DeviceHandle | undefined {
    if (!this.valid(scope)) return undefined;
    if (this.devices.size >= LIMITS.devices) { this.dropped++; this.changed(); return undefined; }
    const handle = Object.freeze({ id: 'device-' + ++this.nextDevice }); this.devices.set(handle, scope); this.changed(); return handle;
  }
  releaseDevice(handle: DeviceHandle) { if (this.devices.delete(handle)) this.changed(); }
  traceContext(handle: OperationHandle): Readonly<{ captureId: string; traceId: string }> | null {
    const entry = this.activeOperations.get(handle);
    return entry && this.valid(entry.scope) && entry.history === this.history && this.detailed && !this.paused &&
      entry.captureId === this.captureId && entry.captureId && entry.traceId
      ? Object.freeze({ captureId: entry.captureId, traceId: entry.traceId }) : null;
  }
  record(scope: MonitorScope | undefined, input: unknown, operation?: OperationHandle, device?: DeviceHandle) {
    if (!this.valid(scope)) return false;
    return this.measured(() => {
      if (!this.detailed || this.paused || !this.captureId) return false;
      const entry = operation ? this.activeOperations.get(operation) : undefined;
      if (operation && (!entry || !this.valid(entry.scope) || entry.history !== this.history)) return false;
      const metadata = sanitizeMetadata(input);
      if (!metadata) { this.rejected++; return false; }
      const current = this.clock('monotonic'), timestamp = this.clock('wall');
      const event: DiagnosticEvent = { ...metadata, sequence: ++this.sequence, timestamp, origin: 'client',
        offsetMs: current };
      if (operation && entry) { event.operationId = operation.id; if (entry.traceId) event.traceId = entry.traceId; }
      const deviceScope = device && this.devices.get(device);
      if (device && this.valid(deviceScope)) event.deviceId = device.id;
      const safe = sanitizeEvent(event)!;
      const bytes = new TextEncoder().encode(JSON.stringify(safe)).byteLength;
      if (bytes > LIMITS.eventBytes) { this.dropped++; return false; }
      this.retain(safe, bytes);
      return true;
    });
  }
  finishOperation(scope: MonitorScope | undefined, handle: OperationHandle, input: unknown, device?: DeviceHandle) {
    if (!this.valid(scope)) return false;
    const entry = this.activeOperations.get(handle);
    if (!entry || !this.valid(entry.scope)) return false;
    try {
      const current = this.clock('monotonic'), metadata = sanitizeMetadata(input);
      const durationMs = entry.started !== null && current !== null && current >= entry.started ? current - entry.started : undefined;
      return metadata && metadata.status !== 'started' && metadata.status !== 'retrying'
        ? this.record(scope, { ...metadata, ...(durationMs === undefined ? {} : { durationMs }) }, handle, device) : false;
    } finally { this.activeOperations.delete(handle); this.changed(); }
  }
  exportLease(): ExportLease | null {
    return this.sessionId ? Object.freeze({ sessionId: this.sessionId, history: this.history, captureId: this.captureId }) : null;
  }
  validExport(lease: ExportLease | null) {
    return !!this.sessionId && own(lease, 'sessionId') === this.sessionId && own(lease, 'history') === this.history && own(lease, 'captureId') === this.captureId;
  }
  private retain(event: MonitorEvent, bytes: number) {
    this.records.push({ event, bytes }); this.retainedBytes += bytes; this.recorded++;
    while (this.records.length > LIMITS.events || this.retainedBytes > LIMITS.bytes) {
      this.retainedBytes -= this.records.shift()!.bytes; this.dropped++;
    }
  }
  setServerStatus(lease: ExportLease | null, scope: MonitorScope | undefined, state: ServerStatus) {
    if (!this.validExport(lease) || !this.valid(scope) || scope.channel !== 'server' || !member(state, SERVER_STATES)) return false;
    this.serverState = sanitizeServerState({ state, source: this.serverState.source }); this.changed(); return true;
  }
  importServer(lease: ExportLease | null, scope: MonitorScope | undefined, input: unknown): ServerStatus {
    if (!this.validExport(lease) || !this.valid(scope) || scope.channel !== 'server' || !this.detailed || this.paused || !this.captureId) return 'cancelled';
    const expectedCapture = this.captureId;
    return this.measured(() => {
      const batch = parseServerTrace(input, expectedCapture);
      if (!this.validExport(lease) || !this.valid(scope) || this.captureId !== expectedCapture) return 'cancelled';
      let state: ServerStatus;
      if (!batch) { this.rejected++; state = 'invalid'; }
      else if (batch.expiresAt === null) state = 'unavailable';
      else {
        const previous = this.serverClocks.get(batch.clockId);
        if (previous !== undefined && batch.next < previous) state = 'stale';
        else if (previous === undefined && this.serverClocks.size >= LIMITS.active) state = 'capacity';
        else {
          for (const event of batch.events) if (event.sequence > (previous ?? 0)) this.retain(event, new TextEncoder().encode(JSON.stringify(event)).byteLength);
          this.serverClocks.set(batch.clockId, batch.next); state = 'ready';
        }
      }
      this.serverState = sanitizeServerState({ state, source: batch ?? this.serverState.source });
      return state;
    });
  }
  snapshot = (): MonitorSnapshot => {
    if (this.cached) return this.cached;
    const cards = Object.fromEntries(Object.keys(this.cards).map(card => [card, Object.freeze(sanitizeCard(card as Card, this.cards[card as Card]))])) as Cards;
    const events = this.records.map(({ event }) => Object.freeze(event.origin === 'server' ? sanitizeServerEvent(event)! : sanitizeEvent(event)!));
    this.cached = Object.freeze({ active: this.sessionId !== null, detailed: this.detailed, paused: this.paused, sessionId: this.sessionId,
      clockId: hexId(this.dependencies.clockId) ? this.dependencies.clockId : null, captureId: this.captureId, cards: Object.freeze(cards),
      events: Object.freeze(events), server: sanitizeServerState(this.serverState), limits: LIMITS, metrics: Object.freeze({ recorded: this.recorded, dropped: this.dropped, rejected: this.rejected,
        retainedBytes: this.retainedBytes, activeOperations: this.activeOperations.size, deviceAliases: this.devices.size, serverClocks: this.serverClocks.size, captureCostMs: this.captureCostMs }) });
    return this.cached;
  };
  exportSnapshot(lease: ExportLease | null) {
    if (!this.validExport(lease)) return null;
    // snapshot() sanitizes again instead of dumping collector, request or business state.
    return Object.freeze({ v: 1, capturedAt: this.clock('wall'), ...this.snapshot() });
  }
}

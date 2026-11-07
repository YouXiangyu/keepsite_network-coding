import { NetworkMonitor, type ExportLease, type MonitorScope, type OperationHandle } from './monitor';
import { number, type Metadata } from './schema';

export const networkMonitor = new NetworkMonitor();
/** This boundary cannot throw into a request, socket or synchronization state machine. */
export function observe<T>(action: () => T): T | undefined { try { return action(); } catch { return undefined; } }
const mono = () => { try { const value = performance.now(); return number(value) ? value : null; } catch { return null; } };
const wall = () => { const value = Date.now(); return number(value) ? value : null; };
const elapsed = (start: number | null) => { const end = mono(); return start !== null && end !== null && end >= start ? end - start : null; };
type ErrorCode = NonNullable<Metadata['errorCode']>;
type Outcome = 'completed' | 'failed' | 'cancelled';
let activeHttp = 0;
let nextRequest = 0;
let currentSocket: SocketObservation | undefined;
const requests = new Map<HttpObservation, string>();
export interface ActiveHttpRequest { readonly alias: string; readonly template: HttpObservation['template']; readonly startedMs: number | null; readonly phase: 'requesting' | 'headers'; readonly status: number | null }
export function httpActivity(): Readonly<{ total: number; omitted: number; requests: readonly ActiveHttpRequest[] }> {
  const current = [...requests].filter(([request]) => !request.ended && networkMonitor.valid(request.scope));
  const list = current.map(([request, alias]) => Object.freeze({ alias, template: request.template, startedMs: request.started,
    phase: request.status === null ? 'requesting' as const : 'headers' as const, status: request.status }));
  return Object.freeze({ total: activeHttp, omitted: Math.max(0, activeHttp - list.length), requests: Object.freeze(list) });
}
export function accountMonitoring(active: boolean) {
  observe(() => { activeHttp = 0; nextRequest = 0; requests.clear(); currentSocket = undefined; if (active) networkMonitor.startAccount(); else networkMonitor.stopAccount(); });
}
export function requestTemplate(path: string): NonNullable<Metadata['template']> {
  const route = path.split('?')[0];
  if (route === '/api/auth/session') return 'session';
  if (route.startsWith('/api/auth/account')) return 'account';
  if (route.startsWith('/api/auth/')) return 'authentication';
  if (route === '/api/sync/snapshot') return 'sync.snapshot';
  if (route === '/api/sync/changes') return 'sync.delta';
  if (route === '/api/media') return 'upload';
  if (/^\/api\/media\/[^/]+\/download$/.test(route)) return 'media.authorize';
  if (/^\/api\/media\/[^/]+$/.test(route)) return 'media.download';
  if (route.startsWith('/api/tickets') || route.startsWith('/api/operations/') || route.startsWith('/api/uploads/')) return 'tickets';
  return 'unknown';
}
export interface HttpObservation {
  scope: MonitorScope; operation?: OperationHandle; template: NonNullable<Metadata['template']>;
  started: number | null; status: number | null; ended: boolean;
}
export function startHttp(template: HttpObservation['template'], parent?: OperationHandle): HttpObservation | undefined {
  return observe(() => {
    const scope = networkMonitor.scope('http'); if (!scope) return;
    const operation = networkMonitor.beginOperation(scope, 'request', parent);
    const observation: HttpObservation = { scope, operation, template, started: mono(), status: null, ended: false };
    if (requests.size < 32) requests.set(observation, 'request-' + ++nextRequest);
    activeHttp++; observe(() => networkMonitor.update(scope, 'api', { active: activeHttp, state: 'checking' }));
    if (operation) observe(() => networkMonitor.record(scope, { protocol: 'http', event: 'http.request', status: 'started', template }, operation));
    return observation;
  });
}
export function requestDiagnosticHeaders(headers: Headers, observation: HttpObservation | undefined, eligible: boolean) {
  observe(() => {
    headers.delete('X-Keepsite-Diagnostics'); headers.delete('X-Keepsite-Trace-Id');
    const context = eligible && observation?.operation && networkMonitor.traceContext(observation.operation);
    if (context) { headers.set('X-Keepsite-Diagnostics', context.captureId); headers.set('X-Keepsite-Trace-Id', context.traceId); }
  });
}
export function httpHeaders(observation: HttpObservation | undefined, status: number) {
  observe(() => {
    if (!observation || observation.ended || !networkMonitor.valid(observation.scope)) return;
    observation.status = number(status, true) && status >= 100 && status <= 599 ? status : null;
    networkMonitor.update(observation.scope, 'api', { headersMs: elapsed(observation.started), status: observation.status,
      state: status >= 500 ? 'degraded' : 'reachable', auth: status === 401 ? 'required' : status === 403 ? 'forbidden' : status < 400 ? 'ok' : 'unknown' });
  });
}
export function finishHttp(observation: HttpObservation | undefined, status: Outcome, errorCode?: ErrorCode) {
  observe(() => {
    if (!observation || observation.ended) return; observation.ended = true;
    requests.delete(observation);
    if (!networkMonitor.valid(observation.scope)) return;
    activeHttp = Math.max(0, activeHttp - 1);
    try {
      networkMonitor.update(observation.scope, 'api', { durationMs: elapsed(observation.started), errorCode: errorCode ?? null,
        ...(status === 'completed' ? { state: 'reachable', lastSuccessAt: wall() } : status === 'failed'
          ? { state: observation.status ? observation.status < 500 ? 'reachable' : 'degraded' : errorCode === 'invalid' ? 'degraded' : 'offline', lastFailureAt: wall() } : {}) });
    } finally {
      observe(() => networkMonitor.update(observation.scope, 'api', { active: activeHttp }));
      if (observation.operation) observe(() => networkMonitor.finishOperation(observation.scope, observation.operation!,
        { protocol: 'http', event: 'http.request', status, template: observation.template, ...(observation.status === null ? {} : { httpStatus: observation.status }), errorCode }));
    }
  });
}
export interface SocketObservation {
  scope: MonitorScope; operation?: OperationHandle; history: ExportLease | null; openedAt: number | null; openedMono: number | null; lastMessage: number | null; ended: boolean;
}
export function socketActivity() {
  const socket = currentSocket;
  return Object.freeze({ connectedDurationMs: socket && !socket.ended && networkMonitor.valid(socket.scope) ? elapsed(socket.openedMono) : null });
}
export function startSocket(timeouts?: { initialTimeoutMs: number; watchdogMs: number }): SocketObservation | undefined {
  return observe(() => {
    const scope = networkMonitor.replaceConnection('ws'); if (!scope) return;
    const operation = networkMonitor.beginOperation(scope, 'peer');
    const observation: SocketObservation = { scope, operation, history: observe(() => networkMonitor.exportLease()) ?? null, openedAt: null, openedMono: null, lastMessage: null, ended: false };
    currentSocket = observation;
    observe(() => networkMonitor.update(scope, 'websocket', { state: 'connecting', heartbeatConfiguredMs: null, diagnosticsVersion: null,
      initialTimeoutMs: timeouts?.initialTimeoutMs ?? null, watchdogMs: timeouts?.watchdogMs ?? null,
      retryDelayMs: null, retryAt: null, closeCode: null, errorCode: null }));
    if (operation) observe(() => networkMonitor.record(scope, { protocol: 'ws', event: 'ws.connection', status: 'started' }, operation));
    return observation;
  });
}
export function socketOpen(connection: SocketObservation | undefined) {
  observe(() => {
    if (!connection || connection.ended || !networkMonitor.valid(connection.scope)) return;
    if (connection.openedAt === null) connection.openedAt = wall();
    if (connection.openedMono === null) connection.openedMono = mono();
    networkMonitor.update(connection.scope, 'websocket', { state: 'open', connectedSince: connection.openedAt });
  });
}
export function socketMessage(connection: SocketObservation | undefined, type: 'sync.ready' | 'sync.hint' | 'peer') {
  observe(() => {
    if (!connection || connection.ended || !networkMonitor.valid(connection.scope)) return;
    const now = mono(), interval = now !== null && connection.lastMessage !== null && now >= connection.lastMessage ? now - connection.lastMessage : null;
    connection.lastMessage = now;
    networkMonitor.update(connection.scope, 'websocket', { lastMessageAt: wall(), messageIntervalMs: interval, messageType: type });
    // A new validated message is an instantaneous observation, even after a history clear.
    networkMonitor.record(connection.scope, { protocol: 'ws', event: 'ws.message', status: 'completed' });
  });
}
export function socketReady(connection: SocketObservation | undefined, heartbeatMs: unknown, diagnosticsVersion: unknown) {
  observe(() => {
    if (!connection || connection.ended || !networkMonitor.valid(connection.scope)) return;
    networkMonitor.update(connection.scope, 'websocket', { state: 'ready', retryDelayMs: null, retryAt: null,
      heartbeatConfiguredMs: number(heartbeatMs) && heartbeatMs > 0 ? heartbeatMs : null, diagnosticsVersion: diagnosticsVersion === 1 ? 1 : null });
  });
}
export function socketClose(connection: SocketObservation | undefined, code: number | null = null, errorCode?: ErrorCode) {
  observe(() => {
    if (!connection || connection.ended || !networkMonitor.valid(connection.scope)) return;
    connection.ended = true;
    try {
      networkMonitor.update(connection.scope, 'websocket', { state: 'closed', connectedSince: null, closeCode: code, errorCode: errorCode ?? null,
        heartbeatConfiguredMs: null, diagnosticsVersion: null });
    } finally {
      if (connection.operation) observe(() => networkMonitor.finishOperation(connection.scope, connection.operation!,
        { protocol: 'ws', event: 'ws.connection', status: errorCode ? 'failed' : 'completed', errorCode }));
    }
  });
}
export function socketRetry(connection: SocketObservation | undefined, delay: number) {
  observe(() => {
    if (!connection || !networkMonitor.valid(connection.scope)) return;
    networkMonitor.update(connection.scope, 'websocket', { state: 'reconnecting', retryDelayMs: delay, retryAt: Date.now() + delay });
    if (networkMonitor.validExport(connection.history)) networkMonitor.record(connection.scope, { protocol: 'ws', event: 'ws.retry', status: 'retrying', durationMs: delay });
  });
}
export function socketError(connection: SocketObservation | undefined) {
  observe(() => {
    if (!connection || connection.ended) return;
    networkMonitor.update(connection.scope, 'websocket', { errorCode: 'network' });
  });
}
export function stopSocket(connection: SocketObservation | undefined) {
  observe(() => {
    if (!connection || !networkMonitor.valid(connection.scope)) return;
    socketClose(connection); networkMonitor.replaceConnection('ws');
  });
}
export interface SyncObservation { scope: MonitorScope }
export interface PullObservation { scope: MonitorScope; operation?: OperationHandle; kind: 'snapshot' | 'delta'; started: number | null; ended: boolean }
export function startSync(): SyncObservation | undefined {
  return observe(() => { const scope = networkMonitor.replaceConnection('sync'); return scope ? { scope } : undefined; });
}
export function stopSync(sync: SyncObservation | undefined) {
  observe(() => { if (sync && networkMonitor.valid(sync.scope)) networkMonitor.replaceConnection('sync'); });
}
export function syncWatermark(sync: SyncObservation | undefined, cursor: number) {
  observe(() => {
    if (!sync || !networkMonitor.valid(sync.scope)) return;
    const state = networkMonitor.snapshot().cards.sync, current = state.localCursor;
    const head = number(state.serverCursor, true) ? Math.max(state.serverCursor, cursor) : cursor;
    const behind = !number(current, true) || head > current;
    const lifecycle = state.state === 'snapshot' || state.state === 'delta' || state.state === 'failed';
    networkMonitor.update(sync.scope, 'sync', { serverCursor: head,
      ...(behind ? { pending: true, ...(!lifecycle ? { state: 'catching_up' } : {}) } : {}) });
  });
}
export function syncRetry(sync: SyncObservation | undefined, delay: number | null) {
  observe(() => { if (sync) networkMonitor.update(sync.scope, 'sync', { retryDelayMs: delay, retryAt: delay === null ? null : Date.now() + delay }); });
}
export function startPull(sync: SyncObservation | undefined, kind: 'snapshot' | 'delta'): PullObservation | undefined {
  return observe(() => {
    if (!sync || !networkMonitor.valid(sync.scope)) return;
    const operation = networkMonitor.beginOperation(sync.scope, kind);
    const observation: PullObservation = { scope: sync.scope, operation, kind, started: mono(), ended: false };
    observe(() => networkMonitor.update(sync.scope, 'sync', { state: kind, pending: true }));
    if (operation) observe(() => networkMonitor.record(sync.scope, { protocol: 'sync', event: kind === 'snapshot' ? 'sync.snapshot' : 'sync.delta', status: 'started' }, operation));
    return observation;
  });
}
export function finishPull(pull: PullObservation | undefined, status: Outcome, applied?: { cursor: number; head: number; count: number; pending: boolean }, errorCode: ErrorCode = 'unknown') {
  observe(() => {
    if (!pull || pull.ended) return; pull.ended = true;
    if (!networkMonitor.valid(pull.scope)) return;
    const event = pull.kind === 'snapshot' ? 'sync.snapshot' : 'sync.delta';
    try {
      if (applied && status === 'completed') {
        const previous = networkMonitor.snapshot().cards.sync.serverCursor;
        const head = number(previous, true) ? Math.max(previous, applied.head) : applied.head;
        const pending = applied.pending || head > applied.cursor;
        networkMonitor.update(pull.scope, 'sync', { localCursor: applied.cursor, serverCursor: head,
          pending, state: pending ? 'catching_up' : 'current', lastDurationMs: elapsed(pull.started), lastCount: applied.count, errorCode: null,
          [pull.kind === 'snapshot' ? 'lastSnapshotAt' : 'lastDeltaAt']: wall() });
      }
      else if (status === 'failed') networkMonitor.update(pull.scope, 'sync', { state: 'failed', pending: true, errorCode });
    } finally {
      if (pull.operation) observe(() => networkMonitor.finishOperation(pull.scope, pull.operation!,
        { protocol: 'sync', event, status, ...(status === 'failed' ? { errorCode } : {}) }));
    }
  });
}

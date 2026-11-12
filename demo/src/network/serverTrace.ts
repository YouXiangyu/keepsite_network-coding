import { LIMITS, hexId, member, number, own, statuses, type Status } from './schema';

export const SERVER_EVENTS = Object.freeze({
  'http.request': 'http', 'sql.commit': 'sql', 'sql.rollback': 'sql',
  'redis.snapshot.read': 'redis', 'redis.snapshot.write': 'redis', 'redis.stream.publish': 'redis',
  'redis.stream.receive': 'redis', 'redis.stream.ack': 'redis', 'redis.pubsub.publish': 'redis', 'redis.pubsub.receive': 'redis',
  'ws.ready': 'ws', 'ws.hint': 'ws', 'p2p.signaling': 'p2p',
  's3.open': 's3', 's3.read': 's3', 's3.head': 's3', 's3.put': 's3',
  'worker.prepare': 'worker', 'worker.ocr': 'worker', 'worker.poster': 'worker', 'worker.publish': 'worker', 'worker.retry': 'worker',
} as const);
const serverErrors = Object.freeze(['operation_failed', 'cancelled', 'unavailable', 'timeout', 'retry_scheduled'] as const);
const installed = Object.freeze(['http', 'sql', 'redis', 'ws', 's3', 'worker', 'p2p'] as const);
export const SERVER_STATES = Object.freeze(['idle', 'loading', 'ready', 'unavailable', 'invalid', 'capacity', 'stale', 'cancelled'] as const);
export type ServerStatus = typeof SERVER_STATES[number];
export const RESPONSE_BYTES = 256 * 1024;
export const JSON_DEPTH = 16;
export const identifier = (value: unknown): value is string => hexId(value) && value !== '0'.repeat(32);
type ServerName = keyof typeof SERVER_EVENTS;
export interface ServerEvent {
  origin: 'server'; captureId: string; clockId: string; sequence: number; timestamp: number;
  offsetMs: number; durationMs: number; startOffsetMs?: number;
  event: ServerName; protocol: typeof SERVER_EVENTS[ServerName]; status: Status;
  traceId: string | null; spanId: string; parentSpanId: string | null;
  bytes?: number; attempt?: number; errorCode?: typeof serverErrors[number]; coalesced?: boolean;
}
export interface ServerSummary {
  v: 1; captureId: string; clockId: string; next: number; dropped: number; capacity: 200; retentionMs: 600000; expiresAt: number | null;
  instrumented: readonly typeof installed[number][];
  metrics: Readonly<{ recorded: number; dropped: number; captureCostMs: number }>;
}
export interface ServerBatch extends ServerSummary { events: readonly Readonly<ServerEvent>[] }
export interface ServerState { state: ServerStatus; source?: Readonly<ServerSummary> }

export function sanitizeServerEvent(input: unknown): ServerEvent | null {
  try {
    const event = own(input, 'event'), protocol = own(input, 'protocol'), status = own(input, 'status');
    const captureId = own(input, 'captureId'), clockId = own(input, 'clockId');
    const sequence = own(input, 'sequence'), timestamp = own(input, 'timestamp'), offsetMs = own(input, 'offsetMs'), durationMs = own(input, 'durationMs');
    const traceId = own(input, 'traceId'), spanId = own(input, 'spanId'), parentSpanId = own(input, 'parentSpanId');
    if (own(input, 'origin') !== 'server' || typeof event !== 'string' || !Object.hasOwn(SERVER_EVENTS, event)
      || protocol !== SERVER_EVENTS[event as ServerName] || !member(status, statuses) || !identifier(captureId) || !identifier(clockId)
      || !number(sequence, true) || sequence < 1 || !number(timestamp) || !number(offsetMs) || !number(durationMs)
      || !(traceId === null || identifier(traceId)) || !identifier(spanId) || !(parentSpanId === null || identifier(parentSpanId))) return null;
    const result: ServerEvent = { origin: 'server', event: event as ServerName, protocol: protocol as ServerEvent['protocol'], status,
      captureId, clockId, sequence, timestamp, offsetMs, durationMs, traceId, spanId, parentSpanId };
    const start = own(input, 'startOffsetMs');
    if (start !== undefined) {
      if (!number(start) || start > offsetMs) return null;
      // Server duration rounds to 0.001ms; account for floating subtraction at this clock scale.
      const tolerance = 0.001 + 2 * Number.EPSILON * Math.max(1, start, offsetMs);
      if (durationMs > offsetMs - start + tolerance) return null;
      result.startOffsetMs = start;
    }
    for (const key of ['bytes', 'attempt'] as const) {
      const value = own(input, key); if (value !== undefined) { if (!number(value, true)) return null; result[key] = value; }
    }
    const error = own(input, 'errorCode'), coalesced = own(input, 'coalesced');
    if (error !== undefined) { if (!member(error, serverErrors)) return null; result.errorCode = error; }
    if (coalesced !== undefined) {
      if (typeof coalesced !== 'boolean' || coalesced && (traceId !== null || parentSpanId !== null)) return null;
      result.coalesced = coalesced;
    }
    return result;
  } catch { return null; }
}
export function sanitizeServerSummary(input: unknown): Readonly<ServerSummary> | null {
  try {
    const captureId = own(input, 'captureId'), clockId = own(input, 'clockId'), next = own(input, 'next'), dropped = own(input, 'dropped'), expiresAt = own(input, 'expiresAt');
    const metrics = own(input, 'metrics'), recorded = own(metrics, 'recorded'), metricDropped = own(metrics, 'dropped'), captureCostMs = own(metrics, 'captureCostMs');
    const list = own(input, 'instrumented'), listLength = own(list, 'length');
    if (own(input, 'v') !== 1 || !identifier(captureId) || !identifier(clockId) || own(input, 'capacity') !== 200 || own(input, 'retentionMs') !== 600000
      || !number(next, true) || !number(dropped, true) || !(expiresAt === null || number(expiresAt))
      || !number(recorded, true) || recorded > next || !number(metricDropped, true) || metricDropped !== dropped || !number(captureCostMs)
      || !Array.isArray(list) || !number(listLength, true) || listLength > installed.length) return null;
    const instrumented: typeof installed[number][] = [];
    for (let i = 0; i < listLength; i++) {
      const value = own(list, String(i)); if (!member(value, installed)) return null;
      if (!instrumented.includes(value)) instrumented.push(value);
    }
    return Object.freeze({ v: 1, captureId, clockId, next, dropped, capacity: 200, retentionMs: 600000, expiresAt,
      instrumented: Object.freeze(instrumented), metrics: Object.freeze({ recorded, dropped: metricDropped, captureCostMs }) });
  } catch { return null; }
}
export function parseServerTrace(input: unknown, expectedCapture: unknown): Readonly<ServerBatch> | null {
  try {
    const summary = sanitizeServerSummary(input), rawEvents = own(input, 'events'), eventCount = own(rawEvents, 'length');
    if (!summary || summary.captureId !== expectedCapture || !Array.isArray(rawEvents) || !number(eventCount, true) || eventCount > LIMITS.events) return null;
    const events: Readonly<ServerEvent>[] = []; let previous = 0, bytes = 0;
    for (let i = 0; i < eventCount; i++) {
      const raw = own(rawEvents, String(i));
      // Build a fixed shallow input, without spreading untrusted values or evaluating accessors.
      const data: Record<string, unknown> = { captureId: summary.captureId, clockId: summary.clockId };
      for (const key of ['origin', 'sequence', 'timestamp', 'offsetMs', 'durationMs', 'startOffsetMs', 'event', 'protocol', 'status', 'traceId', 'spanId', 'parentSpanId', 'bytes', 'attempt', 'errorCode', 'coalesced']) data[key] = own(raw, key);
      const event = sanitizeServerEvent(data);
      if (!event || event.sequence <= previous || event.sequence > summary.next) return null;
      const size = new TextEncoder().encode(JSON.stringify(event)).byteLength;
      bytes += size; if (size > LIMITS.eventBytes || bytes > LIMITS.bytes) return null;
      previous = event.sequence; events.push(Object.freeze(event));
    }
    if (summary.metrics.recorded < events.length || summary.expiresAt === null && events.length) return null;
    return Object.freeze({ ...summary, events: Object.freeze(events) });
  } catch { return null; }
}
export function sanitizeServerState(input: unknown): Readonly<ServerState> {
  const state = own(input, 'state'), source = sanitizeServerSummary(own(input, 'source'));
  return Object.freeze({ state: member(state, SERVER_STATES) ? state : 'idle', ...(source ? { source } : {}) });
}

/** Only a depth guard; JSON.parse remains the syntax authority. */
export function parseDiagnosticJson(text: string): unknown {
  if (text.length > RESPONSE_BYTES || new TextEncoder().encode(text).byteLength > RESPONSE_BYTES) throw new Error('Invalid diagnostic response');
  let depth = 0, quoted = false, escaped = false;
  for (const character of text) {
    if (quoted) { if (escaped) escaped = false; else if (character === '\\') escaped = true; else if (character === '"') quoted = false; }
    else if (character === '"') quoted = true;
    else if (character === '{' || character === '[') { if (++depth > JSON_DEPTH) throw new Error('Invalid diagnostic response'); }
    else if (character === '}' || character === ']') { if (--depth < 0) throw new Error('Invalid diagnostic response'); }
  }
  if (depth || quoted) throw new Error('Invalid diagnostic response');
  return JSON.parse(text) as unknown;
}
export async function readDiagnosticBody(response: Response, signal: AbortSignal): Promise<unknown> {
  if (signal.aborted) throw new DOMException('Diagnostic cancelled', 'AbortError');
  if (response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase() !== 'application/json' || !response.body) {
    void response.body?.cancel().catch(() => {}); throw new Error('Invalid diagnostic response');
  }
  const length = response.headers.get('Content-Length');
  if (length && /^\d+$/.test(length) && Number(length) > RESPONSE_BYTES) { void response.body.cancel().catch(() => {}); throw new Error('Invalid diagnostic response'); }
  const reader = response.body.getReader(), buffer = new Uint8Array(RESPONSE_BYTES); let size = 0;
  let rejectCancelled: () => void = () => {};
  const cancelled = new Promise<never>((_resolve, reject) => { rejectCancelled = () => reject(new DOMException('Diagnostic cancelled', 'AbortError')); });
  const abort = () => { rejectCancelled(); void reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', abort, { once: true });
  try {
    if (signal.aborted) abort();
    while (true) {
      const { done, value } = await Promise.race([reader.read(), cancelled]); if (done) break;
      if (value.byteLength > RESPONSE_BYTES - size) throw new Error('Invalid diagnostic response');
      buffer.set(value, size); size += value.byteLength;
    }
    return parseDiagnosticJson(new TextDecoder('utf-8', { fatal: true }).decode(buffer.subarray(0, size)));
  } finally { signal.removeEventListener('abort', abort); void reader.cancel().catch(() => {}); }
}

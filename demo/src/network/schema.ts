/** Only metadata in this module is eligible for diagnostic state or export. */
export const protocols = Object.freeze(['http', 'ws', 'sync', 'p2p', 'cache', 's3', 'sql', 'redis', 'worker'] as const);
export const events = Object.freeze(['http.request', 'ws.connection', 'ws.message', 'ws.retry', 'sync.snapshot', 'sync.delta',
  'peer.registration', 'peer.discovery', 'peer.signal', 'peer.connection', 'peer.channel',
  'media.read', 'media.attempt', 'media.verify', 'media.fallback'] as const);
export const statuses = Object.freeze(['started', 'completed', 'failed', 'cancelled', 'retrying'] as const);
export const operations = Object.freeze(['request', 'snapshot', 'delta', 'media', 'peer', 'diagnostics'] as const);
export const routes = Object.freeze(['cache', 'p2p_direct', 'p2p_relay', 'p2p_unknown', 'p2p_mixed', 'server_proxy', 'direct_s3', 'unknown'] as const);
export const templates = Object.freeze(['session', 'authentication', 'tickets', 'sync.snapshot', 'sync.delta', 'media.authorize',
  'media.download', 'upload', 'account', 'external.release', 'diagnostics', 'unknown'] as const);
export const errors = Object.freeze(['network', 'timeout', 'aborted', 'auth', 'forbidden', 'http', 'invalid', 'stale',
  'unavailable', 'integrity', 'capacity', 'missing_peer', 'signaling', 'connection', 'stalled', 'lease_expired', 'quota', 'unsupported', 'unknown'] as const);
export const channels = Object.freeze(['http', 'ws', 'sync', 'peer', 'media', 'server'] as const);
export type Channel = typeof channels[number];
export type Protocol = typeof protocols[number];
export type Status = typeof statuses[number];
export type Operation = typeof operations[number];
export type Primitive = string | number | boolean | null;
export const LIMITS = Object.freeze({ events: 200, bytes: 128 * 1024, eventBytes: 2 * 1024, active: 32, devices: 32 });
export const hexId = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);
export function member<T extends string>(value: unknown, allowed: readonly T[]): value is T {
  return typeof value === 'string' && allowed.includes(value as T);
}
export const number = (value: unknown, integer = false): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= Number.MAX_SAFE_INTEGER && (!integer || Number.isSafeInteger(value));
/** Read own data values without evaluating accessors; isolate reflection errors. */
export function own(input: unknown, key: string): unknown {
  if (!input || typeof input !== 'object') return undefined;
  try { return Object.getOwnPropertyDescriptor(input, key)?.value; } catch { return undefined; }
}
type Rule = 'number' | 'integer' | 'boolean' | readonly string[];
const api = { state: ['unknown', 'checking', 'reachable', 'degraded', 'offline'], auth: ['unknown', 'ok', 'required', 'forbidden'],
  lastSuccessAt: 'number', lastFailureAt: 'number', status: 'integer', durationMs: 'number', headersMs: 'number', active: 'integer', errorCode: errors } as const;
const websocket = { state: ['unknown', 'closed', 'connecting', 'open', 'ready', 'reconnecting', 'error'], connectedSince: 'number',
  lastMessageAt: 'number', messageIntervalMs: 'number', heartbeatConfiguredMs: 'number', initialTimeoutMs: 'number', watchdogMs: 'number',
  retryDelayMs: 'number', retryAt: 'number', closeCode: 'integer', messageType: ['sync.ready', 'sync.hint', 'peer'], diagnosticsVersion: 'integer', errorCode: errors } as const;
const sync = { state: ['unknown', 'idle', 'snapshot', 'delta', 'catching_up', 'current', 'failed'], localCursor: 'integer', serverCursor: 'integer',
  lastSnapshotAt: 'number', lastDeltaAt: 'number', lastDurationMs: 'number', lastCount: 'integer', pending: 'boolean', retryDelayMs: 'number', retryAt: 'number', errorCode: errors } as const;
const peer = { state: ['unknown', 'disabled', 'unavailable', 'idle', 'registering', 'registered', 'discovering', 'signaling', 'connecting', 'open', 'transferring', 'completed', 'failed'],
  configured: 'boolean', registered: 'boolean', peers: 'integer', receiving: 'integer', sending: 'integer', stunConfigured: 'boolean', turnConfigured: 'boolean',
  heartbeatConfiguredMs: 'number', leaseMs: 'number', iceState: ['new', 'checking', 'connected', 'completed', 'failed', 'disconnected', 'closed'],
  dtlsState: ['new', 'connecting', 'connected', 'closed', 'failed'], channelState: ['connecting', 'open', 'closing', 'closed'],
  localType: ['host', 'srflx', 'prflx', 'relay'], remoteType: ['host', 'srflx', 'prflx', 'relay'], route: routes,
  roundTripMs: 'number', channelBytesSent: 'integer', channelBytesReceived: 'integer', errorCode: errors } as const;
const transfer = { state: statuses, route: routes, direction: ['send', 'receive'], bytes: 'integer', controlBytes: 'integer', discardedBytes: 'integer',
  durationMs: 'number', speedBytesPerSecond: 'number', verified: 'boolean', errorCode: errors } as const;
const specifications = { api, websocket, sync, peer, transfer } satisfies Record<string, Record<string, Rule>>;
export type Card = keyof typeof specifications;
export type CardState = Readonly<Record<string, Primitive>>;
export type Cards = Readonly<Record<Card, CardState>>;
const cardChannels: Record<Card, readonly Channel[]> = { api: ['http'], websocket: ['ws'], sync: ['sync'], peer: ['peer'], transfer: ['media', 'peer'] };
export function allowedCard(channel: Channel, card: Card) {
  return Object.hasOwn(cardChannels, card) && cardChannels[card].includes(channel);
}
export function sanitizeCard(card: Card, input: unknown): Record<string, Primitive> {
  const spec: Record<string, Rule> = specifications[card];
  const result: Record<string, Primitive> = {};
  if (!spec) return result;
  for (const [key, rule] of Object.entries(spec)) {
    const value = own(input, key);
    if (value === undefined) continue;
    result[key] = rule === 'number' ? number(value) ? value : null
      : rule === 'integer' ? number(value, true) ? value : null
      : rule === 'boolean' ? typeof value === 'boolean' ? value : null
      : member(value, rule) ? value : null;
  }
  return result;
}
export function emptyCards(): Record<Card, Record<string, Primitive>> {
  return { api: { state: 'unknown' }, websocket: { state: 'closed' }, sync: { state: 'unknown' }, peer: { state: 'unknown' }, transfer: {} };
}
export interface Metadata {
  protocol: Protocol; event: typeof events[number]; status: Status; template?: typeof templates[number];
  bytes?: number; controlBytes?: number; discardedBytes?: number; durationMs?: number; attempt?: number; httpStatus?: number;
  route?: typeof routes[number]; verified?: boolean; errorCode?: typeof errors[number];
}
export function sanitizeMetadata(input: unknown): Metadata | null {
  const protocol = own(input, 'protocol'), event = own(input, 'event'), status = own(input, 'status');
  if (!member(protocol, protocols) || !member(event, events) || !member(status, statuses)) return null;
  const result: Metadata = { protocol, event, status };
  const template = own(input, 'template'), route = own(input, 'route'), errorCode = own(input, 'errorCode'), verified = own(input, 'verified');
  if (member(template, templates)) result.template = template;
  if (member(route, routes)) result.route = route;
  if (member(errorCode, errors)) result.errorCode = errorCode;
  if (typeof verified === 'boolean') result.verified = verified;
  for (const key of ['bytes', 'controlBytes', 'discardedBytes', 'attempt', 'httpStatus'] as const) {
    const value = own(input, key); if (number(value, true)) result[key] = value;
  }
  const duration = own(input, 'durationMs'); if (number(duration)) result.durationMs = duration;
  return result;
}
export interface DiagnosticEvent extends Metadata {
  sequence: number; timestamp: number | null; offsetMs: number | null; origin: 'client'; operationId?: string; deviceId?: string; traceId?: string;
}
export function sanitizeEvent(input: unknown): DiagnosticEvent | null {
  const data = sanitizeMetadata(input), sequence = own(input, 'sequence');
  if (!data || !number(sequence, true) || own(input, 'origin') !== 'client') return null;
  const timestamp = own(input, 'timestamp'), offset = own(input, 'offsetMs');
  const result: DiagnosticEvent = { ...data, sequence, origin: 'client', timestamp: number(timestamp) ? timestamp : null, offsetMs: number(offset) ? offset : null };
  const operation = own(input, 'operationId'), device = own(input, 'deviceId'), trace = own(input, 'traceId');
  if (typeof operation === 'string' && /^op-[1-9][0-9]{0,14}$/.test(operation)) result.operationId = operation;
  if (typeof device === 'string' && /^device-[1-9][0-9]{0,14}$/.test(device)) result.deviceId = device;
  if (hexId(trace)) result.traceId = trace;
  return result;
}

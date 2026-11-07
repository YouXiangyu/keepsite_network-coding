import { type DeviceHandle, type MonitorScope, type OperationHandle } from './monitor';
import { networkMonitor, observe } from './runtime';
import { number, type Metadata } from './schema';

export type MediaRoute = NonNullable<Metadata['route']>;
export type MediaError = NonNullable<Metadata['errorCode']>;
type Outcome = 'completed' | 'failed' | 'cancelled';
const mono = () => observe(() => { const value = performance.now(); return number(value) ? value : null; }) ?? null;
const elapsed = (started: number | null) => { const current = mono(); return current !== null && started !== null && current >= started ? current - started : null; };
export interface MediaObservation {
  scope: MonitorScope; operation?: OperationHandle; started: number | null; ended: boolean; expected: number;
  direction: 'receive' | 'send'; attempts: number; bytes: number; observed: boolean; verifiedBytes: number | null; control: number; discarded: number;
  route: MediaRoute; current?: MediaAttempt; detach?: () => void;
}
export interface MediaAttempt {
  parent: MediaObservation; operation?: OperationHandle; scope: MonitorScope; device?: DeviceHandle;
  route: MediaRoute; protocol: 'cache' | 'p2p' | 's3' | 'http'; index: number; started: number | null; published: number | null;
  bytes: number; observed: boolean; control: number; discarded: number; ended: boolean;
}
export function mediaScope() { return observe(() => networkMonitor.replaceConnection('media')); }
export function retireMedia(scope: MonitorScope | undefined) { return observe(() => networkMonitor.valid(scope) ? networkMonitor.replaceConnection('media') : undefined); }
export function startMedia(scope: MonitorScope | undefined, expected: number, signal: AbortSignal, direction: MediaObservation['direction'] = 'receive'): MediaObservation | undefined {
  return observe(() => {
    if (!networkMonitor.valid(scope)) return;
    const operation = networkMonitor.beginOperation(scope, 'media');
    const observation: MediaObservation = { scope, operation, expected, direction, started: mono(), ended: false, attempts: 0,
      bytes: 0, observed: false, verifiedBytes: null, control: 0, discarded: 0, route: 'unknown' };
    const abort = () => finishMedia(observation, 'cancelled', 'aborted');
    observation.detach = () => signal.removeEventListener('abort', abort); observe(() => signal.addEventListener('abort', abort, { once: true }));
    if (operation) observe(() => networkMonitor.record(scope, { protocol: 'media', event: 'media.read', status: 'started', expectedBytes: expected }, operation));
    observe(() => networkMonitor.update(scope, 'transfer', { state: 'started', direction, expectedBytes: expected, bytes: null,
      route: 'unknown', verifiedBytes: null, controlBytes: 0, discardedBytes: 0, durationMs: null, speedBytesPerSecond: null, verified: false, errorCode: null }));
    if (signal.aborted) abort(); return observation;
  });
}
export function startAttempt(parent: MediaObservation | undefined, protocol: MediaAttempt['protocol'], route: MediaRoute, scope = parent?.scope): MediaAttempt | undefined {
  return observe(() => {
    if (!parent || parent.ended || !networkMonitor.valid(parent.scope) || !networkMonitor.valid(scope)) return;
    const operation = parent.operation ? networkMonitor.beginOperation(scope, 'media', parent.operation) : undefined;
    const attempt: MediaAttempt = { parent, scope, operation, protocol, route, index: ++parent.attempts, started: mono(), published: null, bytes: 0, observed: false, control: 0, discarded: 0, ended: false };
    parent.current = attempt;
    if (operation) observe(() => networkMonitor.record(scope, { protocol, route, event: 'media.attempt', status: 'started', attempt: attempt.index, expectedBytes: parent.expected }, operation));
    publishAttempt(attempt); return attempt;
  });
}
export function attemptBytes(attempt: MediaAttempt | undefined, bytes: number, control = 0, discarded = 0) {
  observe(() => {
    if (!attempt || attempt.ended || attempt.parent.ended || !networkMonitor.valid(attempt.scope) || !networkMonitor.valid(attempt.parent.scope)) return;
    if (!number(bytes, true) || !number(control, true) || !number(discarded, true)) return;
    const parent = attempt.parent;
    if (![attempt.bytes + bytes, parent.bytes + bytes, attempt.control + control, parent.control + control, attempt.discarded + discarded, parent.discarded + discarded].every(value => number(value, true))) return;
    attempt.bytes += bytes; parent.bytes += bytes; attempt.observed = parent.observed = true;
    attempt.control += control; parent.control += control; attempt.discarded += discarded; parent.discarded += discarded;
  });
}
function transferValues(attempt: MediaAttempt, state: Outcome | 'started', verified: boolean, errorCode?: MediaError) {
  const duration = elapsed(attempt.started);
  return { state, direction: attempt.parent.direction, route: attempt.route, expectedBytes: attempt.parent.expected,
    bytes: attempt.observed ? attempt.bytes : null, verifiedBytes: null, controlBytes: attempt.control, discardedBytes: attempt.discarded, durationMs: duration,
    speedBytesPerSecond: attempt.observed && duration !== null && duration > 0 ? attempt.bytes * 1000 / duration : null, verified, errorCode: errorCode ?? null };
}
export function publishAttempt(attempt: MediaAttempt | undefined) {
  observe(() => {
    if (!attempt || attempt.ended || attempt.parent.ended || !networkMonitor.valid(attempt.scope) || !networkMonitor.valid(attempt.parent.scope)) return;
    const current = mono();
    if (current !== null && attempt.published !== null && current - attempt.published < 250) return;
    attempt.published = current;
    networkMonitor.update(attempt.parent.scope, 'transfer', transferValues(attempt, 'started', false));
  });
}
export function finishAttempt(attempt: MediaAttempt | undefined, status: Outcome, errorCode?: MediaError, verified = false) {
  observe(() => {
    if (!attempt || attempt.ended) return; attempt.ended = true;
    const parent = attempt.parent;
    try {
      if (!parent.ended && networkMonitor.valid(attempt.scope) && networkMonitor.valid(parent.scope)) {
        if (status === 'completed' && verified) parent.route = attempt.route;
        networkMonitor.update(parent.scope, 'transfer', transferValues(attempt, status, verified, errorCode));
      }
    } finally {
      if (attempt.operation) observe(() => networkMonitor.finishOperation(attempt.scope, attempt.operation!, { protocol: attempt.protocol, event: 'media.attempt', status,
        route: attempt.route, attempt: attempt.index, ...(attempt.observed ? { bytes: attempt.bytes } : {}), controlBytes: attempt.control,
        discardedBytes: attempt.discarded, verified, errorCode }, attempt.device));
      if (parent.current === attempt) parent.current = undefined;
    }
  });
}
export function mediaVerification(parent: MediaObservation | undefined, status: 'completed' | 'failed') {
  observe(() => { if (parent && !parent.ended && parent.operation) networkMonitor.record(parent.scope,
    { protocol: parent.current?.protocol ?? (parent.route.startsWith('p2p') ? 'p2p' : 'media'), event: 'media.verify', status, verified: status === 'completed', ...(status === 'failed' ? { errorCode: 'integrity' } : {}) }, parent.operation); });
}
export function mediaFallback(parent: MediaObservation | undefined, errorCode: MediaError, protocol = parent?.current?.protocol ?? 'media') {
  observe(() => { if (parent && !parent.ended && parent.operation) networkMonitor.record(parent.scope,
    { protocol, event: 'media.fallback', status: 'retrying', errorCode }, parent.operation); });
}
export function discardMedia(parent: MediaObservation | undefined, bytes: number) {
  observe(() => { if (parent && !parent.ended && networkMonitor.valid(parent.scope) && number(bytes, true) && number(parent.discarded + bytes, true)) parent.discarded += bytes; });
}
export function finishMedia(parent: MediaObservation | undefined, status: Outcome, errorCode?: MediaError, fallbackRoute?: MediaRoute, verifiedBytes?: number) {
  observe(() => {
    if (!parent || parent.ended) return; parent.ended = true;
    if (status === 'completed' && parent.route === 'unknown' && fallbackRoute) parent.route = fallbackRoute;
    // A declared expected size is never substituted for the final validated Blob size.
    if (status === 'completed' && number(verifiedBytes, true)) parent.verifiedBytes = verifiedBytes;
    observe(() => parent.detach?.()); parent.detach = undefined;
    finishAttempt(parent.current, status === 'completed' ? 'failed' : status, errorCode);
    try {
      const duration = elapsed(parent.started);
      networkMonitor.update(parent.scope, 'transfer', { state: status, direction: parent.direction, route: parent.route, expectedBytes: parent.expected,
        bytes: parent.observed ? parent.bytes : null, verifiedBytes: parent.verifiedBytes, controlBytes: parent.control, discardedBytes: parent.discarded, durationMs: duration,
        speedBytesPerSecond: parent.observed && duration !== null && duration > 0 ? parent.bytes * 1000 / duration : null, verified: status === 'completed', errorCode: errorCode ?? null });
    } finally {
      if (parent.operation) observe(() => networkMonitor.finishOperation(parent.scope, parent.operation!,
        { protocol: parent.route === 'direct_s3' ? 's3' : parent.route.startsWith('p2p') ? 'p2p' : parent.route === 'server_proxy' ? 'http' : parent.route === 'cache' ? 'cache' : 'media', event: 'media.read', status,
          route: parent.route, ...(parent.observed ? { bytes: parent.bytes } : {}), ...(parent.verifiedBytes === null ? {} : { verifiedBytes: parent.verifiedBytes }), expectedBytes: parent.expected,
          controlBytes: parent.control, discardedBytes: parent.discarded, verified: status === 'completed', errorCode }));
    }
  });
}

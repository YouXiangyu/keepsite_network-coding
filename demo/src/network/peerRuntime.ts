import type { DeviceHandle, MonitorScope, OperationHandle } from './monitor';
import { networkMonitor, observe } from './runtime';
import { member, number, own, sanitizeCard, type Metadata } from './schema';
import { attemptBytes, type MediaAttempt } from './mediaRuntime';
import type { ActivePeerTransfer } from '../media/peerProtocol';

type PeerEvent = 'peer.registration' | 'peer.discovery' | 'peer.signal' | 'peer.connection' | 'peer.channel';
export interface PeerObservation {
  scope: MonitorScope; operation?: OperationHandle; event: PeerEvent; ended: boolean; control: number; observed: boolean; attempt?: MediaAttempt;
}
export function peerScope(previous?: MonitorScope, fresh = false) {
  return observe(() => fresh || networkMonitor.valid(previous) ? networkMonitor.replaceConnection('peer') : undefined);
}
export function peerState(scope: MonitorScope | undefined, values: unknown) {
  observe(() => {
    if (!networkMonitor.valid(scope)) return;
    const safe = sanitizeCard('peer', values), current = networkMonitor.snapshot().cards.peer;
    if (Object.keys(safe).some(key => !Object.is(safe[key], current[key]))) networkMonitor.update(scope, 'peer', safe);
  });
}
export function startPeer(scope: MonitorScope | undefined, event: PeerEvent, parent?: OperationHandle, attempt?: MediaAttempt): PeerObservation | undefined {
  return observe(() => {
    if (!networkMonitor.valid(scope)) return;
    const operation = attempt && !attempt.operation ? undefined : networkMonitor.beginOperation(scope, 'peer', parent ?? attempt?.operation);
    const observation: PeerObservation = { scope, operation, event, ended: false, control: 0, observed: false, attempt };
    if (operation) observe(() => networkMonitor.record(scope, { protocol: 'p2p', event, status: 'started' }, operation, attempt?.device));
    return observation;
  });
}
export function peerBytes(observation: PeerObservation | undefined, bytes: number) {
  observe(() => {
    if (!observation || observation.ended || !networkMonitor.valid(observation.scope) || !number(bytes, true) || !number(observation.control + bytes, true)) return;
    observation.control += bytes; observation.observed = true; attemptBytes(observation.attempt, 0, bytes);
  });
}
export function finishPeer(observation: PeerObservation | undefined, status: 'completed' | 'failed' | 'cancelled', errorCode?: Metadata['errorCode']) {
  observe(() => {
    if (!observation || observation.ended) return; observation.ended = true;
    if (observation.operation) networkMonitor.finishOperation(observation.scope, observation.operation,
      { protocol: 'p2p', event: observation.event, status, ...(observation.observed ? { controlBytes: observation.control } : {}), errorCode }, observation.attempt?.device);
  });
}
export function peerDevice(scope: MonitorScope | undefined): DeviceHandle | undefined { return observe(() => networkMonitor.device(scope)); }
export function releasePeerDevice(device: DeviceHandle | undefined) { if (device) observe(() => networkMonitor.releaseDevice(device)); }
/** The active UI projection can be sanitized again for export; it never contains business IDs or names. */
export function activePeerProjection(input: unknown): Readonly<ActivePeerTransfer> | null {
  const alias = own(input, 'alias'), direction = own(input, 'direction'), device = own(input, 'deviceAlias');
  if (typeof alias !== 'string' || !/^channel-[1-9][0-9]{0,14}$/.test(alias) || !member(direction, ['receive', 'send'])) return null;
  const pick = <T extends string>(key: string, values: readonly T[]) => { const value = own(input, key); return member(value, values) ? value : null; };
  const count = (key: string) => { const value = own(input, key); return number(value, true) ? value : null; };
  const rtt = own(input, 'roundTripMs');
  const duration = own(input, 'durationMs'), rate = own(input, 'payloadBytesPerSecond');
  return Object.freeze({ alias, direction, deviceAlias: typeof device === 'string' && /^device-[1-9][0-9]{0,14}$/.test(device) ? device : null,
    expectedBytes: count('expectedBytes'), payloadBytes: count('payloadBytes'), controlBytes: count('controlBytes'), discardedBytes: count('discardedBytes'), localVerifiedBytes: count('localVerifiedBytes'), sourceConfirmed: own(input, 'sourceConfirmed') === true,
    progressBytes: count('progressBytes'), resumeOffset: count('resumeOffset'), bufferedAmount: count('bufferedAmount'), durationMs: number(duration) ? duration : null, payloadBytesPerSecond: number(rate) ? rate : null,
    pcState: pick('pcState', ['new', 'connecting', 'connected', 'disconnected', 'failed', 'closed'] as const),
    iceState: pick('iceState', ['new', 'checking', 'connected', 'completed', 'failed', 'disconnected', 'closed'] as const),
    dtlsState: pick('dtlsState', ['new', 'connecting', 'connected', 'closed', 'failed'] as const),
    channelState: pick('channelState', ['connecting', 'open', 'closing', 'closed'] as const),
    localType: pick('localType', ['host', 'srflx', 'prflx', 'relay'] as const), remoteType: pick('remoteType', ['host', 'srflx', 'prflx', 'relay'] as const),
    route: pick('route', ['direct', 'relay', 'unknown', 'mixed'] as const) ?? 'unknown', roundTripMs: number(rtt) ? rtt : null,
    channelBytesSent: count('channelBytesSent'), channelBytesReceived: count('channelBytesReceived') });
}

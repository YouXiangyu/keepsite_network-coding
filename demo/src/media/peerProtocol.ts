import type { MediaRef } from '../data';

export type PeerAsset = Pick<MediaRef, 'assetId' | 'sha256' | 'size' | 'mimeType'>;
export interface PeerIdentity { peerId: string; name: string }
export interface PeerLimits { maxInventory: number; maxBatchAssets: number; maxControlBytes: number; maxSignalBytes: number; maxSdpBytes: number; maxIceBytes: number; maxActiveTransfers: number }
export type SignalKind = 'offer' | 'answer' | 'ice';
export type SignalPayload = RTCSessionDescriptionInit | RTCIceCandidateInit | null;
export interface PeerMessage {
  readonly wireBytes?: number;
  v: 1; type: string; serverId: string; userId: string; requestId?: string;
  peerId?: string | null; enabled?: boolean; reason?: string | null; heartbeatMs?: number; leaseMs?: number;
  inventoryRevision?: number; limits?: PeerLimits; iceServers?: RTCIceServer[]; turnEnabled?: boolean;
  revision?: number; count?: number; complete?: boolean; nextBatchIndex?: number | null;
  peers?: PeerIdentity[]; assetId?: string; transferId?: string; requesterPeerId?: string; sourcePeerId?: string; offererPeerId?: string;
  media?: PeerAsset; fromPeerId?: string; kind?: SignalKind; payload?: SignalPayload; code?: string; message?: string;
}
export interface ClientPeerMessage { v: 1; type: string; requestId: string; [key: string]: unknown }
export interface EventConnectionState { connected: boolean; serverId: string; generation: number }
export interface AccountEventPort {
  eventState(): EventConnectionState;
  subscribeConnection(listener: () => void): () => void;
  subscribeEvents(listener: (message: PeerMessage) => void): () => void;
  sendEvent(message: ClientPeerMessage): boolean;
}
export type PeerRoute = 'direct' | 'relay' | 'unknown' | 'mixed';
export interface TransferRecord {
  id: string; direction: 'receive' | 'send'; path: 'peer' | 's3' | 'server' | 'cache'; route?: PeerRoute;
  status: 'active' | 'complete' | 'failed' | 'cancelled'; bytes: number; controlBytes: number; discardedBytes: number; milliseconds: number; reason: string;
  discoveryMs?: number; authorizationMs?: number; connectionMs?: number; firstByteMs?: number; verificationMs?: number; sourceReadMs?: number;
  resumeOffset?: number; discardedPrefixBytes?: number;
}
export interface PeerView {
  wanted: boolean; connected: boolean; enabled: boolean; peerId: string | null; reason: string;
  peers: PeerIdentity[]; inventoryCount: number; receiving: number; sending: number;
  recent: TransferRecord[]; totals: { peer: number; direct: number; relay: number; unknown: number; mixed: number; s3: number; server: number; cache: number; discarded: number; control: number };
}
export interface PeerCachePort {
  listShareable(signal: AbortSignal): Promise<PeerAsset[]>;
  readForPeer(media: PeerAsset, signal: AbortSignal): Promise<Blob | null>;
  canShare(assetId: string): boolean;
  subscribeInventory(listener: () => void): () => void;
}
export interface PeerMediaPort {
  tryDownload(media: MediaRef, signal: AbortSignal): Promise<{ blob: Blob; transport: 'peer' } | null>;
  invalidate(assetIds?: readonly string[]): void;
  recordCloud(id: string, path: 's3' | 'server', event: 'start' | 'bytes' | 'complete' | 'failed', bytes?: number): void;
  recordCache(bytes: number): void;
}
export const safeId = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_-]{1,120}$/.test(value);
export const safeRequestId = (value: unknown): value is string => typeof value === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(value);
export const validPeerAsset = (value: unknown): value is PeerAsset => {
  if (!value || typeof value !== 'object') return false;
  const media = value as PeerAsset;
  return typeof media.assetId === 'string' && /^[a-f0-9]{64}$/.test(media.assetId) && media.sha256 === media.assetId
    && Number.isSafeInteger(media.size) && media.size > 0 && media.size <= 12 * 1024 * 1024 && ['image/jpeg', 'image/png', 'image/webp'].includes(media.mimeType);
};
export const sameAsset = (a: PeerAsset, b: PeerAsset) => a.assetId === b.assetId && a.sha256 === b.sha256 && a.size === b.size && a.mimeType === b.mimeType;
export const bytesOf = (value: unknown) => new TextEncoder().encode(typeof value === 'string' ? value : JSON.stringify(value)).byteLength;
const peerTypes = new Set(['device.registered', 'device.alive', 'device.peers', 'device.unregistered', 'cache.ack', 'peer.sources', 'peer.pending', 'peer.request', 'peer.authorized', 'peer.signalled', 'peer.signal', 'peer.lease', 'peer.cancelled', 'peer.error']);
export function validPeerEnvelope(message: PeerMessage) {
  return peerTypes.has(message.type) && (message.requestId === undefined || safeRequestId(message.requestId));
}

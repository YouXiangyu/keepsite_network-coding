import { validateData, validMedia, validTicket, type Ticket, type MediaRef } from './data';
import { bytesOf, validPeerEnvelope, type ClientPeerMessage, type PeerMessage } from './media/peerProtocol';

export const API_BASE = '';
export interface User { id: string; email: string; name: string }
export interface AuthSession {
  authenticated: boolean;
  user?: User;
  csrfToken?: string;
  providers: { google: boolean; email: boolean };
  configured: boolean;
}
export interface SignedInSession extends AuthSession {
  authenticated: true;
  user: User;
  csrfToken: string;
}
export interface AccountMethods {
  password: { linked: boolean; email: string | null };
  google: { linked: boolean; email: string | null };
}
function checkedAccountMethods(value: AccountMethods): AccountMethods {
  if (!value || !['password', 'google'].every(key => {
    const method = value[key as keyof AccountMethods];
    return method && typeof method.linked === 'boolean' && (method.linked ? typeof method.email === 'string' && !!method.email : method.email === null);
  })) throw new ApiError('登录方式信息不完整，请重新读取。');
  return value;
}
export async function getAccountMethods(signal?: AbortSignal) {
  return checkedAccountMethods(await jsonRequest<AccountMethods>('/api/auth/account/methods', { authenticated: true, signal }));
}
export async function addAccountPassword(action: 'register' | 'confirm' | 'link', email: string, password: string, code?: string) {
  const result = await jsonRequest<AccountMethods | { verificationRequired: boolean; email: string }>(`/api/auth/account/password/${action}`, {
    method: 'POST', authenticated: true, ...jsonBody({ email, password, ...(action === 'confirm' ? { code } : {}) }),
  });
  if (action === 'register') {
    if (!('verificationRequired' in result) || result.verificationRequired !== true || typeof result.email !== 'string' || !result.email) throw new ApiError('验证邮件请求结果不完整，请核对后重试。');
    return result;
  }
  return checkedAccountMethods(result as AccountMethods);
}
export async function startAccountGoogle() {
  const result = await jsonRequest<{ browserUrl: string; expiresAt: number }>('/api/auth/account/google/start', { method: 'POST', authenticated: true, ...jsonBody({}) });
  const url = new URL(result.browserUrl, window.location.origin);
  if (url.origin !== window.location.origin || url.pathname !== '/api/auth/account/google/launch' || url.hash || !url.searchParams.get('flow')
    || [...url.searchParams.keys()].length !== 1 || !Number.isSafeInteger(result.expiresAt) || result.expiresAt <= Date.now()) throw new ApiError('Google 绑定地址无效，请重试。');
  return url.href;
}
export interface Capabilities { processing: boolean; processingUnavailableReason: string | null; cloudStorage: boolean; deviceSync: boolean }
export interface SnapshotPage { serverId: string; userId: string; snapshotId: string; cursor: number; total: number; offset: number; tickets: Ticket[]; nextPageToken: string | null; expiresAt: number; capabilities: Capabilities }
export type Change = { cursor: number; type: 'upsert'; ticket: Ticket } | { cursor: number; type: 'delete'; id: string; version: number; deletedAt: number };
export interface ChangePage { serverId: string; userId: string; fromCursor: number; nextCursor: number; headCursor: number; hasMore: boolean; changes: Change[]; capabilities: Capabilities }
export interface SyncPosition { serverId: string; committedCursor: number | null }
export interface MutationReceipt { serverId: string; operationId: string; committedCursor: number }
export interface SyncHint { v: 1; type: 'sync.ready' | 'sync.hint'; serverId: string; userId: string; cursor: number }
export interface MediaAsset {
  assetId: string;
  url: string;
  sha256: string;
  size: number;
  width: number;
  height: number;
  mimeType: string;
}
export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status = 0, code?: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}
export const errorMessage = (error: unknown) => error instanceof Error ? error.message : '请求失败，请重试。';

let epoch = 0;
let activeSession: SignedInSession | null = null;
const pending = new Set<AbortController>();
const sessionListeners = new Set<() => void>();
export const sessionAccountId = () => activeSession?.user.id ?? null;
export function onSessionChanged(listener: () => void) { sessionListeners.add(listener); return () => { sessionListeners.delete(listener); }; }
interface SessionEnd { reason: 'expired' | 'logout'; logoutUrl?: string }
const expiredListeners = new Set<(event: SessionEnd) => void>();

export const sessionEpoch = () => epoch;
export function assertSessionEpoch(expected: number) {
  if (expected !== epoch) staleRequest();
}

export function cancelSessionRequests() {
  epoch += 1;
  for (const listener of sessionListeners) listener();
  for (const controller of pending) controller.abort();
  pending.clear();
}
export function installSession(session: AuthSession | null) {
  cancelSessionRequests();
  activeSession = null;
  activeSession = session?.authenticated ? requireSignedIn(session) : null;
}
export function onSessionExpired(listener: (event: SessionEnd) => void) {
  expiredListeners.add(listener);
  return () => { expiredListeners.delete(listener); };
}
function expireSession(event: SessionEnd = { reason: 'expired' }) {
  installSession(null);
  for (const listener of expiredListeners) listener(event);
}
function staleRequest(): never {
  throw new DOMException('请求所属会话已结束。', 'AbortError');
}
function validateSession(value: unknown): AuthSession {
  if (!value || typeof value !== 'object') throw new ApiError('登录服务返回的信息不完整。');
  const session = value as AuthSession;
  if (typeof session.authenticated !== 'boolean' || typeof session.configured !== 'boolean'
      || typeof session.providers?.email !== 'boolean' || typeof session.providers?.google !== 'boolean') {
    throw new ApiError('登录服务返回的信息不完整。');
  }
  if (session.authenticated) requireSignedIn(session);
  return session;
}
function requireSignedIn(session: AuthSession): SignedInSession {
  if (!session.authenticated || !session.user || typeof session.user.id !== 'string' || !session.user.id || typeof session.user.email !== 'string'
      || typeof session.user.name !== 'string' || typeof session.csrfToken !== 'string' || !session.csrfToken) {
    throw new ApiError('登录状态不完整，请重新登录。', 401);
  }
  return session as SignedInSession;
}

interface RequestOptions extends RequestInit { authenticated?: boolean; signedMedia?: boolean }
async function request<T>(path: string, options: RequestOptions, consume: (response: Response, signal: AbortSignal) => Promise<T>): Promise<T> {
  const { authenticated = false, signedMedia = false, ...init } = options;
  if (signedMedia ? !validS3Url(path) : !path.startsWith('/api/') || path.includes('://')) throw new ApiError('请求地址无效。');
  if (authenticated && !activeSession) throw new ApiError('请先登录。', 401);
  const started = epoch;
  const headers = new Headers(init.headers);
  if (!signedMedia && init.method && init.method !== 'GET') {
    headers.set('X-Keepsite-Client', 'web');
    if (activeSession) headers.set('X-CSRF-Token', activeSession.csrfToken);
  }
  const controller = new AbortController();
  pending.add(controller);
  let rejectAborted: () => void = () => {};
  const aborted = new Promise<never>((_resolve, reject) => {
    rejectAborted = () => reject(new DOMException('请求已取消。', 'AbortError'));
    controller.signal.addEventListener('abort', rejectAborted, { once: true });
  });
  const abort = () => controller.abort();
  init.signal?.addEventListener('abort', abort, { once: true });
  if (init.signal?.aborted) abort();
  const timeout = setTimeout(abort, path === '/api/media' ? 120000 : 30000);
  const checkActive = () => {
    if (started !== epoch || controller.signal.aborted) staleRequest();
  };
  const consumeRequest = async () => {
    checkActive();
    const response = await fetch(path, { ...init, headers, ...(signedMedia ? { credentials: 'omit' as const, mode: 'cors' as const, referrerPolicy: 'no-referrer' as const, redirect: 'error' as const } : { credentials: 'same-origin' as const }), signal: controller.signal });
    checkActive();
    if (!response.ok) {
      if (signedMedia) throw new ApiError('云端图片读取失败。', response.status);
      let detail = `请求未成功（${response.status}），请重试。`;
      let code: string | undefined;
      try {
        const body = await response.json();
        if (typeof body.detail === 'string') detail = body.detail;
        else if (body.detail && typeof body.detail.message === 'string') { detail = body.detail.message; if (typeof body.detail.code === 'string') code = body.detail.code; }
        if (typeof body.code === 'string') code = body.code;
      } catch { /* Preserve the HTTP status when an error body is unavailable. */ }
      checkActive();
      if (response.status === 401 && (authenticated || activeSession)) {
        // This request has a definitive response; cancel the other requests only.
        pending.delete(controller);
        expireSession();
      }
      throw new ApiError(detail, response.status, code);
    }
    const value = await consume(response, controller.signal);
    checkActive();
    return value;
  };
  try {
    // The deadline includes body consumption, even if a consumer ignores abort.
    return await Promise.race([consumeRequest(), aborted]);
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (started !== epoch || init.signal?.aborted) staleRequest();
    throw new ApiError(controller.signal.aborted ? '请求超时，结果尚未确认，当前草稿仍保留，请重试。' : '暂时无法连接服务，请稍后重试。');
  } finally {
    clearTimeout(timeout);
    pending.delete(controller);
    controller.signal.removeEventListener('abort', rejectAborted);
    init.signal?.removeEventListener('abort', abort);
    controller.abort();
  }
}
function jsonRequest<T>(path: string, options: RequestOptions = {}) {
  return request<T>(path, options, response => response.json());
}
function jsonBody(body: unknown): RequestInit {
  return { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}
export async function getSession(signal?: AbortSignal) {
  const scope = epoch;
  const result = await jsonRequest('/api/auth/session', { signal });
  assertSessionEpoch(scope);
  return validateSession(result);
}
export async function signIn(email: string, password: string) {
  const scope = epoch;
  const result = await jsonRequest('/api/auth/login', {
    method: 'POST', ...jsonBody({ email, password }),
  });
  assertSessionEpoch(scope);
  return requireSignedIn(validateSession(result));
}
function checkedLogoutUrl(value: unknown, returnOrigin: string): string | undefined {
  if (typeof value !== 'string' || /[\u0000-\u0020\\]/.test(value)) return;
  try {
    const url = new URL(value);
    const params = [...url.searchParams];
    if (url.protocol !== 'https:' || url.username || url.password || url.port || url.hash || url.pathname !== '/logout'
        || !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.auth\.[a-z]{2}(?:-[a-z]+)+-\d\.amazoncognito\.com$/.test(url.hostname)
        || params.length !== 2 || params.some(([key]) => key !== 'client_id' && key !== 'logout_uri')
        || !/^[A-Za-z0-9]{1,128}$/.test(url.searchParams.get('client_id') ?? '')
        || url.searchParams.get('logout_uri') !== new URL('/', returnOrigin).href) return;
    return url.href;
  } catch { return; }
}
export async function signOut(returnOrigin: string) {
  const scope = epoch;
  const result = await jsonRequest<{ logoutUrl?: unknown }>('/api/auth/logout', { method: 'POST', authenticated: true });
  assertSessionEpoch(scope);
  expireSession({ reason: 'logout', logoutUrl: checkedLogoutUrl(result?.logoutUrl, returnOrigin) });
}
export function registerAccount(email: string, password: string) {
  return jsonRequest<{ verificationRequired: boolean; email: string }>('/api/auth/register', {
    method: 'POST', ...jsonBody({ email, password }),
  });
}
export function verifyEmail(email: string, code: string) {
  return jsonRequest<{ verified: boolean }>('/api/auth/verify-email', { method: 'POST', ...jsonBody({ email, code }) });
}
export function resendCode(email: string) {
  return jsonRequest('/api/auth/resend-code', { method: 'POST', ...jsonBody({ email }) });
}
export function forgotPassword(email: string) {
  return jsonRequest('/api/auth/forgot-password', { method: 'POST', ...jsonBody({ email }) });
}
export function resetPassword(email: string, code: string, password: string) {
  return jsonRequest('/api/auth/reset-password', { method: 'POST', ...jsonBody({ email, code, password }) });
}

const integer = (value: unknown, minimum = 0): value is number => Number.isSafeInteger(value) && Number(value) >= minimum;
function checkedIdentity(value: { userId: string; serverId: string }, expectedUser: string) {
  if (value.userId !== expectedUser || activeSession?.user.id !== expectedUser) { expireSession(); throw new ApiError('账户状态已改变，请重新登录。', 401); }
  if (typeof value.serverId !== 'string' || !value.serverId) throw new ApiError('同步服务身份不完整。');
}
function checkedCapabilities(value: Capabilities) {
  if (!value || typeof value.processing !== 'boolean' || typeof value.deviceSync !== 'boolean' || typeof value.cloudStorage !== 'boolean'
      || !(value.processingUnavailableReason === null || typeof value.processingUnavailableReason === 'string')) throw new ApiError('功能状态不完整，未更新界面。');
}
async function syncJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request(path, { authenticated: true, signal }, async (response, requestSignal) => {
    if (!response.body) throw new ApiError('同步响应正文缺失。');
    const reader = response.body.getReader(); const blocks: Uint8Array[] = []; let size = 0;
    try {
      while (true) {
        if (requestSignal.aborted) staleRequest();
        const { done, value } = await reader.read(); if (done) break;
        size += value.byteLength; if (size > 1024 * 1024) throw new ApiError('同步页面超过大小限制。');
        blocks.push(value);
      }
      const bytes = new Uint8Array(size); let offset = 0; for (const block of blocks) { bytes.set(block, offset); offset += block.byteLength; }
      return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)) as T;
    } finally { void reader.cancel().catch(() => {}); }
  });
}
export async function getSnapshotPage(pageToken: string | null = null, signal?: AbortSignal): Promise<SnapshotPage> {
  const scope = epoch, user = activeSession?.user.id;
  if (!user) throw new ApiError('请先登录。', 401);
  const query = new URLSearchParams({ limit: '100', ...(pageToken ? { pageToken } : {}) });
  const page = await syncJson<SnapshotPage>('/api/sync/snapshot?' + query, signal);
  assertSessionEpoch(scope);
  if (!page || typeof page !== 'object') throw new ApiError('快照格式不完整。');
  checkedIdentity(page, user); checkedCapabilities(page.capabilities);
  if (typeof page.snapshotId !== 'string' || !page.snapshotId || !integer(page.cursor) || !integer(page.total) || page.total > 10000
    || !integer(page.offset) || !integer(page.expiresAt) || !(page.nextPageToken === null || typeof page.nextPageToken === 'string' && !!page.nextPageToken)
    || !Array.isArray(page.tickets) || page.tickets.length > 200 || page.offset + page.tickets.length > page.total
    || page.tickets.some(ticket => !validTicket(ticket) || ticket.deletedAt !== null)) throw new ApiError('快照分页格式异常。');
  validateData({ tickets: page.tickets });
  return page;
}
export async function getChanges(serverId: string, after: number, signal?: AbortSignal): Promise<ChangePage> {
  const scope = epoch, user = activeSession?.user.id;
  if (!user) throw new ApiError('请先登录。', 401);
  const page = await syncJson<ChangePage>('/api/sync/changes?' + new URLSearchParams({ serverId, after: String(after), limit: '100' }), signal);
  assertSessionEpoch(scope);
  if (!page || typeof page !== 'object') throw new ApiError('增量格式不完整。');
  checkedIdentity(page, user); checkedCapabilities(page.capabilities);
  if (page.serverId !== serverId) throw new ApiError('同步服务已变更，需要重新读取快照。', 410, 'server_changed');
  if (page.fromCursor !== after || !integer(page.nextCursor) || !integer(page.headCursor) || page.headCursor < page.nextCursor
    || typeof page.hasMore !== 'boolean' || page.hasMore !== (page.nextCursor < page.headCursor) || !Array.isArray(page.changes)
    || page.changes.length > 200 || page.nextCursor !== after + page.changes.length) throw new ApiError('增量游标不连续，需要重新校准。', 410, 'cursor_gap');
  for (const [index, change] of page.changes.entries()) {
    if (!change || change.cursor !== after + index + 1 || (change.type === 'upsert' ? !validTicket(change.ticket) || change.ticket.deletedAt !== null
      : change.type !== 'delete' || typeof change.id !== 'string' || !/^[A-Za-z0-9_-]{1,120}$/.test(change.id) || !integer(change.version, 1) || !integer(change.deletedAt))) throw new ApiError('增量记录不完整，需要重新校准。', 410, 'cursor_gap');
  }
  return page;
}
export async function mutate(path: string, method = 'POST', body?: unknown): Promise<MutationReceipt> {
  const scope = epoch;
  if (!activeSession) throw new ApiError('请先登录。', 401);
  const expected = (body as { operationId?: unknown } | undefined)?.operationId;
  if (typeof expected !== 'string' || !expected) throw new ApiError('操作标识无效。');
  const result = await jsonRequest<MutationReceipt>(path, { method, authenticated: true, ...jsonBody(body) });
  assertSessionEpoch(scope);
  if (!result || typeof result.serverId !== 'string' || !result.serverId || result.operationId !== expected || !integer(result.committedCursor)) throw new ApiError('写入回执不完整，结果尚未确认。');
  return result;
}
export interface EventConnection { stop(): void; send(message: ClientPeerMessage): boolean }
export function openSyncEvents(onMessage: (message: SyncHint | PeerMessage) => void, onClose: () => void): EventConnection {
  const scope = epoch, user = activeSession?.user.id;
  if (!user) throw new ApiError('请先登录。', 401);
  const url = new URL('/api/sync/events', window.location.origin); url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(url);
  let closed = false;
  const current = () => !closed && scope === epoch && activeSession?.user.id === user;
  const message = (event: MessageEvent) => {
    if (!current()) return;
    try {
      if (typeof event.data !== 'string' || bytesOf(event.data) > 65536) throw new Error();
      const value = JSON.parse(event.data) as SyncHint | PeerMessage;
      if (value.v !== 1 || value.userId !== user || typeof value.serverId !== 'string' || !value.serverId) throw new Error();
      if (value.type === 'sync.ready' || value.type === 'sync.hint') {
        if (bytesOf(event.data) > 4096 || !integer((value as SyncHint).cursor)) throw new Error();
      } else if (!validPeerEnvelope(value as PeerMessage) || bytesOf(event.data) > (value.type === 'peer.signal' && (value as PeerMessage).kind !== 'ice' ? 65536 : 4096)) throw new Error();
      if (value.type !== 'sync.ready' && value.type !== 'sync.hint') Object.defineProperty(value, 'wireBytes', { value: bytesOf(event.data), enumerable: false });
      onMessage(value);
    } catch { socket.close(); }
  };
  const end = () => { if (current()) onClose(); };
  socket.addEventListener('message', message); socket.addEventListener('close', end);
  const stop = () => { if (closed) return; closed = true; socket.removeEventListener('message', message); socket.removeEventListener('close', end); socket.close(); unsubscribe(); };
  const unsubscribe = onSessionChanged(stop);
  return { stop, send: message => {
    if (!current() || socket.readyState !== WebSocket.OPEN) return false;
    const body = JSON.stringify(message);
    if (bytesOf(body) > (message.type === 'peer.signal' && message.kind !== 'ice' ? 65536 : 4096) || socket.bufferedAmount + bytesOf(body) > 256 * 1024) return false;
    try { socket.send(body); return true; } catch { return false; }
  } };
}
export async function uploadMedia(file: File, uploadId: string): Promise<MediaAsset & { uploadId: string }> {
  const scope = epoch;
  const body = new FormData();
  if (!/^[A-Za-z0-9_-]{1,120}$/.test(uploadId)) throw new ApiError('上传标识无效。');
  body.append('file', file);
  body.append('uploadId', uploadId);
  const asset = await jsonRequest<MediaAsset & { uploadId: string }>('/api/media', { method: 'POST', body, authenticated: true });
  assertSessionEpoch(scope);
  if (asset?.uploadId !== uploadId || !validMedia({ ...asset, cloudStatus: 'not_configured' })) {
    throw new ApiError('图片上传结果不完整，请重试。');
  }
  return asset;
}

export async function getOperationStatus(operationId: string): Promise<SyncPosition & { operationId: string; applied: boolean }> {
  if (!/^[A-Za-z0-9_-]{1,120}$/.test(operationId)) throw new ApiError('操作标识无效。');
  const scope = epoch;
  const result = await jsonRequest<SyncPosition & { operationId: string; applied: boolean }>(`/api/operations/${encodeURIComponent(operationId)}`, { authenticated: true });
  assertSessionEpoch(scope);
  if (!result || typeof result.serverId !== 'string' || !result.serverId || result.operationId !== operationId || typeof result.applied !== 'boolean' || (result.applied ? !integer(result.committedCursor) : result.committedCursor !== null)) throw new ApiError('操作核对结果不完整，草稿仍保留。');
  return result;
}

export async function getUploadStatus(uploadId: string) {
  if (!/^[A-Za-z0-9_-]{1,120}$/.test(uploadId)) throw new ApiError('上传标识无效。');
  const scope = epoch;
  const result = await jsonRequest<{ uploadId: string; status: 'pending' | 'stored' | 'attached'; asset: MediaAsset | null }>(`/api/uploads/${encodeURIComponent(uploadId)}`, { authenticated: true });
  assertSessionEpoch(scope);
  if (!result || result.uploadId !== uploadId || !['pending', 'stored', 'attached'].includes(result.status)
    || (result.status === 'pending' ? result.asset !== null : !validMedia({ ...result.asset, cloudStatus: 'not_configured' }))) throw new ApiError('上传核对结果不完整，草稿仍保留。');
  return result;
}

export function validS3Url(value: string) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password && !url.port && !url.hash
      && /^(?:[a-z0-9][a-z0-9.-]*\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com(?:\.cn)?$/.test(url.hostname);
  } catch { return false; }
}
interface DownloadAuthorization { url: string; expiresAt: number; transport: 's3' | 'server'; sha256: string; size: number; mimeType: string }
export async function authorizeMedia(media: MediaRef, signal: AbortSignal): Promise<DownloadAuthorization> {
  const scope = epoch;
  const result = await jsonRequest<DownloadAuthorization>(`/api/media/${media.assetId}/download`, { authenticated: true, signal });
  assertSessionEpoch(scope);
  if (!result || !Number.isSafeInteger(result.expiresAt) || result.sha256 !== media.sha256 || result.size !== media.size || result.mimeType !== media.mimeType
    || (result.transport === 'server' ? result.url !== `/api/media/${media.sha256}` : result.transport !== 's3' || !validS3Url(result.url))) {
    throw new ApiError('图片下载授权不完整。');
  }
  return result;
}
async function boundedImage(response: Response, signal: AbortSignal, media: MediaRef, onBytes?: (bytes: number) => void) {
  const mimeType = response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase();
  if (mimeType !== media.mimeType || !response.body || media.size > 12 * 1024 * 1024) throw new ApiError('图片格式或大小与记录不一致。');
  const reader = response.body.getReader();
  const chunks: Uint8Array<ArrayBuffer>[] = [];
  let total = 0;
  const abort = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', abort, { once: true });
  try {
    while (true) {
      if (signal.aborted) staleRequest();
      const { done, value } = await reader.read();
      if (done) break;
      onBytes?.(value.byteLength);
      total += value.byteLength;
      if (total > media.size) throw new ApiError('图片大小与记录不一致。');
      chunks.push(new Uint8Array(value));
    }
    if (total !== media.size) throw new ApiError('图片下载不完整。');
    return new Blob(chunks, { type: mimeType });
  } finally { signal.removeEventListener('abort', abort); void reader.cancel().catch(() => {}); }
}
export interface MediaDownloadObservation { id: string; transport: 's3' | 'server'; event: 'start' | 'bytes' | 'failed'; bytes?: number }
export async function downloadMedia(media: MediaRef, signal: AbortSignal, observe?: (event: MediaDownloadObservation) => void): Promise<{ blob: Blob; transport: 's3' | 'server'; downloadId: string }> {
  const scope = epoch;
  const read = async (url: string, transport: 's3' | 'server') => {
    const id = crypto.randomUUID(); observe?.({ id, transport, event: 'start' });
    try {
      const blob = await request(url, { authenticated: true, signedMedia: transport === 's3', signal }, (response, requestSignal) => boundedImage(response, requestSignal, media, bytes => observe?.({ id, transport, event: 'bytes', bytes })));
      assertSessionEpoch(scope); return { blob, transport, downloadId: id };
    } catch (error) { observe?.({ id, transport, event: 'failed' }); throw error; }
  };
  let authorization = await authorizeMedia(media, signal);
  for (let attempt = 0; attempt < 2; attempt++) {
    assertSessionEpoch(scope);
    if (authorization.expiresAt <= Date.now()) {
      if (attempt === 0) { authorization = await authorizeMedia(media, signal); continue; }
      break;
    }
    try {
      return await read(authorization.url, authorization.transport);
    } catch (error) {
      assertSessionEpoch(scope);
      if (signal.aborted || error instanceof DOMException && error.name === 'AbortError') throw error;
      if (authorization.transport === 'server') throw error;
      if (attempt === 0 && error instanceof ApiError && error.status === 403) { authorization = await authorizeMedia(media, signal); continue; }
      break;
    }
  }
  assertSessionEpoch(scope);
  return read(`/api/media/${media.assetId}`, 'server');
}

import { validateData, validMedia, type AppData, type MediaRef } from './data';

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
export interface ServerState {
  initialized: boolean;
  serverId: string;
  userId: string;
  revision: number;
  data: AppData | null;
  capabilities: { processing: boolean; processingUnavailableReason: string | null; cloudStorage: boolean; deviceSync: false };
}
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
    headers.set('X-MemoVerse-Client', 'web');
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

function checkedState(value: unknown, expectedUser: string): ServerState {
  if (!value || typeof value !== 'object') throw new ApiError('票根服务返回的数据格式不正确。');
  const state = value as ServerState;
  if (state.userId !== expectedUser || activeSession?.user.id !== expectedUser) {
    expireSession();
    throw new ApiError('账户状态已改变，请重新登录。', 401);
  }
  if (typeof state.initialized !== 'boolean' || typeof state.serverId !== 'string' || !state.serverId
      || !Number.isSafeInteger(state.revision) || state.revision < 0) {
    throw new ApiError('票根状态不完整，未更新界面。');
  }
  if (!state.capabilities || typeof state.capabilities.processing !== 'boolean' || state.capabilities.deviceSync !== false
      || typeof state.capabilities.cloudStorage !== 'boolean'
      || !(state.capabilities.processingUnavailableReason === null || typeof state.capabilities.processingUnavailableReason === 'string')) throw new ApiError('功能状态不完整，未更新界面。');
  if (state.initialized) validateData(state.data);
  else if (state.data !== null) throw new ApiError('票根空间尚未准备完成。');
  return state;
}
export async function getState(signal?: AbortSignal): Promise<ServerState> {
  const scope = epoch;
  const user = activeSession?.user.id;
  if (!user) throw new ApiError('请先登录。', 401);
  const result = await jsonRequest('/api/state', { signal, authenticated: true });
  assertSessionEpoch(scope);
  return checkedState(result, user);
}
export async function mutate(path: string, method = 'POST', body?: unknown): Promise<ServerState> {
  const scope = epoch;
  const user = activeSession?.user.id;
  if (!user) throw new ApiError('请先登录。', 401);
  const result = await jsonRequest(path, {
    method, authenticated: true, ...(body === undefined ? {} : jsonBody(body)),
  });
  assertSessionEpoch(scope);
  return checkedState(result, user);
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

export async function getOperationStatus(operationId: string): Promise<{ operationId: string; applied: boolean }> {
  if (!/^[A-Za-z0-9_-]{1,120}$/.test(operationId)) throw new ApiError('操作标识无效。');
  const scope = epoch;
  const result = await jsonRequest<{ operationId: string; applied: boolean }>(`/api/operations/${encodeURIComponent(operationId)}`, { authenticated: true });
  assertSessionEpoch(scope);
  if (!result || result.operationId !== operationId || typeof result.applied !== 'boolean') throw new ApiError('操作核对结果不完整，草稿仍保留。');
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
async function boundedImage(response: Response, signal: AbortSignal, media: MediaRef) {
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
      total += value.byteLength;
      if (total > media.size) throw new ApiError('图片大小与记录不一致。');
      chunks.push(new Uint8Array(value));
    }
    if (total !== media.size) throw new ApiError('图片下载不完整。');
    return new Blob(chunks, { type: mimeType });
  } finally { signal.removeEventListener('abort', abort); void reader.cancel().catch(() => {}); }
}
export async function downloadMedia(media: MediaRef, signal: AbortSignal): Promise<{ blob: Blob; transport: 's3' | 'server' }> {
  const scope = epoch;
  let authorization = await authorizeMedia(media, signal);
  for (let attempt = 0; attempt < 2; attempt++) {
    assertSessionEpoch(scope);
    if (authorization.expiresAt <= Date.now()) {
      if (attempt === 0) { authorization = await authorizeMedia(media, signal); continue; }
      break;
    }
    try {
      const blob = await request(authorization.url, { authenticated: true, signedMedia: authorization.transport === 's3', signal }, (response, requestSignal) => boundedImage(response, requestSignal, media));
      assertSessionEpoch(scope);
      return { blob, transport: authorization.transport };
    } catch (error) {
      assertSessionEpoch(scope);
      if (signal.aborted || error instanceof DOMException && error.name === 'AbortError') throw error;
      if (authorization.transport === 'server') throw error;
      if (attempt === 0 && error instanceof ApiError && error.status === 403) { authorization = await authorizeMedia(media, signal); continue; }
      break;
    }
  }
  assertSessionEpoch(scope);
  const blob = await request(`/api/media/${media.assetId}`, { authenticated: true, signal }, (response, requestSignal) => boundedImage(response, requestSignal, media));
  assertSessionEpoch(scope);
  return { blob, transport: 'server' };
}

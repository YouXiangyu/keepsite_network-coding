import { validateData, type AppData } from './data';

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
  generationEngine?: { available: boolean; reason?: string; pipeline?: string };
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
interface SessionEnd { reason: 'expired' | 'logout'; logoutUrl?: string }
const expiredListeners = new Set<(event: SessionEnd) => void>();

export const sessionEpoch = () => epoch;
export function assertSessionEpoch(expected: number) {
  if (expected !== epoch) staleRequest();
}

export function cancelSessionRequests() {
  epoch += 1;
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

interface RequestOptions extends RequestInit { authenticated?: boolean }
async function request<T>(path: string, options: RequestOptions, consume: (response: Response) => Promise<T>): Promise<T> {
  if (!path.startsWith('/api/') || path.includes('://')) throw new ApiError('请求地址无效。');
  const { authenticated = false, ...init } = options;
  if (authenticated && !activeSession) throw new ApiError('请先登录。', 401);
  const started = epoch;
  const headers = new Headers(init.headers);
  if (init.method && init.method !== 'GET') {
    headers.set('X-MemoVerse-Client', 'web');
    if (activeSession) headers.set('X-CSRF-Token', activeSession.csrfToken);
  }
  const controller = new AbortController();
  pending.add(controller);
  const abort = () => controller.abort();
  init.signal?.addEventListener('abort', abort, { once: true });
  if (init.signal?.aborted) abort();
  const timeout = setTimeout(abort, path === '/api/media' || path === '/api/backup' ? 120000 : 30000);
  try {
    const response = await fetch(path, { ...init, headers, credentials: 'same-origin', signal: controller.signal });
    if (started !== epoch || controller.signal.aborted) staleRequest();
    if (!response.ok) {
      let detail = `请求未成功（${response.status}），请重试。`;
      let code: string | undefined;
      try {
        const body = await response.json();
        if (typeof body.detail === 'string') detail = body.detail;
        if (typeof body.code === 'string') code = body.code;
      } catch { /* Preserve the HTTP status when an error body is unavailable. */ }
      if (started !== epoch) staleRequest();
      if (response.status === 401 && (authenticated || activeSession)) expireSession();
      throw new ApiError(detail, response.status, code);
    }
    const value = await consume(response);
    if (started !== epoch || controller.signal.aborted) staleRequest();
    return value;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (started !== epoch || init.signal?.aborted) staleRequest();
    throw new ApiError(controller.signal.aborted ? '请求超时，未提交内容仍保留，请重试。' : '暂时无法连接服务，请稍后重试。');
  } finally {
    clearTimeout(timeout);
    pending.delete(controller);
    init.signal?.removeEventListener('abort', abort);
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
  if (!value || typeof value !== 'object') throw new ApiError('收藏服务返回的数据格式不正确。');
  const state = value as ServerState;
  if (state.userId !== expectedUser || activeSession?.user.id !== expectedUser) {
    expireSession();
    throw new ApiError('账户状态已改变，请重新登录。', 401);
  }
  if (typeof state.initialized !== 'boolean' || typeof state.serverId !== 'string' || !state.serverId
      || !Number.isSafeInteger(state.revision) || state.revision < 0) {
    throw new ApiError('收藏状态不完整，未更新界面。');
  }
  if (state.initialized) validateData(state.data);
  else if (state.data !== null) throw new ApiError('收藏空间尚未准备完成。');
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
export async function uploadMedia(file: File): Promise<MediaAsset> {
  const scope = epoch;
  const body = new FormData();
  body.append('file', file);
  const asset = await jsonRequest<MediaAsset>('/api/media', { method: 'POST', body, authenticated: true });
  assertSessionEpoch(scope);
  if (!asset || typeof asset.assetId !== 'string' || !asset.url?.startsWith('/api/media/')) {
    throw new ApiError('图片上传结果不完整，请重试。');
  }
  return asset;
}
export function fetchModel(path: string, signal: AbortSignal) {
  return request(path, { signal, authenticated: true }, response => response.arrayBuffer());
}
export async function downloadBackup(): Promise<void> {
  const scope = epoch;
  const blob = await request('/api/backup', { authenticated: true }, response => response.blob());
  assertSessionEpoch(scope);
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = 'memoverse-backup.zip';
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

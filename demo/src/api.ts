import { makeSeedData, markMigrationCompleted, readMigrationSource, validateData, type AppData, type MigrationSource } from './data';

export const API_BASE = (import.meta.env?.VITE_API_BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
export interface ServerState {
  initialized: boolean;
  serverId: string;
  revision: number;
  data: AppData | null;
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
  constructor(message: string, status = 0) { super(message); this.name = 'ApiError'; this.status = status; }
}
export const errorMessage = (error: unknown) => error instanceof Error ? error.message : '请求失败，请重试。';

async function request(path: string, options: RequestInit = {}): Promise<Response> {
  const headers = new Headers(options.headers);
  if (options.method && options.method !== 'GET') headers.set('X-MemoVerse-Client', 'local');
  const controller = new AbortController();
  const abort = () => controller.abort();
  options.signal?.addEventListener('abort', abort, { once: true });
  if (options.signal?.aborted) controller.abort();
  const timeout = setTimeout(abort, path === '/api/bootstrap' || path === '/api/media' ? 120000 : 30000);
  try {
    const response = await fetch(`${API_BASE}${path}`, { ...options, headers, signal: controller.signal });
    if (!response.ok) {
      let detail = `请求未成功（${response.status}），请重试。`;
      try { const body = await response.json(); if (typeof body.detail === 'string') detail = body.detail; } catch { /* preserve HTTP error */ }
      throw new ApiError(detail, response.status);
    }
    return response;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (options.signal?.aborted) throw error;
    throw new ApiError(controller.signal.aborted ? '本机服务响应超时，草稿已保留，可重试。' : '无法连接本机服务，请确认服务运行后重试。');
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener('abort', abort);
  }
}

async function readState(response: Response): Promise<ServerState> {
  const state: unknown = await response.json();
  if (!state || typeof state !== 'object') throw new ApiError('服务返回的数据格式不正确。');
  const s = state as ServerState;
  if (typeof s.initialized !== 'boolean' || typeof s.serverId !== 'string' || !s.serverId
    || !Number.isSafeInteger(s.revision) || s.revision < 0) throw new ApiError('服务状态不完整，未更新本地界面。');
  if (s.initialized) validateData(s.data);
  else if (s.data !== null) throw new ApiError('未初始化服务返回了不一致的数据。');
  // Resolve only server-owned media paths. Domain objects and IDs stay unchanged.
  if (s.data) for (const item of [...s.data.collections, ...s.data.shares.map(share => share.item)]) {
    if (item.customImage?.startsWith('/api/')) item.customImage = new URL(item.customImage, `${API_BASE}/`).href;
    if (item.modelUrl?.startsWith('/api/')) item.modelUrl = new URL(item.modelUrl, `${API_BASE}/`).href;
  }
  return s;
}

export async function getState(signal?: AbortSignal): Promise<ServerState> {
  return readState(await request('/api/state', { signal }));
}

export async function mutate(path: string, method = 'POST', body?: unknown): Promise<ServerState> {
  return readState(await request(path, {
    method,
    ...(body === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
  }));
}

export function planInitialization(legacy: MigrationSource, state: Pick<ServerState, 'initialized' | 'serverId'>) {
  if (legacy.completedServerIds.includes(state.serverId)) {
    if (!state.initialized) throw new ApiError('已迁移的工作区变为空，请先检查本机数据或备份；不会重新写入旧快照。');
    return null;
  }
  if (state.initialized && !legacy.data) return null;
  return { migrationId: legacy.migrationId, data: legacy.data ?? makeSeedData() };
}

export async function initializeWorkspace(): Promise<ServerState> {
  const legacy = await readMigrationSource();
  const state = await getState();
  const plan = planInitialization(legacy, state);
  if (!plan) return state;
  // Existing legacy data must be imported explicitly even when the target is nonempty:
  // the server either recognizes this migration ID or reports a conflict, never merges.
  const imported = await mutate('/api/bootstrap', 'POST', plan);
  if (!imported.initialized || !imported.data) throw new ApiError('服务未确认迁移完成，原收藏仍保留。');
  await markMigrationCompleted(legacy.migrationId, imported.serverId);
  return imported;
}

export async function uploadMedia(file: File): Promise<MediaAsset> {
  const body = new FormData();
  body.append('file', file);
  const asset: MediaAsset = await (await request('/api/media', { method: 'POST', body })).json();
  if (!asset || typeof asset.assetId !== 'string' || typeof asset.url !== 'string') throw new ApiError('图片上传结果不完整，请重试。');
  return { ...asset, url: new URL(asset.url, `${API_BASE}/`).href };
}

export async function downloadBackup(): Promise<void> {
  const response = await request('/api/backup');
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = 'memoverse-local-backup.zip';
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

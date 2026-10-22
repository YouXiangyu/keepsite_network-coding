import { assertSessionEpoch, downloadMedia, errorMessage, sessionAccountId, sessionEpoch } from './api';
import type { MediaRef } from './data';

export const CACHE_LIMIT = 128 * 1024 * 1024;
export const CACHE_DATABASE = 'memoverse-media-cache-v1';
interface CachedMedia { accountId: string; sha256: string; blob: Blob; size: number; mimeType: string; accessedAt: number }
type Guard = () => void;
const abortError = () => new DOMException('图片读取已取消。', 'AbortError');
const storageFailure = (error: unknown) => error instanceof DOMException && error.name === 'QuotaExceededError' ? 'quota' : 'unavailable';

async function openCache(signal: AbortSignal): Promise<IDBDatabase> {
  if (signal.aborted) throw abortError();
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (error?: unknown, database?: IDBDatabase) => {
      if (settled) { database?.close(); return; }
      settled = true;
      clearTimeout(timer);
      signal.removeEventListener('abort', abort);
      if (error) reject(error); else resolve(database!);
    };
    const abort = () => finish(abortError());
    const timer = setTimeout(() => finish(new Error('本机缓存暂时不可用。')), 3000);
    signal.addEventListener('abort', abort, { once: true });
    try {
      const request = indexedDB.open(CACHE_DATABASE, 1);
      request.onupgradeneeded = () => {
        const store = request.result.createObjectStore('media', { keyPath: ['accountId', 'sha256'] });
        store.createIndex('accountId', 'accountId');
      };
      request.onerror = () => finish(request.error);
      request.onsuccess = () => finish(undefined, request.result);
    } catch (error) { finish(error); }
  });
}

async function transaction<T>(mode: IDBTransactionMode, signal: AbortSignal, guard: Guard,
  action: (store: IDBObjectStore, result: (value: T) => void, fail: (error: unknown) => void) => void): Promise<T> {
  const database = await openCache(signal);
  try {
    guard();
    return await new Promise<T>((resolve, reject) => {
      const tx = database.transaction('media', mode);
      let value: T;
      let failure: unknown;
      const abort = () => { try { tx.abort(); } catch { /* Already completed. */ } };
      signal.addEventListener('abort', abort, { once: true });
      tx.oncomplete = () => {
        signal.removeEventListener('abort', abort);
        try { guard(); resolve(value); } catch (error) { reject(error); }
      };
      tx.onabort = tx.onerror = () => {
        signal.removeEventListener('abort', abort);
        reject(signal.aborted ? abortError() : failure ?? tx.error ?? new Error('本机缓存不可用。'));
      };
      try {
        guard();
        action(tx.objectStore('media'), next => { value = next; }, error => { failure = error; abort(); });
      } catch (error) { failure = error; abort(); }
    });
  } finally { database.close(); }
}

async function readCached(accountId: string, hash: string, signal: AbortSignal, guard: Guard) {
  return transaction<CachedMedia | undefined>('readonly', signal, guard, (store, result) => {
    const request = store.get([accountId, hash]);
    request.onsuccess = () => result(request.result);
  });
}
async function inventory(accountId: string, signal: AbortSignal, guard: Guard) {
  return transaction<CachedMedia[]>('readonly', signal, guard, (store, result) => {
    const request = store.index('accountId').getAll(accountId);
    request.onsuccess = () => result(request.result);
  });
}
async function removeCached(accountId: string, keep: Set<string>, signal: AbortSignal, guard: Guard) {
  return transaction<void>('readwrite', signal, guard, (store, result, fail) => {
    const request = store.index('accountId').openCursor(IDBKeyRange.only(accountId));
    request.onsuccess = () => {
      try {
        guard();
        const cursor = request.result;
        if (!cursor) { result(); return; }
        if (!keep.has(cursor.value.sha256)) cursor.delete();
        cursor.continue();
      } catch (error) { fail(error); }
    };
  });
}
async function deleteCached(accountId: string, hash: string, signal: AbortSignal, guard: Guard) {
  return transaction<void>('readwrite', signal, guard, (store, result) => {
    store.delete([accountId, hash]);
    result();
  });
}
export function planCacheEviction(records: readonly { sha256: string; size: number; accessedAt: number }[], incomingSize: number,
  active: ReadonlySet<string>, limit = CACHE_LIMIT) {
  let bytes = records.reduce((total, record) => total + record.size, 0) + incomingSize;
  const removed: string[] = [];
  for (const record of [...records].sort((a, b) => a.accessedAt - b.accessedAt)) {
    if (bytes <= limit) break;
    if (active.has(record.sha256)) continue;
    removed.push(record.sha256);
    bytes -= record.size;
  }
  if (bytes > limit) throw new DOMException('本机缓存已达到 128 MiB 上限。', 'QuotaExceededError');
  return removed;
}
async function writeCached(accountId: string, media: MediaRef, blob: Blob, active: Set<string>, signal: AbortSignal, guard: Guard) {
  return transaction<void>('readwrite', signal, guard, (store, result, fail) => {
    const request = store.index('accountId').getAll(accountId);
    request.onsuccess = () => {
      try {
        guard();
        const records = (request.result as CachedMedia[]).filter(record => record.sha256 !== media.sha256);
        for (const hash of planCacheEviction(records.map(record => ({ ...record, size: record.blob.size })), blob.size, active)) store.delete([accountId, hash]);
        store.put({ accountId, sha256: media.sha256, blob, size: blob.size, mimeType: media.mimeType, accessedAt: Date.now() } satisfies CachedMedia);
        result();
      } catch (error) { fail(error); }
    };
  });
}

export async function verifyMediaBlob(blob: Blob, media: Pick<MediaRef, 'sha256' | 'size' | 'mimeType'>, guard: Guard = () => {}) {
  guard();
  if (!(blob instanceof Blob) || blob.size !== media.size || blob.type !== media.mimeType) throw new Error('图片大小或格式校验失败。');
  const bytes = await blob.arrayBuffer();
  guard();
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  guard();
  const hash = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('');
  if (hash !== media.sha256) throw new Error('图片完整性校验失败，请重试。');
}

export interface MediaView { status: 'idle' | 'loading' | 'cached' | 'temporary' | 'quota' | 'error'; url?: string; message: string; source?: 'cache' | 's3' | 'server' }
export interface CacheSummary { count: number; bytes: number; state: 'checking' | 'ready' | 'unavailable' | 'quota'; clearing: boolean; message: string }
interface Resource { media: MediaRef; view: MediaView; controller: AbortController; references: number }
const EMPTY_VIEW: MediaView = { status: 'idle', message: '图片尚未读取' };

/** Memory and object URLs live only inside the authenticated account tree. */
export class AccountMediaCache {
  readonly accountId: string;
  private readonly epoch: number;
  private generation = 0;
  private live = new Set<string>();
  private resources = new Map<string, Resource>();
  private downloads = 0;
  private queue: { resource: Resource; controller: AbortController }[] = [];
  private listeners = new Set<() => void>();
  private maintenance = new Set<AbortController>();
  private referenceController: AbortController | null = null;
  private summary: CacheSummary = { count: 0, bytes: 0, state: 'checking', clearing: false, message: '' };
  constructor(accountId: string) { this.accountId = accountId; this.epoch = sessionEpoch(); }
  subscribe = (listener: () => void) => { this.listeners.add(listener); return () => { this.listeners.delete(listener); }; };
  snapshot = (hash: string) => this.resources.get(hash)?.view ?? EMPTY_VIEW;
  cacheSummary = () => this.summary;
  private notify() { for (const listener of this.listeners) listener(); }
  private guard(signal: AbortSignal, generation = this.generation) {
    assertSessionEpoch(this.epoch);
    if (sessionAccountId() !== this.accountId || signal.aborted || generation !== this.generation) throw abortError();
  }
  private update(resource: Resource, view: MediaView) {
    if (this.resources.get(resource.media.sha256) !== resource) return;
    resource.view = view;
    this.notify();
  }
  private async refreshSummary(signal: AbortSignal) {
    const generation = this.generation;
    const guard = () => this.guard(signal, generation);
    try {
      const records = await inventory(this.accountId, signal, guard);
      guard();
      const stored = new Set(records.map(record => record.sha256));
      for (const resource of this.resources.values()) if (resource.view.status === 'cached' && !stored.has(resource.media.sha256)) {
        this.update(resource, { ...resource.view, status: 'temporary', message: '本机缓存已被回收 · 当前仅临时展示' });
      }
      this.summary = { ...this.summary, count: records.length, bytes: records.reduce((total, record) => total + (record.blob instanceof Blob ? record.blob.size : 0), 0), state: 'ready', message: '' };
      this.notify();
    } catch (error) {
      if (signal.aborted || generation !== this.generation) return;
      try { guard(); } catch { return; }
      this.summary = { ...this.summary, state: 'unavailable', message: '本机缓存不可用，图片仍可临时展示。' };
      this.notify();
    }
  }
  async refresh() {
    const controller = new AbortController();
    this.maintenance.add(controller);
    try { await this.refreshSummary(controller.signal); }
    finally { this.maintenance.delete(controller); }
  }
  setReferences(media: MediaRef[]) {
    const next = new Set(media.map(item => item.sha256));
    if (next.size === this.live.size && [...next].every(hash => this.live.has(hash)) && this.summary.state !== 'checking') return;
    this.referenceController?.abort();
    this.live = next;
    for (const [hash, resource] of this.resources) if (!next.has(hash)) this.releaseResource(hash, resource);
    const controller = new AbortController();
    this.referenceController = controller;
    this.maintenance.add(controller);
    const generation = this.generation;
    const guard = () => { this.guard(controller.signal, generation); if (this.live !== next) throw abortError(); };
    void removeCached(this.accountId, next, controller.signal, guard).then(() => this.refreshSummary(controller.signal))
      .catch(() => this.refreshSummary(controller.signal)).finally(() => this.maintenance.delete(controller));
  }
  acquire(media: MediaRef) {
    let resource = this.resources.get(media.sha256);
    if (!resource) {
      resource = { media, view: { status: 'loading', message: '正在读取图片…' }, controller: new AbortController(), references: 0 };
      this.resources.set(media.sha256, resource);
      this.enqueue(resource);
    }
    resource.references++;
    this.notify();
    return () => {
      resource!.references--;
      if (resource!.references <= 0) this.releaseResource(media.sha256, resource!);
    };
  }
  private releaseResource(hash: string, resource: Resource) {
    resource.controller.abort();
    if (resource.view.url) URL.revokeObjectURL(resource.view.url);
    if (this.resources.get(hash) === resource) this.resources.delete(hash);
    this.notify();
  }
  retry(hash: string) {
    const resource = this.resources.get(hash);
    if (!resource || resource.view.status === 'loading') return;
    resource.controller.abort();
    if (resource.view.url) URL.revokeObjectURL(resource.view.url);
    resource.controller = new AbortController();
    this.update(resource, { status: 'loading', message: '正在重新读取图片…' });
    this.enqueue(resource);
  }
  private enqueue(resource: Resource) {
    this.queue.push({ resource, controller: resource.controller });
    this.pump();
  }
  private pump() {
    while (this.downloads < 4 && this.queue.length) {
      const { resource, controller } = this.queue.shift()!;
      if (controller.signal.aborted || resource.controller !== controller || this.resources.get(resource.media.sha256) !== resource) continue;
      this.downloads++;
      void this.load(resource).finally(() => { this.downloads--; this.pump(); });
    }
  }
  private async load(resource: Resource) {
    const signal = resource.controller.signal;
    const generation = this.generation;
    const media = resource.media;
    const guard = () => {
      this.guard(signal, generation);
      if (!this.live.has(media.sha256) || this.resources.get(media.sha256) !== resource) throw abortError();
    };
    try {
      guard();
      let cached: CachedMedia | undefined;
      try { cached = await readCached(this.accountId, media.sha256, signal, guard); } catch { guard(); }
      if (cached) {
        try {
          if (cached.accountId !== this.accountId || cached.sha256 !== media.sha256) throw new Error('缓存归属异常。');
          await verifyMediaBlob(cached.blob, media, guard);
          guard();
          try { await writeCached(this.accountId, media, cached.blob, new Set(this.resources.keys()), signal, guard); } catch { guard(); }
          guard();
          this.update(resource, { status: 'cached', url: URL.createObjectURL(cached.blob), message: '本机缓存可用 · 已校验', source: 'cache' });
          return;
        } catch {
          guard();
          try { await deleteCached(this.accountId, media.sha256, signal, guard); await this.refreshSummary(signal); } catch { guard(); }
        }
      }
      const result = await downloadMedia(media, signal);
      await verifyMediaBlob(result.blob, media, guard);
      let status: MediaView['status'] = 'cached';
      let message = '已校验并保存到本机缓存';
      try {
        await writeCached(this.accountId, media, result.blob, new Set(this.resources.keys()), signal, guard);
      } catch (error) {
        guard();
        status = storageFailure(error) === 'quota' ? 'quota' : 'temporary';
        message = status === 'quota' ? '本机缓存空间不足 · 当前仅临时展示' : '本机缓存不可用 · 当前仅临时展示';
      }
      guard();
      this.update(resource, { status, url: URL.createObjectURL(result.blob), message, source: result.transport });
      await this.refreshSummary(signal);
      if (status === 'quota') { this.summary = { ...this.summary, state: 'quota', message }; this.notify(); }
    } catch (error) {
      if (signal.aborted || generation !== this.generation) return;
      try { guard(); } catch { return; }
      this.update(resource, { status: 'error', message: errorMessage(error) });
    }
  }
  async clear() {
    if (this.summary.clearing) return;
    for (const resource of this.resources.values()) resource.controller.abort();
    for (const controller of this.maintenance) controller.abort();
    const controller = new AbortController();
    this.maintenance.add(controller);
    const generation = this.generation;
    const guard = () => this.guard(controller.signal, generation);
    this.summary = { ...this.summary, clearing: true, message: '' };
    this.notify();
    try {
      await removeCached(this.accountId, new Set(), controller.signal, guard);
      for (const resource of this.resources.values()) this.update(resource, resource.view.url
        ? { ...resource.view, status: 'temporary', message: '本机缓存已清除 · 当前仅临时展示' }
        : { status: 'error', message: '清理缓存已取消下载，请重试图片。' });
      this.summary = { count: 0, bytes: 0, state: 'ready', clearing: false, message: '已清除本账号的本机缓存，云端记录和图片不受影响。' };
    } catch (error) {
      try { guard(); } catch { return; }
      this.summary = { ...this.summary, clearing: false, state: 'unavailable', message: '本机缓存清理未完成，请重试。' };
    } finally { this.maintenance.delete(controller); this.notify(); }
  }
  dispose() {
    this.generation++;
    this.queue = [];
    for (const controller of this.maintenance) controller.abort();
    this.maintenance.clear();
    for (const [hash, resource] of this.resources) this.releaseResource(hash, resource);
  }
}

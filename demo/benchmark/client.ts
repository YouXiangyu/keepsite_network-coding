import { assertSessionEpoch, getSession, installSession, onSessionChanged, sessionEpoch } from '../src/api';
import { AccountSync } from '../src/app/accountSync';
import { AccountMediaCache, CACHE_DATABASE, type MediaView } from '../src/mediaCache';
import { PeerMedia } from '../src/media/peerMedia';
import type { TransferRecord } from '../src/media/peerProtocol';
import type { MediaRef, Ticket } from '../src/data';

type TaskStatus = 'cached' | 'quota' | 'temporary' | 'error' | 'timeout' | 'cancelled';
export interface BenchmarkTask {
  assetId: string; source: MediaView['source'] | null; status: TaskStatus; bytes: number; receivedBytes: null;
  startedAtMs: number | null; finishedAtMs: number; elapsedMs: number | null; errorCode: string | null;
}
export interface BenchmarkRun {
  tasks: BenchmarkTask[]; elapsedMs: number; peakActive: number; timedOut: boolean;
  diagnostics: { records: (TransferRecord & { assetId: string | null })[]; recordCount: number; expectedPeerTransfers: number; collectedPeerTransfers: number; missingPeerTransfers: number; snapshotWindow: 50; overflow: boolean };
}
export interface BenchmarkVerification {
  ok: boolean;
  items: { assetId: string; ok: boolean; sha256: string | null; size: number | null; mimeType: string | null; errorCode: string | null }[];
}
interface Runtime {
  userId: string; epoch: number; sync: AccountSync; cache: AccountMediaCache; peer: PeerMedia; assets: Map<string, MediaRef>;
  abort: AbortController; active: number; unsubscribes: (() => void)[]; run?: Promise<BenchmarkRun>; verify?: Promise<BenchmarkVerification>;
}
const DEADLINE_MS = 120000;
let current: Runtime | null = null;
const aborted = () => new DOMException('Benchmark operation cancelled', 'AbortError');
function requireLocalFixture() { if (location.origin !== 'http://127.0.0.1:4178') throw new Error('Benchmark entry is restricted to its loopback fixture'); }
function guard(runtime: Runtime, signal = runtime.abort.signal) {
  assertSessionEpoch(runtime.epoch);
  if (current !== runtime || runtime.abort.signal.aborted || signal.aborted) throw aborted();
}
function selected(runtime: Runtime, values: unknown): MediaRef[] {
  if (!Array.isArray(values) || values.length < 1 || values.length > 300 || new Set(values).size !== values.length || values.some(value => typeof value !== 'string' || !runtime.assets.has(value))) throw new Error('Expected 1–300 unique owned asset identifiers');
  return values.map(value => runtime.assets.get(value)!);
}
function waitUntil(runtime: Runtime, subscribe: (listener: () => void) => () => void, ready: () => boolean, timeout: number) {
  return new Promise<void>((resolve, reject) => {
    let unsubscribe = () => {}, settled = false;
    const finish = (error?: unknown) => { if (settled) return; settled = true; unsubscribe(); clearTimeout(timer); runtime.abort.signal.removeEventListener('abort', abort); error ? reject(error) : resolve(); };
    const check = () => { try { guard(runtime); if (ready()) finish(); } catch (error) { finish(error); } };
    const abort = () => finish(aborted());
    const timer = setTimeout(() => finish(new Error('Benchmark bootstrap timed out')), timeout);
    runtime.abort.signal.addEventListener('abort', abort, { once: true }); unsubscribe = subscribe(check); check();
  });
}
async function boot(options: { peerWanted: boolean }) {
  requireLocalFixture();
  if (typeof options?.peerWanted !== 'boolean') throw new Error('peerWanted must be a boolean');
  await dispose();
  const session = await getSession(); if (!session.authenticated || !session.user) throw new Error('Fixture session is required'); installSession(session);
  const sync = new AccountSync(session.user.id), cache = new AccountMediaCache(session.user.id), peer = new PeerMedia(sync, cache); cache.setPeer(peer);
  const runtime: Runtime = { userId: session.user.id, epoch: sessionEpoch(), sync, cache, peer, assets: new Map(), abort: new AbortController(), active: 0, unsubscribes: [] };
  current = runtime; let tickets: Ticket[] | undefined;
  const update = () => {
    const view = sync.snapshot();
    if (view.ready && tickets !== view.data.tickets) {
      tickets = view.data.tickets;
      const media = tickets.filter(ticket => ticket.deletedAt === null).flatMap(ticket => [ticket.media.original, ...(ticket.media.poster ? [ticket.media.poster] : [])]);
      runtime.assets = new Map(media.map(asset => [asset.assetId, asset])); cache.setReferences(media);
    }
    peer.setSupported(view.ready && !!view.capabilities?.deviceSync);
  };
  const foreground = () => { peer.setForeground(document.visibilityState !== 'hidden'); if (document.visibilityState !== 'hidden') { sync.refresh(); void cache.refresh(); } };
  const leave = () => { void dispose(); };
  runtime.unsubscribes.push(sync.subscribe(update), onSessionChanged(leave));
  for (const [target, name, callback] of [[window, 'focus', foreground], [window, 'online', foreground], [document, 'visibilitychange', foreground], [window, 'pagehide', leave]] as const) {
    target.addEventListener(name, callback); runtime.unsubscribes.push(() => target.removeEventListener(name, callback));
  }
  peer.setWanted(options.peerWanted); peer.setForeground(document.visibilityState !== 'hidden'); peer.start(); sync.start();
  try { await waitUntil(runtime, sync.subscribe, () => sync.snapshot().ready, 30000); guard(runtime); }
  catch (error) { await dispose(); throw error; }
  return { userId: runtime.userId, serverId: sync.snapshot().serverId, ticketCount: sync.snapshot().data.tickets.length };
}
function status() {
  const runtime = current;
  if (!runtime) return { booted: false, userId: null, serverId: null, ticketCount: 0, peer: { peerId: null, enabled: false, inventoryCount: 0, wanted: false, reason: 'disposed', activeTransfers: 0 }, inflight: 0, running: false };
  const peer = runtime.peer.snapshot(), view = runtime.sync.snapshot();
  return { booted: view.ready, userId: runtime.userId, serverId: view.serverId, ticketCount: view.data.tickets.length,
    peer: { peerId: peer.peerId, enabled: peer.enabled, inventoryCount: peer.inventoryCount, wanted: peer.wanted, reason: peer.reason, activeTransfers: peer.receiving + peer.sending },
    cache: runtime.cache.cacheSummary(), inflight: runtime.active, running: !!runtime.run || !!runtime.verify };
}
async function setPeerWanted(wanted: boolean) { const runtime = current; if (!runtime || typeof wanted !== 'boolean') throw new Error('Benchmark is not booted or mode is invalid'); guard(runtime); runtime.peer.setWanted(wanted); }

function acquire(runtime: Runtime, media: MediaRef, signal: AbortSignal): Promise<BenchmarkTask> {
  const start = performance.now(); runtime.active++;
  return new Promise(resolve => {
    let unsubscribe = () => {}, release = () => {}, settled = false;
    const finish = (view?: MediaView) => {
      if (settled) return; settled = true; const end = performance.now();
      unsubscribe(); signal.removeEventListener('abort', cancel); runtime.abort.signal.removeEventListener('abort', cancel); release(); runtime.active--;
      const cancelled = runtime.abort.signal.aborted;
      const state: TaskStatus = view ? view.status as TaskStatus : cancelled ? 'cancelled' : 'timeout';
      resolve({ assetId: media.assetId, source: view?.source ?? null, status: state, bytes: state === 'cached' ? media.size : 0, receivedBytes: null,
        startedAtMs: start, finishedAtMs: end, elapsedMs: end - start, errorCode: state === 'cached' ? null : state === 'error' ? 'cache_error' : state });
    };
    const cancel = () => finish();
    const check = () => {
      if (signal.aborted || runtime.abort.signal.aborted || current !== runtime) { finish(); return; }
      const view = runtime.cache.snapshot(media.sha256); if (['cached', 'quota', 'temporary', 'error'].includes(view.status)) finish(view);
    };
    unsubscribe = runtime.cache.subscribe(check); signal.addEventListener('abort', cancel, { once: true }); runtime.abort.signal.addEventListener('abort', cancel, { once: true });
    if (signal.aborted || runtime.abort.signal.aborted) { finish(); return; }
    try { release = runtime.cache.acquire(media); check(); } catch { finish({ status: 'error', message: 'Unable to acquire media' }); }
    // acquire() may synchronously notify an already available resource.
    if (settled) release();
  });
}
async function run(options: { assetIds: string[]; concurrency: 1 | 2 | 4 }): Promise<BenchmarkRun> {
  const runtime = current; if (!runtime) throw new Error('Benchmark is not booted'); guard(runtime);
  if (runtime.run || runtime.verify || ![1, 2, 4].includes(options?.concurrency)) throw new Error('Expected one phase with concurrency 1, 2 or 4');
  const assets = selected(runtime, options.assetIds), controller = new AbortController(), start = performance.now();
  const original = new Set(runtime.peer.snapshot().recent.map(record => record.id)), collected = new Map<string, TransferRecord>(), expected = new Set<string>(), transferAssets = new Map<string, string>();
  let overflow = false, peakActive = 0, cursor = 0;
  const collect = () => { for (const record of runtime.peer.snapshot().recent) if (!original.has(record.id)) { if (collected.size >= 1200 && !collected.has(record.id)) { overflow = true; continue; } collected.set(record.id, { ...record }); } };
  const stopRecords = runtime.peer.subscribe(collect), stopEvents = runtime.sync.subscribeEvents(event => {
    if (event.transferId && ['peer.pending', 'peer.request', 'peer.authorized'].includes(event.type)) {
      if (expected.size < 1200) expected.add(event.transferId); else overflow = true;
      if (event.media) transferAssets.set(event.transferId, event.media.assetId);
    }
  });
  const timer = setTimeout(() => controller.abort(), DEADLINE_MS), tasks: (BenchmarkTask | undefined)[] = new Array(assets.length);
  const operation = (async () => {
    const worker = async () => {
      while (cursor < assets.length && !controller.signal.aborted && !runtime.abort.signal.aborted) {
        const index = cursor++, task = acquire(runtime, assets[index], controller.signal); peakActive = Math.max(peakActive, runtime.active);
        tasks[index] = await task;
      }
    };
    try {
      await Promise.all(Array.from({ length: options.concurrency }, worker));
      const end = performance.now(); clearTimeout(timer);
      for (let index = 0; index < tasks.length; index++) if (!tasks[index]) tasks[index] = { assetId: assets[index].assetId, source: null, status: runtime.abort.signal.aborted ? 'cancelled' : 'timeout', bytes: 0, receivedBytes: null,
        startedAtMs: null, finishedAtMs: end, elapsedMs: null, errorCode: runtime.abort.signal.aborted ? 'phase_cancelled_before_start' : 'phase_timeout_before_start' };
      // Allow the public, throttled diagnostic view to publish its final records.
      // This is excluded from the task/phase latency sample.
      if (!runtime.abort.signal.aborted) await new Promise(resolve => setTimeout(resolve, 120));
      collect(); const records = [...collected.values()].map(record => ({ ...record, assetId: transferAssets.get(record.id) ?? null }));
      const received = records.filter(record => record.path === 'peer' && expected.has(record.id)).length;
      return { tasks: tasks as BenchmarkTask[], elapsedMs: end - start, peakActive, timedOut: controller.signal.aborted,
        diagnostics: { records, recordCount: records.length, expectedPeerTransfers: expected.size, collectedPeerTransfers: received, missingPeerTransfers: Math.max(0, expected.size - received), snapshotWindow: 50 as const, overflow } };
    } finally { clearTimeout(timer); stopRecords(); stopEvents(); }
  })();
  runtime.run = operation;
  try { return await operation; } finally { if (runtime.run === operation) runtime.run = undefined; }
}

async function openReadonly(runtime: Runtime, signal: AbortSignal): Promise<IDBDatabase> {
  guard(runtime, signal);
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(CACHE_DATABASE); let settled = false;
    const finish = (error?: unknown, database?: IDBDatabase) => {
      if (settled) { database?.close(); return; } settled = true; clearTimeout(timer); signal.removeEventListener('abort', abort);
      error ? reject(error) : resolve(database!);
    };
    const abort = () => finish(aborted()), timer = setTimeout(() => finish(new Error('Cache open timed out')), 3000);
    signal.addEventListener('abort', abort, { once: true });
    request.onupgradeneeded = () => { request.transaction?.abort(); finish(new Error('Cache does not exist')); };
    request.onerror = () => finish(request.error);
    request.onsuccess = () => finish(undefined, request.result);
  });
}
async function verify(options: { assetIds: string[] }): Promise<BenchmarkVerification> {
  const runtime = current; if (!runtime) throw new Error('Benchmark is not booted'); guard(runtime);
  if (runtime.run || runtime.verify) throw new Error('Verification must run outside the timed phase');
  const assets = selected(runtime, options?.assetIds), controller = new AbortController(), abort = () => controller.abort();
  runtime.abort.signal.addEventListener('abort', abort, { once: true }); const timer = setTimeout(abort, DEADLINE_MS);
  const operation = (async () => {
    let database: IDBDatabase;
    const items: BenchmarkVerification['items'] = [];
    try { database = await openReadonly(runtime, controller.signal); }
    catch { return { ok: false, items: assets.map(media => ({ assetId: media.assetId, ok: false, sha256: null, size: null, mimeType: null, errorCode: controller.signal.aborted ? 'cancelled' : 'storage_unavailable' })) }; }
    try {
      for (const media of assets) {
        const item: BenchmarkVerification['items'][number] = { assetId: media.assetId, ok: false, sha256: null, size: null, mimeType: null, errorCode: null };
        try {
          guard(runtime, controller.signal);
          const record = await new Promise<{ blob?: Blob; accountId?: string; sha256?: string } | undefined>((resolve, reject) => {
            const transaction = database.transaction('media', 'readonly'), request = transaction.objectStore('media').get([runtime.userId, media.sha256]); let value: { blob?: Blob; accountId?: string; sha256?: string } | undefined;
            const cancel = () => { try { transaction.abort(); } catch { /* An already-finished readonly transaction has no work left to cancel. */ } }; controller.signal.addEventListener('abort', cancel, { once: true });
            request.onsuccess = () => { value = request.result; };
            transaction.oncomplete = () => { controller.signal.removeEventListener('abort', cancel); resolve(value); };
            transaction.onerror = transaction.onabort = () => { controller.signal.removeEventListener('abort', cancel); reject(transaction.error ?? aborted()); };
          });
          guard(runtime, controller.signal);
          if (!record || record.accountId !== runtime.userId || record.sha256 !== media.sha256 || !(record.blob instanceof Blob)) { item.errorCode = 'missing_or_invalid_cache'; items.push(item); continue; }
          item.size = record.blob.size; item.mimeType = record.blob.type;
          if (item.size !== media.size || item.mimeType !== media.mimeType || item.size > 12 * 1024 * 1024) { item.errorCode = 'size_or_type_mismatch'; items.push(item); continue; }
          const bytes = await record.blob.arrayBuffer(); guard(runtime, controller.signal);
          const hash = await crypto.subtle.digest('SHA-256', bytes); guard(runtime, controller.signal);
          item.sha256 = [...new Uint8Array(hash)].map(value => value.toString(16).padStart(2, '0')).join(''); item.ok = item.sha256 === media.sha256; item.errorCode = item.ok ? null : 'checksum_mismatch';
        } catch { item.errorCode = controller.signal.aborted ? 'cancelled' : 'storage_error'; }
        items.push(item);
      }
      return { ok: items.every(item => item.ok), items };
    } finally { database.close(); }
  })();
  runtime.verify = operation;
  try { return await operation; } finally { clearTimeout(timer); runtime.abort.signal.removeEventListener('abort', abort); if (runtime.verify === operation) runtime.verify = undefined; }
}
async function dispose() {
  const runtime = current; if (!runtime) return;
  current = null; for (const stop of runtime.unsubscribes.splice(0)) stop(); runtime.abort.abort();
  runtime.peer.stop(); runtime.cache.dispose(); runtime.sync.stop(); installSession(null);
  await Promise.allSettled([runtime.run, runtime.verify].filter(Boolean));
}

export const benchmark = { boot, status, setPeerWanted, run, verify, dispose };
declare global { interface Window { __memoVerseBenchmark: typeof benchmark } }
requireLocalFixture(); window.__memoVerseBenchmark = benchmark;

import { createContext, useContext, useEffect, useLayoutEffect, useMemo, useSyncExternalStore, type ReactNode } from 'react';
import { onSessionChanged, sessionEpoch } from './api';
import { AccountMediaCache } from './mediaCache';
import type { MediaRef, Ticket } from './data';

const MediaContext = createContext<AccountMediaCache | null>(null);
export function MediaProvider({ accountId, ready, tickets, children }: { accountId: string; ready: boolean; tickets: Ticket[]; children: ReactNode }) {
  const epoch = sessionEpoch();
  const cache = useMemo(() => new AccountMediaCache(accountId), [accountId, epoch]);
  useLayoutEffect(() => {
    if (ready) cache.setReferences(tickets.filter(ticket => ticket.deletedAt === null).flatMap(ticket => [ticket.media.original, ...(ticket.media.poster ? [ticket.media.poster] : [])]));
  }, [cache, ready, tickets]);
  useEffect(() => {
    const unsubscribe = onSessionChanged(() => cache.dispose());
    const refresh = () => { if (document.visibilityState !== 'hidden') void cache.refresh(); };
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh); unsubscribe(); cache.dispose(); };
  }, [cache]);
  return <MediaContext.Provider value={cache}>{children}</MediaContext.Provider>;
}
function useCache() {
  const cache = useContext(MediaContext);
  if (!cache) throw new Error('图片必须在已登录账号内读取。');
  return cache;
}
export function useMedia(media?: MediaRef, enabled = true) {
  const cache = useCache();
  const hash = media?.sha256 ?? '';
  const snapshot = useSyncExternalStore(cache.subscribe, () => cache.snapshot(hash));
  useEffect(() => {
    if (media && enabled) return cache.acquire(media);
  }, [cache, hash, media?.size, media?.mimeType, enabled]);
  return { ...snapshot, retry: () => cache.retry(hash) };
}
export function CacheStatus() {
  const cache = useCache();
  const summary = useSyncExternalStore(cache.subscribe, cache.cacheSummary);
  useEffect(() => { void cache.refresh(); }, [cache]);
  return <><h2>当前设备缓存</h2><p>{summary.state === 'checking' ? '正在检查本机缓存…' : `本账号已存 ${summary.count} 张图片 · ${(summary.bytes / 1024 / 1024).toFixed(1)} MiB / 128 MiB`}</p>
    <p>读取时校验完整性。浏览器可能回收缓存；已云存图片可重新获取。</p>
    {summary.message && <p role="status">{summary.message}</p>}
    <button className="secondary-button" disabled={summary.clearing} onClick={() => void cache.clear()}>{summary.clearing ? '正在清理…' : '清除本账号本机缓存'}</button></>;
}

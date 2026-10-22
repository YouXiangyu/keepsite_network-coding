import { createContext, useContext, useEffect, useLayoutEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from 'react';
import { onSessionChanged, sessionEpoch } from './api';
import { AccountMediaCache } from './mediaCache';
import type { MediaRef, Ticket } from './data';
import { PeerMedia } from './media/peerMedia';
import type { AccountEventPort } from './media/peerProtocol';

const MediaContext = createContext<AccountMediaCache | null>(null);
const PeerContext = createContext<{ peer: PeerMedia; wanted: boolean; setWanted(value: boolean): void } | null>(null);
export function MediaProvider({ accountId, ready, tickets, eventPort, deviceSync, children }: { accountId: string; ready: boolean; tickets: Ticket[]; eventPort: AccountEventPort; deviceSync: boolean; children: ReactNode }) {
  const epoch = sessionEpoch();
  const [wanted, setWanted] = useState(true);
  const { cache, peer } = useMemo(() => { const cache = new AccountMediaCache(accountId), peer = new PeerMedia(eventPort, cache); cache.setPeer(peer); return { cache, peer }; }, [accountId, epoch, eventPort]);
  useLayoutEffect(() => {
    if (ready) cache.setReferences(tickets.filter(ticket => ticket.deletedAt === null).flatMap(ticket => [ticket.media.original, ...(ticket.media.poster ? [ticket.media.poster] : [])]));
  }, [cache, ready, tickets]);
  useEffect(() => { peer.setSupported(ready && deviceSync); peer.setWanted(wanted); }, [peer, ready, deviceSync, wanted]);
  useEffect(() => {
    peer.setForeground(document.visibilityState !== 'hidden'); peer.start();
    const unsubscribe = onSessionChanged(() => { peer.stop(); cache.dispose(); });
    const refresh = () => { peer.setForeground(document.visibilityState !== 'hidden'); if (document.visibilityState !== 'hidden') void cache.refresh(); };
    window.addEventListener('focus', refresh);
    document.addEventListener('visibilitychange', refresh);
    return () => { window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh); unsubscribe(); peer.stop(); cache.dispose(); };
  }, [cache, peer]);
  return <MediaContext.Provider value={cache}><PeerContext.Provider value={{ peer, wanted, setWanted }}>{children}</PeerContext.Provider></MediaContext.Provider>;
}
export function usePeerStatus() {
  const value = useContext(PeerContext); if (!value) throw new Error('设备状态必须在已登录账号内读取。');
  const state = useSyncExternalStore(value.peer.subscribe, value.peer.snapshot);
  return { ...state, wanted: value.wanted, setWanted: value.setWanted, refresh: value.peer.refreshPeers };
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

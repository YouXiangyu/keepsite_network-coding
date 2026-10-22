import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import { assertSessionEpoch, errorMessage, onSessionChanged, sessionEpoch, type MutationReceipt, type SignedInSession, type SyncPosition } from '../api';
import type { AppData } from '../data';
import { AccountSync } from './accountSync';

export interface OperationHandlers<T> { onStart?: () => void; onSuccess?: (value: T) => void; onError?: (error: unknown) => void; onCancel?: () => void }
export type RunMutation = (operation: () => Promise<MutationReceipt>, handlers?: OperationHandlers<AppData>) => Promise<void>;
export type RunReconciliation = (operation: () => Promise<SyncPosition>, handlers?: OperationHandlers<AppData>) => Promise<void>;
const cancelled = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';

export function useAccountWorkspace(session: SignedInSession) {
  const [sync] = useState(() => new AccountSync(session.user.id));
  const state = useSyncExternalStore(sync.subscribe, sync.snapshot);
  const [busy, setBusy] = useState(false), [storageError, setStorageError] = useState('');
  const alive = useRef(true), lock = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    sync.start();
    const unsubscribe = onSessionChanged(() => sync.stop());
    const refresh = () => { if (document.visibilityState !== 'hidden') sync.refresh(); };
    window.addEventListener('online', refresh); window.addEventListener('focus', refresh); document.addEventListener('visibilitychange', refresh);
    return () => { unsubscribe(); sync.stop(); window.removeEventListener('online', refresh); window.removeEventListener('focus', refresh); document.removeEventListener('visibilitychange', refresh); };
  }, [sync, session.csrfToken]);
  const runExclusive = useCallback(async <T,>(operation: () => Promise<T>, handlers: OperationHandlers<T> = {}) => {
    if (lock.current) return;
    lock.current = true; setBusy(true); const scope = sessionEpoch();
    try {
      handlers.onStart?.();
      const result = await operation(); assertSessionEpoch(scope);
      if (alive.current) handlers.onSuccess?.(result);
    } catch (error) {
      if (alive.current && cancelled(error)) handlers.onCancel?.();
      else if (alive.current) { if (handlers.onError) handlers.onError(error); else setStorageError(errorMessage(error)); }
    } finally { lock.current = false; if (alive.current) setBusy(false); }
  }, []);
  const runMutation: RunMutation = useCallback((operation, handlers = {}) => runExclusive(async () => {
    const scope = sessionEpoch();
    const receipt = await operation(); assertSessionEpoch(scope);
    // Reads use the independent single pull loop, never the user mutation lock.
    return sync.catchUp(receipt.committedCursor, false, receipt.serverId);
  }, { ...handlers, onStart: () => { setStorageError(''); handlers.onStart?.(); } }), [sync, runExclusive]);
  const runReconciliation: RunReconciliation = useCallback((operation, handlers = {}) => runExclusive(async () => {
    const scope = sessionEpoch();
    const receipt = await operation(); assertSessionEpoch(scope);
    return sync.catchUp(receipt.committedCursor ?? 0, true, receipt.serverId);
  }, handlers), [sync, runExclusive]);
  return { data: state.data, ready: state.ready, rebuilding: state.rebuilding, connected: state.connected,
    busy: busy || state.rebuilding, capabilities: state.capabilities, storageError, syncError: state.error,
    setStorageError, runMutation, runReconciliation, runExclusive, retry: () => { setStorageError(''); sync.refresh(); } };
}

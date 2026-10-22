import { useCallback, useEffect, useRef, useState } from 'react';
import { assertSessionEpoch, errorMessage, getState, sessionEpoch, type ServerState, type SignedInSession } from '../api';
import type { AppData } from '../data';

export interface OperationHandlers<T> {
  onStart?: () => void;
  onSuccess?: (value: T) => void;
  onError?: (error: unknown) => void;
  onCancel?: () => void;
}
export type RunMutation = (operation: () => Promise<ServerState>, handlers?: OperationHandlers<AppData>) => Promise<void>;

const emptyData = (): AppData => ({ tickets: [] });
const cancelled = (error: unknown) => error instanceof DOMException && error.name === 'AbortError';

export function useAccountWorkspace(session: SignedInSession, watchDetail: boolean) {
  const [data, setData] = useState<AppData>(emptyData);
  const [ready, setReady] = useState(false);
  const [bootAttempt, setBootAttempt] = useState(0);
  const [storageError, setStorageError] = useState('');
  const [pollError, setPollError] = useState('');
  const [busy, setBusy] = useState(false);
  const [capabilities, setCapabilities] = useState<ServerState['capabilities']>();
  const alive = useRef(true);
  const operationLock = useRef(false);
  const operationGeneration = useRef(0);
  const activePoll = useRef<AbortController | null>(null);
  const latest = useRef(data);
  const revision = useRef({ serverId: '', value: -1, requestOrder: -1 });
  const requestOrder = useRef(0);
  const retryAction = useRef<(() => void) | null>(null);

  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const acceptState = useCallback((state: ServerState, order: number) => {
    if (!state.initialized || !state.data) throw new Error('票根空间正在准备中，请稍后重试。');
    if (revision.current.serverId && revision.current.serverId !== state.serverId) throw new Error('票根空间已更新，请重新加载后继续。');
    if (state.revision > revision.current.value || state.revision === revision.current.value && order >= revision.current.requestOrder) {
      revision.current = { serverId: state.serverId, value: state.revision, requestOrder: order };
      latest.current = state.data;
      setData(state.data);
      setCapabilities(state.capabilities);
    }
    return latest.current;
  }, []);

  useEffect(() => {
    let live = true;
    const controller = new AbortController();
    const order = ++requestOrder.current;
    const generation = operationGeneration.current;
    setStorageError('');
    getState(controller.signal).then(state => {
      if (live && !operationLock.current && generation === operationGeneration.current) { acceptState(state, order); setReady(true); }
    }).catch(error => {
      if (live && !operationLock.current && generation === operationGeneration.current && !cancelled(error)) setStorageError(errorMessage(error));
    });
    return () => { live = false; controller.abort(); };
  }, [bootAttempt, session.csrfToken, acceptState]);

  useEffect(() => {
    if (!ready || !watchDetail) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController | null = null;
    const poll = async () => {
      if (operationLock.current) {
        timer = setTimeout(poll, 10000);
        return;
      }
      controller = new AbortController();
      activePoll.current = controller;
      const generation = operationGeneration.current;
      const order = ++requestOrder.current;
      try {
        const state = await getState(controller.signal);
        if (live && !operationLock.current && generation === operationGeneration.current) { acceptState(state, order); setPollError(''); }
      } catch (error) {
        if (live && !operationLock.current && generation === operationGeneration.current && !cancelled(error)) setPollError(errorMessage(error));
      } finally {
        if (activePoll.current === controller) activePoll.current = null;
        if (live) timer = setTimeout(poll, 10000);
      }
    };
    timer = setTimeout(poll, 10000);
    return () => {
      live = false;
      clearTimeout(timer);
      controller?.abort();
      if (activePoll.current === controller) activePoll.current = null;
    };
  }, [ready, watchDetail, session.csrfToken, acceptState]);

  // A single lock covers business writes, upload-and-save.
  const runExclusive = useCallback(async <T,>(operation: () => Promise<T>, handlers: OperationHandlers<T> = {}) => {
    if (operationLock.current) return;
    operationLock.current = true;
    operationGeneration.current += 1;
    activePoll.current?.abort();
    const scope = sessionEpoch();
    setBusy(true);
    try {
      handlers.onStart?.();
      const result = await operation();
      assertSessionEpoch(scope);
      if (alive.current) handlers.onSuccess?.(result);
    } catch (error) {
      if (alive.current && cancelled(error)) handlers.onCancel?.();
      else if (alive.current) {
        if (handlers.onError) handlers.onError(error);
        else setStorageError(errorMessage(error));
      }
    } finally {
      operationGeneration.current += 1;
      operationLock.current = false;
      if (alive.current) setBusy(false);
    }
  }, []);

  const runMutation: RunMutation = useCallback((operation, handlers = {}) => runExclusive(async () => {
    const scope = sessionEpoch();
    const order = ++requestOrder.current;
    const state = await operation();
    assertSessionEpoch(scope);
    if (!alive.current) throw new DOMException('账户页面已关闭。', 'AbortError');
    return acceptState(state, order);
  }, {
    onStart: () => { if (!handlers.onError) setStorageError(''); handlers.onStart?.(); },
    onSuccess: next => { retryAction.current = null; handlers.onSuccess?.(next); },
    onCancel: handlers.onCancel,
    onError: error => {
      if (handlers.onError) handlers.onError(error);
      else {
        setStorageError(errorMessage(error));
        retryAction.current = () => { void runMutation(operation, handlers); };
      }
    },
  }), [acceptState, runExclusive]);

  const retry = () => {
    if (!ready) { setBootAttempt(value => value + 1); return; }
    if (storageError && retryAction.current) { retryAction.current(); return; }
    void runMutation(() => getState(), { onSuccess: () => { setPollError(''); setStorageError(''); } });
  };

  return { data, ready, busy, capabilities, storageError, pollError, setStorageError, runMutation, runExclusive, retry };
}

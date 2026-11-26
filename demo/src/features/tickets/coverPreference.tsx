import { createContext, useContext, useEffect, useRef, useState } from 'react';
import { errorMessage, getCoverPreference, setCoverPreference, type CoverPreference } from '../../api';
import type { Ticket } from '../../data';

export const CoverContext = createContext<CoverPreference>('poster');
export function selectCover(ticket: Ticket, preference: CoverPreference) {
  return preference === 'original' ? ticket.media.original : ticket.media.poster ?? ticket.media.original;
}
export function useCoverPreference() {
  const [preference, setPreference] = useState<CoverPreference | null>(null);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const active = useRef(false), locked = useRef(false), generation = useRef(0);
  const request = useRef<AbortController | null>(null);
  const refresh = async () => {
    if (!active.current || locked.current || request.current || document.visibilityState === 'hidden') return;
    const controller = new AbortController(); request.current = controller;
    const version = generation.current;
    try {
      const value = await getCoverPreference(controller.signal);
      if (active.current && !controller.signal.aborted && version === generation.current) { setPreference(value); setError(''); }
    } catch (failure) {
      if (active.current && !controller.signal.aborted && version === generation.current) setError(errorMessage(failure));
    } finally { if (request.current === controller) request.current = null; }
  };
  useEffect(() => {
    active.current = true;
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    const wake = () => void refresh();
    window.addEventListener('focus', wake); window.addEventListener('online', wake);
    document.addEventListener('visibilitychange', wake);
    return () => {
      active.current = false; generation.current++; request.current?.abort(); request.current = null;
      window.clearInterval(timer); window.removeEventListener('focus', wake); window.removeEventListener('online', wake);
      document.removeEventListener('visibilitychange', wake);
    };
  }, []);
  const save = async (value: CoverPreference) => {
    if (!active.current || locked.current) return;
    locked.current = true; generation.current++; request.current?.abort(); request.current = null;
    setSaving(true); setError('');
    const version = generation.current;
    try {
      const result = await setCoverPreference(value);
      if (active.current && version === generation.current) setPreference(result);
    } catch (failure) {
      if (active.current && version === generation.current) setError(errorMessage(failure));
    } finally {
      locked.current = false;
      if (active.current) setSaving(false);
    }
  };
  return { preference, error, saving, save, refresh };
}
export function useTicketCover(ticket: Ticket) {
  return selectCover(ticket, useContext(CoverContext));
}

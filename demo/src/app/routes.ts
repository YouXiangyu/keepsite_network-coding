export type Route = { type: 'tickets' | 'devices' | 'new' | 'not-found' } | { type: 'detail' | 'edit'; id: string };
export function parseRoute(path: string): Route {
  let parts: string[];
  try { parts = new URL(path, window.location.origin).pathname.split('/').filter(Boolean).map(decodeURIComponent); }
  catch { return { type: 'not-found' }; }
  if (!parts.length || parts.length === 1 && parts[0] === 'tickets') return { type: 'tickets' };
  if (parts.length === 1 && parts[0] === 'devices') return { type: 'devices' };
  if (parts[0] === 'tickets' && parts[1] === 'new' && parts.length === 2) return { type: 'new' };
  if (parts[0] === 'tickets' && parts.length === 2) return { type: 'detail', id: parts[1] };
  if (parts[0] === 'tickets' && parts.length === 3 && parts[2] === 'edit') return { type: 'edit', id: parts[1] };
  return { type: 'not-found' };
}

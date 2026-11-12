import type { TicketOrder } from '../features/tickets/collections';

export type Route = { type: 'tickets' | 'devices' | 'network' | 'new' | 'mine' | 'months' | 'not-found' }
  | { type: 'month'; month: string } | { type: 'detail' | 'edit'; id: string; from: string };
export interface ListContext { path: string; search: string; sort: TicketOrder }
const routeOrigin = 'https://keepsite.invalid';
const monthPattern = /^(?!0000)\d{4}-(0[1-9]|1[0-2])$/;
const validMonth = (month: string) => month.length === 7 && monthPattern.test(month);
function listPathname(path: string): string | null {
  if (path === '/' || path === '/tickets') return '/tickets';
  if (!path.startsWith('/months/') || path.slice(8).includes('/')) return null;
  try {
    const month = decodeURIComponent(path.slice(8));
    return validMonth(month) ? `/months/${month}` : null;
  } catch { return null; }
}

/** Read only our list options; unrelated query parameters keep their existing meaning. */
export function listContext(path: string): ListContext | null {
  try {
    const url = new URL(path, routeOrigin), pathname = listPathname(url.pathname);
    if (!pathname) return null;
    const searches = url.searchParams.getAll('search'), sorts = url.searchParams.getAll('sort');
    return { path: pathname, search: searches.length === 1 ? searches[0] : '', sort: sorts.length === 1 && sorts[0] === 'oldest' ? 'oldest' : 'recent' };
  } catch { return null; }
}

/** Can serialize local filter state on a detail link without navigating on each keystroke. */
export function listPath(context: ListContext): string {
  const pathname = listPathname(context.path) ?? '/tickets', query = new URLSearchParams();
  if (context.search) query.set('search', context.search);
  if (context.sort === 'oldest') query.set('sort', context.sort);
  const suffix = query.toString();
  return pathname + (suffix ? `?${suffix}` : '');
}

function safeSource(path: string): string {
  if (!path.startsWith('/') || path.startsWith('//') || /[\\#\u0000-\u0020\u007f]/.test(path)) return '/tickets';
  try {
    const url = new URL(path, routeOrigin), rawPath = path.split('?')[0];
    if (url.origin !== routeOrigin || url.pathname !== rawPath) return '/tickets';
    const keys = [...url.searchParams.keys()];
    if (url.pathname === '/mine' || url.pathname === '/months') return keys.length ? '/tickets' : url.pathname;
    if (!listPathname(rawPath) || rawPath === '/' || keys.some(key => !['search', 'sort'].includes(key)) ||
      new Set(keys).size !== keys.length || url.searchParams.has('sort') && !['recent', 'oldest'].includes(url.searchParams.get('sort')!)) return '/tickets';
    return listPath(listContext(path)!);
  } catch { return '/tickets'; }
}

function sourceFrom(url: URL): string {
  const sources = url.searchParams.getAll('from');
  return sources.length === 1 ? safeSource(sources[0]) : '/tickets';
}

export function detailPath(id: string, from = '/tickets'): string {
  const source = safeSource(from), query = new URLSearchParams();
  if (source !== '/tickets') query.set('from', source);
  return `/tickets/${encodeURIComponent(id)}` + (query.size ? `?${query}` : '');
}

export function editPath(id: string, from = '/tickets'): string {
  const detail = new URL(detailPath(id, from), routeOrigin);
  return `${detail.pathname}/edit${detail.search}`;
}

export function parseRoute(path: string): Route {
  let parts: string[], url: URL;
  try { url = new URL(path, routeOrigin); parts = url.pathname.split('/').filter(Boolean).map(decodeURIComponent); }
  catch { return { type: 'not-found' }; }
  if (!parts.length || parts.length === 1 && parts[0] === 'tickets') return { type: 'tickets' };
  if (parts.length === 1 && parts[0] === 'devices') return { type: 'devices' };
  if (parts.length === 2 && parts[0] === 'devices' && parts[1] === 'network') return { type: 'network' };
  if (url.pathname === '/mine') return { type: 'mine' };
  if (url.pathname === '/months') return { type: 'months' };
  if (url.pathname.startsWith('/months/') && parts.length === 2 && url.pathname.split('/').length === 3 && validMonth(parts[1])) return { type: 'month', month: parts[1] };
  if (parts[0] === 'tickets' && parts[1] === 'new' && parts.length === 2) return { type: 'new' };
  if (parts[0] === 'tickets' && parts.length === 2) return { type: 'detail', id: parts[1], from: sourceFrom(url) };
  if (parts[0] === 'tickets' && parts.length === 3 && parts[2] === 'edit') return { type: 'edit', id: parts[1], from: sourceFrom(url) };
  return { type: 'not-found' };
}

import { Books, Compass, Users, User } from '@phosphor-icons/react';
import type { Category } from '../data';
import { categories } from '../features/collections/presentation';

export type Tab = 'collection' | 'explore' | 'friends' | 'profile';
export type Route = { type: 'main'; tab: Tab } | { type: 'category'; category: Category }
  | { type: 'detail'; source: 'collection' | 'community' | 'shares'; id: string }
  | { type: 'add'; editId?: string } | { type: 'month'; month: string }
  | { type: 'notifications' } | { type: 'search'; scope: Tab } | { type: 'not-found' };
export const tabs = [
  { id: 'collection' as const, label: '我的收藏', icon: Books },
  { id: 'explore' as const, label: '探索', icon: Compass },
  { id: 'friends' as const, label: '朋友分享', icon: Users },
  { id: 'profile' as const, label: '我的', icon: User },
];
export function parseRoute(path: string): Route {
  const url = new URL(path, window.location.origin);
  let parts: string[];
  try { parts = url.pathname.split('/').filter(Boolean).map(decodeURIComponent); }
  catch { return { type: 'not-found' }; }
  if (!parts.length) return { type: 'main', tab: 'collection' };
  if (parts.length === 1 && tabs.some(tab => tab.id === parts[0])) return { type: 'main', tab: parts[0] as Tab };
  if (parts[0] === 'category' && parts.length === 2 && categories.some(category => category.id === parts[1])) return { type: 'category', category: parts[1] as Category };
  if (parts[0] === 'collection' && parts.length === 3 && parts[2] === 'edit') return { type: 'add', editId: parts[1] };
  if (['collection', 'community', 'shares'].includes(parts[0]) && parts.length === 2) return { type: 'detail', source: parts[0] as 'collection' | 'community' | 'shares', id: parts[1] };
  if (parts[0] === 'add' && parts.length === 1) return { type: 'add' };
  if (parts[0] === 'month' && parts.length === 2 && /^\d{4}-(0[1-9]|1[0-2])$/.test(parts[1])) return { type: 'month', month: parts[1] };
  if (parts[0] === 'notifications' && parts.length === 1) return { type: 'notifications' };
  if (parts[0] === 'search' && parts.length === 1) {
    const scope = url.searchParams.get('scope');
    return { type: 'search', scope: tabs.some(tab => tab.id === scope) ? scope as Tab : 'collection' };
  }
  return { type: 'not-found' };
}

import { useEffect, useState } from 'react';

const eventName = 'memoverse:navigate';
export function navigate(path: string, replace = false) {
  if (!path.startsWith('/') || path.startsWith('//') || path.includes('\\')) throw new Error('页面地址无效。');
  const depth = Number(window.history.state?.memoverseDepth ?? 0) + (replace ? 0 : 1);
  window.history[replace ? 'replaceState' : 'pushState']({ memoverseDepth: depth }, '', path);
  window.dispatchEvent(new Event(eventName));
}
export function useLocationPath() {
  const current = () => window.location.pathname + window.location.search;
  const [path, setPath] = useState(current);
  useEffect(() => {
    const update = () => setPath(current());
    window.addEventListener('popstate', update);
    window.addEventListener(eventName, update);
    return () => {
      window.removeEventListener('popstate', update);
      window.removeEventListener(eventName, update);
    };
  }, []);
  return path;
}

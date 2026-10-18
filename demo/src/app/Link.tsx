import type { MouseEvent, ReactNode } from 'react';
import { navigate } from '../navigation';

export default function Link({ to, children, className = '', onNavigate, current, disabled = false }: {
  to: string; children: ReactNode; className?: string; onNavigate?: () => void; current?: boolean; disabled?: boolean;
}) {
  const click = (event: MouseEvent<HTMLAnchorElement>) => {
    if (disabled) { event.preventDefault(); return; }
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    onNavigate?.();
    navigate(to);
  };
  return <a href={to} className={className} onClick={click} aria-current={current ? 'page' : undefined} aria-disabled={disabled || undefined}>{children}</a>;
}

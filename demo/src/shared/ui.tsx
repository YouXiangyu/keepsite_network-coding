import type { ReactNode } from 'react';
import { Books, CaretRight } from '@phosphor-icons/react';
import Link from '../app/Link';

export function IconButton({ label, children, onClick, className = '', disabled = false }: {
  label: string; children: ReactNode; onClick: () => void; className?: string; disabled?: boolean;
}) {
  return <button type="button" className={`icon-button ${className}`} aria-label={label} onClick={onClick} disabled={disabled}>
    {children}
  </button>;
}

export function Empty({ title = '这里还没有收藏', text = '留下一张照片，让喜欢的瞬间有处可寻。', children }: {
  title?: string; text?: string; children?: ReactNode;
}) {
  return <div className="empty-state">
    <Books size={38} weight="light" />
    <h2>{title}</h2>
    <p>{text}</p>
    {children}
  </div>;
}

export function PageHeading({ title, subtitle, action }: { title: string; subtitle?: string; action?: ReactNode }) {
  return <header className="page-heading">
    <div>
      <span className="eyebrow">YOUR LIFE, COLLECTED</span>
      <h1>{title}</h1>
      {subtitle && <p>{subtitle}</p>}
    </div>
    {action}
  </header>;
}

export function SectionHeading({ title, to, count }: { title: string; to?: string; count?: number }) {
  return <div className="section-heading">
    <h2>{title}{count !== undefined && <span>{count}</span>}</h2>
    {to && <Link to={to}>查看全部<CaretRight size={16} /></Link>}
  </div>;
}

import type { ReactNode } from 'react';
import { BookmarkSimple } from '@phosphor-icons/react';
import type { ShareItem } from '../../data';
import { Empty, PageHeading } from '../../shared/ui';
import PhotoCard from '../collections/PhotoCard';
import { dateLabel } from '../collections/presentation';

export default function SharesPage({ items, onlyUnread, unseen, busy, onUnreadChange, onView, onCollect, toolbar }: {
  items: ShareItem[];
  onlyUnread: boolean;
  unseen: number;
  busy: boolean;
  onUnreadChange: (onlyUnread: boolean) => void;
  onView: (id: string) => void;
  onCollect: (id: string) => void;
  toolbar: ReactNode;
}) {
  const shares = items.filter(item => !onlyUnread || item.unread);
  return <>
    {toolbar}
    <PageHeading title="朋友分享" subtitle="收到的每一份美好，都可以留在自己的收藏里。" />
    <div className="filter-tabs" role="group" aria-label="分享筛选">
      <button aria-pressed={!onlyUnread} className={!onlyUnread ? 'active' : ''} onClick={() => onUnreadChange(false)}>全部分享</button>
      <button aria-pressed={onlyUnread} className={onlyUnread ? 'active' : ''} onClick={() => onUnreadChange(true)}>
        未查看{unseen > 0 && <span className="count-pill">{unseen}</span>}
      </button>
    </div>
    {shares.length ? <div className="collection-grid friends-grid">
      {shares.map(item => <article className="friend-card" key={item.id}>
        <div className="sender-line">
          <span className="letter-avatar">{item.sender.slice(0, 1)}</span>
          <span><strong>{item.sender}</strong><small>{dateLabel(item.receivedAt)}</small></span>
          {item.unread && <span className="notice-label">未查看</span>}
        </div>
        <PhotoCard item={item.item} to={`/shares/${encodeURIComponent(item.id)}`} onNavigate={() => onView(item.id)} hero />
        <p className="share-note">{item.item.note}</p>
        <button className="text-button" disabled={busy} onClick={() => onCollect(item.id)}>
          <BookmarkSimple size={18} />收藏这份分享
        </button>
      </article>)}
    </div> : <Empty title="都已经看过了" text="下一份来自朋友的美好，正在路上。" />}
  </>;
}

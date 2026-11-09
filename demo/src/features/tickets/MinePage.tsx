import { CaretRight, Devices, Plus } from '@phosphor-icons/react';
import type { User } from '../../api';
import Link from '../../app/Link';
import { detailPath } from '../../app/routes';
import TicketImage from './TicketImage';
import { HOME_MONTH_LIMIT, MONTH_PREVIEW_LIMIT, type MonthCollection, type ticketCollections } from './collections';

export function MonthCards({ months }: { months: MonthCollection[] }) {
  return <div className="month-grid">{months.map(group => <Link key={group.month} to={`/months/${group.month}`} className="month-card">
    <div className="month-covers" aria-hidden="true">{group.tickets.slice(0, MONTH_PREVIEW_LIMIT).map(ticket => <div className="month-photo" key={ticket.id}><TicketImage retry={false} media={ticket.media.poster ?? ticket.media.original} title={ticket.fields.title} /></div>)}</div>
    <h3>{group.label}</h3><p>{group.tickets.length} 张</p>
  </Link>)}</div>;
}

export default function MinePage({ user, collection }: { user: User; collection: ReturnType<typeof ticketCollections> }) {
  const name = user.name.trim() || user.email.split('@')[0] || '当前账号';
  return <div className="mine-page"><header className="page-heading"><h1>我的</h1><Link to="/tickets/new" className="secondary-button"><Plus size={18} />录入电影票</Link></header>
    <section className="profile-summary" aria-label="当前账号"><div className="profile-avatar" aria-hidden="true">{Array.from(name)[0]}</div><div><h2>{name}</h2>{user.email && user.email !== name && <p>{user.email}</p>}</div></section>
    <dl className="profile-stats" aria-label="观影统计"><div><dt>电影票</dt><dd>{collection.tickets.length}<span> 张</span></dd></div><div><dt>观影月份</dt><dd>{collection.months.length}<span> 个</span></dd></div></dl>
    {collection.tickets.length ? <>
      <section className="mine-section" aria-labelledby="recent-title"><div className="section-heading"><h2 id="recent-title">最近添加</h2><Link to="/tickets" className="text-link">查看票夹<CaretRight size={16} /></Link></div>
        <ul className="recent-tickets" aria-label="最近添加的电影票">{collection.recent.map(ticket => <li key={ticket.id}><Link to={detailPath(ticket.id, '/mine')}><TicketImage retry={false} media={ticket.media.poster ?? ticket.media.original} title={ticket.fields.title} /><h3>{ticket.fields.title || '待补充片名'}</h3></Link></li>)}</ul>
      </section>
      <section className="mine-section" aria-labelledby="months-title"><div className="section-heading"><h2 id="months-title">按月回看</h2>{collection.months.length > HOME_MONTH_LIMIT && <Link to="/months" className="text-link">查看全部月份<CaretRight size={16} /></Link>}</div><MonthCards months={collection.months.slice(0, HOME_MONTH_LIMIT)} />
        {!!collection.undated.length && <p className="field-hint">{collection.undated.length} 张电影票日期不可用，可在<Link to="/tickets" className="text-link">票夹</Link>查看。</p>}
      </section>
    </> : <div className="empty-state"><h2>还没有电影票</h2><p>录入后，这里会显示最近添加和按月回看。</p><Link to="/tickets/new" className="primary-button">上传第一张电影票</Link></div>}
    <Link to="/devices" className="mine-settings"><Devices size={22} /><span>设备与状态</span><CaretRight size={18} /></Link>
  </div>;
}

import { CaretRight, Devices, DownloadSimple, SignOut } from '@phosphor-icons/react';
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

export default function MinePage({ user, collection, logoutBusy, onLogout }: { user: User; collection: ReturnType<typeof ticketCollections>; logoutBusy: boolean; onLogout: () => void }) {
  const name = user.name.trim() || user.email.split('@')[0] || '当前账号';
  return <div className="mine-page"><header className="page-heading"><h1>我的</h1></header>
    <section className="profile-summary" aria-label="当前账号"><div className="profile-avatar" aria-hidden="true">{Array.from(name)[0]}</div><div><h2>{name}</h2>{user.email && user.email !== name && <p>{user.email}</p>}</div></section>
    <p className="profile-stats">{collection.tickets.length} 张电影票 · {collection.months.length} 个观影月份</p>
    {collection.tickets.length ? <>
      <section className="mine-section" aria-labelledby="recent-title"><h2 id="recent-title" className="section-heading"><Link to="/tickets" className="section-link">最近添加<CaretRight size={18} /></Link></h2>
        <ul className="recent-tickets" aria-label="最近添加的电影票">{collection.recent.map(ticket => <li key={ticket.id}><Link to={detailPath(ticket.id, '/mine')}><TicketImage retry={false} media={ticket.media.poster ?? ticket.media.original} title={ticket.fields.title} /><h3>{ticket.fields.title || '待补充片名'}</h3></Link></li>)}</ul>
      </section>
      <section className="mine-section" aria-labelledby="months-title"><h2 id="months-title" className="section-heading">{collection.months.length > HOME_MONTH_LIMIT ? <Link to="/months" className="section-link">按月回看<CaretRight size={18} /></Link> : '按月回看'}</h2><MonthCards months={collection.months.slice(0, HOME_MONTH_LIMIT)} />
        {!!collection.undated.length && <p className="field-hint">{collection.undated.length} 张电影票日期不可用，可在<Link to="/tickets" className="text-link">票夹</Link>查看。</p>}
      </section>
    </> : <div className="empty-state"><h2>还没有电影票</h2><p>录入后，这里会显示最近添加和按月回看。</p><Link to="/tickets/new" className="primary-button">录入第一张电影票</Link></div>}
    <nav className="mine-settings" aria-label="设置">
      <Link to="/devices"><Devices size={22} /><span>账户与设备</span><CaretRight size={18} /></Link>
      <Link to="/download/android"><DownloadSimple size={22} /><span>下载 Android App</span><CaretRight size={18} /></Link>
      <button type="button" disabled={logoutBusy} onClick={onLogout}><SignOut size={22} /><span>{logoutBusy ? '正在退出…' : '退出登录'}</span></button>
    </nav>
  </div>;
}

import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowClockwise, CaretDown, FilmSlate, Devices, Plus, SignOut, Trash, PencilSimple, MagnifyingGlass } from '@phosphor-icons/react';
import { Brand } from './AuthGate';
import { errorMessage, mutate, signOut, type SignedInSession } from './api';
import { navigate, useLocationPath } from './navigation';
import { parseRoute } from './app/routes';
import Link from './app/Link';
import { useAccountWorkspace } from './app/useAccountWorkspace';
import WebDialog from './WebDialog';
import TicketEditor, { fieldLabels } from './features/tickets/TicketEditor';
import TicketImage from './features/tickets/TicketImage';
import TicketGrid from './features/tickets/TicketGrid';
import { fieldNames } from './data';
import { MediaProvider, CacheStatus } from './MediaProvider';
import ProcessingStatus from './features/tickets/ProcessingStatus';
import PosterAttribution from './features/tickets/PosterAttribution';
import DeviceStatus from './features/tickets/DeviceStatus';
import { processingLabels, sourceLabels } from './features/tickets/processing';

export default function Prototype({ session }: { session: SignedInSession }) {
  const path = useLocationPath();
  const route = parseRoute(path);
  const { data, ready, busy, capabilities, storageError, syncError, runMutation, runReconciliation, rebuilding, connected, eventPort, retry } = useAccountWorkspace(session);
  const tickets = data.tickets.filter(ticket => ticket.deletedAt === null);
  const item = 'id' in route ? tickets.find(ticket => ticket.id === route.id) : undefined;
  const [deleting, setDeleting] = useState(false);
  const [actionError, setActionError] = useState('');
  const [logoutBusy, setLogoutBusy] = useState(false);
  const [search, setSearch] = useState('');
  const [sort, setSort] = useState<'recent' | 'oldest'>('recent');
  const matchingTickets = tickets.filter(ticket => `${ticket.fields.title} ${ticket.fields.cinema}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
    .sort((a, b) => sort === 'recent' ? b.createdAt - a.createdAt : a.createdAt - b.createdAt);
  const deleteOperation = useRef({ id: '', operationId: '' });
  const alive = useRef(true);
  const heading = useRef<HTMLDivElement>(null);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { setDeleting(false); setActionError(''); window.scrollTo(0, 0); heading.current?.focus({ preventScroll: true }); }, [path]);
  useEffect(() => { document.title = `${item?.fields.title || (route.type === 'devices' ? '设备与保存状态' : '我的票夹')} · MemoVerse`; }, [item?.fields.title, route.type]);
  const logout = async () => { setLogoutBusy(true); try { await signOut(window.location.origin); } catch (error) { if (alive.current) { setActionError(errorMessage(error)); setLogoutBusy(false); } } };
  const remove = () => {
    if (!item) return;
    if (deleteOperation.current.id !== item.id) deleteOperation.current = { id: item.id, operationId: crypto.randomUUID() };
    void runMutation(() => mutate(`/api/tickets/${encodeURIComponent(item.id)}`, 'DELETE', { operationId: deleteOperation.current.operationId }), {
      onStart: () => setActionError(''), onError: error => setActionError(errorMessage(error)),
      onSuccess: () => { setDeleting(false); navigate('/tickets'); },
    });
  };
  const back = <Link to="/tickets" className="back-link"><ArrowLeft size={18} />返回票夹</Link>;
  let content;
  if (route.type === 'tickets') content = <><header className="page-heading collection-heading"><div className="collection-title"><div><span className="eyebrow">YOUR FILM JOURNAL</span><h1><span className="desktop-collection-title">我的票夹</span><span className="mobile-collection-title">电影</span></h1><p>留住每一次走进电影院的时刻。</p></div><span className="collection-count" aria-label={`${tickets.length} 张电影票`}>{tickets.length}件</span></div><Link to="/tickets/new" className="primary-button collection-upload"><Plus size={20} />上传电影票</Link></header>
    <div className="collection-tools"><label className="collection-sort"><span className="visually-hidden">电影票排序</span><select value={sort} onChange={event => setSort(event.target.value === 'oldest' ? 'oldest' : 'recent')}><option value="recent">最近添加</option><option value="oldest">最早添加</option></select><CaretDown size={15} aria-hidden="true" /></label><button className="icon-button" aria-label="刷新票夹" onClick={retry} disabled={busy}><ArrowClockwise size={19} /></button></div>
    <label className="collection-search"><MagnifyingGlass size={22} /><span className="visually-hidden">搜索片名或影院</span><input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索片名或影院" /></label>
    {tickets.length ? matchingTickets.length ? <TicketGrid>{matchingTickets.map(ticket => <article className="ticket-card" key={ticket.id}>
      <Link to={`/tickets/${encodeURIComponent(ticket.id)}`}><TicketImage retry={false} media={ticket.media.poster ?? ticket.media.original} title={ticket.fields.title} /><div className="ticket-card-copy"><h2>{ticket.fields.title || '待补充片名'}</h2><p className="ticket-cinema">{ticket.fields.cinema || '影院未填写'}</p><p className="ticket-date">{ticket.fields.showtime ? <time dateTime={ticket.fields.showtime.slice(0, 10)}>{ticket.fields.showtime.slice(0, 10).replace(/-/g, '.')}</time> : '日期待补充'}</p>{!['not_started', 'ready'].includes(ticket.processing.status) && <span className="status-pill" data-state={ticket.processing.status}>{processingLabels[ticket.processing.status]}</span>}{ticket.media.original.cloudStatus !== 'saved' && <p className="ticket-cloud-warning">原票尚未云存</p>}{ticket.media.poster && ticket.media.poster.cloudStatus !== 'saved' && <p className="ticket-cloud-warning">海报尚未云存</p>}</div></Link>
    </article>)}</TicketGrid> : <div className="empty-state" role="status"><h2>没有找到匹配的电影票</h2><p>试试其他片名或影院。</p><button className="secondary-button" onClick={() => setSearch('')}>清除搜索</button></div> : <div className="empty-state"><FilmSlate size={48} weight="light" /><h2>你的第一张电影票</h2><p>上传一张原票照片，开始整理自己的观影记录。</p><Link to="/tickets/new" className="primary-button">上传第一张电影票</Link></div>}</>;
  else if (route.type === 'new' || route.type === 'edit') content = <>{back}<TicketEditor key={path.split('?')[0]} item={item} editId={route.type === 'edit' ? route.id : undefined} busy={busy} runMutation={runMutation} runReconciliation={runReconciliation} processingEnabled={!!capabilities?.processing} processingUnavailableReason={capabilities?.processingUnavailableReason ?? null} onSaved={id => navigate(`/tickets/${encodeURIComponent(id)}`)} /></>;
  else if (route.type === 'detail' && item) content = <>{back}<div className="detail-layout"><div><TicketImage media={item.media.poster ?? item.media.original} title={item.fields.title} />{item.media.poster && <PosterAttribution source={item.media.poster.source} />}{item.media.poster && <details className="original-photo"><summary>查看原票照片</summary><TicketImage media={item.media.original} title="原票照片" /></details>}</div>
    <section className="detail-information"><span className="eyebrow">MOVIE TICKET</span><h1>{item.fields.title || '待补充片名'}</h1><dl className="ticket-facts">{fieldNames.map(key => <div key={key}><dt>{fieldLabels[key]}</dt><dd>{item.fields[key].replace('T', ' ') || '未填写'}<small>{sourceLabels[item.fieldSources[key]]}</small></dd></div>)}</dl>
    <div className="status-panel"><h2>保存状态</h2><p>{item.media.original.cloudStatus === 'saved' ? '原票已保存到云端' : '原票已保存到当前服务器，尚未云存'}</p><p>{item.media.poster ? item.media.poster.cloudStatus === 'saved' ? '海报已保存到云端' : '海报尚未云存' : '暂无海报，正在显示原票'}</p><p>本机可用情况以图片下方的校验与缓存状态为准。</p></div>
    <ProcessingStatus key={item.id} ticket={item} capabilities={capabilities} busy={busy} runMutation={runMutation} runReconciliation={runReconciliation} />
    <div className="action-row"><Link to={`/tickets/${encodeURIComponent(item.id)}/edit`} className="primary-button" disabled={busy}><PencilSimple size={18} />编辑票根</Link><button className="secondary-button" disabled={busy} onClick={() => setDeleting(true)}><Trash size={18} />删除票根</button></div></section></div></>;
  else if (route.type === 'devices') content = <><header className="page-heading"><div><span className="eyebrow">STORAGE & DEVICES</span><h1>设备与保存状态</h1><p>区分云端保存、处理进度与这台设备上的可用情况。</p></div></header><div className="device-panels"><section className="status-panel"><h2>云端保存</h2><p>{capabilities?.cloudStorage ? '已配置私有云存储；请以每张票根的保存状态为准。' : '当前服务器未配置云存储。当前保存不具备云端恢复保障。'}</p><p>上传完成并确认票根记录后，才会更新保存状态。</p><p>自动识别：{capabilities?.processing ? '配置已就绪' : '配置未就绪'}</p>{!capabilities?.processing && capabilities?.processingUnavailableReason && <p>{capabilities.processingUnavailableReason}</p>}</section><section className="status-panel"><CacheStatus /></section><section className="status-panel device-peer-panel"><DeviceStatus /><p>{connected ? '状态提示已连接，变化通过增量同步更新。' : '状态连接恢复中，正在通过增量请求核对。'}</p></section></div></>;
  else content = <>{back}<div className="empty-state"><h1>票根或页面不存在</h1><p>记录可能已被删除，请返回票夹查看。</p></div></>;
  return <MediaProvider accountId={session.user.id} ready={ready} tickets={data.tickets} eventPort={eventPort} deviceSync={!!capabilities?.deviceSync}><div className="memoverse-shell" data-authenticated="true" data-user-id={session.user.id}><a className="skip-link" href="#main-content">跳到主要内容</a>
    <header className="app-header"><Link to="/tickets"><div className="desktop-brand"><Brand /></div><span className="mobile-brand">我的票夹</span></Link><nav aria-label="主导航"><Link to="/tickets" current={route.type !== 'devices' && route.type !== 'new'}><FilmSlate size={24} weight={route.type !== 'devices' && route.type !== 'new' ? 'fill' : 'regular'} />票夹</Link><Link to="/tickets/new" className="mobile-upload" current={route.type === 'new'}><Plus size={26} /><span className="visually-hidden">上传电影票</span></Link><Link to="/devices" current={route.type === 'devices'}><Devices size={24} /><span className="visually-hidden">设备与状态</span><span className="desktop-nav-label" aria-hidden="true">设备与状态</span><span className="mobile-nav-label" aria-hidden="true">设备</span></Link></nav><div className="account-actions"><span>{session.user.email}</span><button className="icon-button" aria-label="退出登录" disabled={logoutBusy} onClick={() => void logout()}><SignOut size={22} /></button></div></header>
    <main className="workspace" id="main-content" aria-busy={busy}><div ref={heading} tabIndex={-1} className="page-focus-anchor" />
      {(storageError || syncError) && <div className="form-error" role="alert">{storageError || syncError}<button className="text-button" onClick={retry} disabled={busy}>重试</button></div>}
      {actionError && !deleting && <p className="form-error" role="alert">{actionError}</p>}
      {rebuilding && ready && <p className="status-note" role="status">正在重新校准票夹，当前页面和草稿仍保留。</p>}
      {ready ? content : <p role="status">正在读取票夹…</p>}
    </main><WebDialog title="删除这张电影票？" open={deleting} onOpenChange={value => { if (!busy) setDeleting(value); }}><p>删除后将撤销后续读取权限。云端对象按保留策略处理。</p>{actionError && <p className="form-error" role="alert">{actionError}</p>}<div className="action-row"><button className="danger-button" disabled={busy} onClick={remove}>{busy ? '正在删除…' : '确认删除'}</button><button className="secondary-button" disabled={busy} onClick={() => setDeleting(false)}>取消</button></div></WebDialog>
  </div></MediaProvider>;
}

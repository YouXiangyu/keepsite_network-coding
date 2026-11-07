import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react';
import Link from '../../app/Link';
import { usePeerStatus } from '../../MediaProvider';
import { httpActivity, networkMonitor, socketActivity } from '../../network/runtime';
import { MonitorViewActions, serverTraceClient } from '../../network/serverTraceClient';
import type { MonitorSnapshot } from '../../network/monitor';
import { protocols, statuses } from '../../network/schema';
import { ReportDownload, safePeerView, type ReportFormat } from '../../network/report';
import { bytes, count, emptyFilter, eventClock, eventGroup, filterEvents, instant, label, milliseconds, relatedEvents, requestSamples, timeFilterInvalid, yesNo, type EventFilter } from '../../network/presentation';
import Timeline from './Timeline';
import './network.css';

function subscribe(listener: () => void) {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const stop = networkMonitor.subscribe(() => { if (!timer) timer = setTimeout(() => { timer = undefined; listener(); }, 250); });
  return () => { stop(); if (timer) clearTimeout(timer); };
}
function Facts({ rows }: { rows: readonly (readonly [string, ReactNode])[] }) {
  return <dl className="nm-facts">{rows.map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value}</dd></div>)}</dl>;
}
function CardFacts({ rows, primary }: { rows: readonly (readonly [string, ReactNode])[]; primary: readonly string[] }) {
  return <><Facts rows={rows.filter(([name]) => primary.includes(name))} /><details className="nm-card-details"><summary>全部观测</summary><Facts rows={rows} /></details></>;
}
function Overview({ snapshot, wanted }: { snapshot: MonitorSnapshot; wanted: boolean }) {
  const { api, websocket: ws, sync, peer, transfer } = snapshot.cards;
  return <><div className="nm-cards">
    <section className="status-panel"><span className="eyebrow">API</span><h2>{label(api.state)}</h2><p className="nm-muted nm-wrap">{window.location.origin} · 同源 {window.location.protocol}</p><CardFacts primary={["最近HTTP","请求耗时","在途"]} rows={[
      ['最近HTTP', count(api.status)], ['响应头', milliseconds(api.headersMs)], ['请求耗时', milliseconds(api.durationMs)], ['在途', count(api.active)],
      ['最近成功', instant(api.lastSuccessAt)], ['最近失败', instant(api.lastFailureAt)], ['认证', label(api.auth)], ['结果码', api.errorCode ?? '暂无']]} /></section>
    <section className="status-panel"><span className="eyebrow">WEBSOCKET</span><h2>{label(ws.state)}</h2><CardFacts primary={["连接持续（当前观测）","最后消息","消息类型"]} rows={[
      ['连接始于', instant(ws.connectedSince)], ['连接持续（当前观测）', milliseconds(socketActivity().connectedDurationMs)], ['最后消息', instant(ws.lastMessageAt)], ['消息类型', label(ws.messageType)], ['观测间隔', milliseconds(ws.messageIntervalMs)],
      ['心跳配置', milliseconds(ws.heartbeatConfiguredMs)], ['初始超时', milliseconds(ws.initialTimeoutMs)], ['看门狗', milliseconds(ws.watchdogMs)], ['重试等待', milliseconds(ws.retryDelayMs)], ['重试时点', instant(ws.retryAt)], ['关闭码', count(ws.closeCode)], ['结果码', ws.errorCode ?? '暂无']]} /></section>
    <section className="status-panel"><span className="eyebrow">DATA SYNC</span><h2>{label(sync.state)}</h2><CardFacts primary={["本机cursor","观测水位","待追赶"]} rows={[
      ['本机cursor', count(sync.localCursor)], ['观测水位', count(sync.serverCursor)], ['待追赶', yesNo(sync.pending)], ['最后初始应用', instant(sync.lastSnapshotAt)], ['最后增量应用', instant(sync.lastDeltaAt)],
      ['最近应用耗时', milliseconds(sync.lastDurationMs)], ['最近批次条目', count(sync.lastCount)], ['重试等待', milliseconds(sync.retryDelayMs)], ['结果码', sync.errorCode ?? '暂无']]} /></section>
    <section className="status-panel"><span className="eyebrow">PEER</span><h2>{label(peer.state)}</h2><CardFacts primary={["用户开关","已登记","接收 / 发送"]} rows={[
      ['用户开关', yesNo(wanted)], ['服务支持', yesNo(peer.configured)], ['已登记', yesNo(peer.registered)], ['发现设备', count(peer.peers)], ['接收 / 发送', `${count(peer.receiving)} / ${count(peer.sending)}`],
      ['STUN / TURN配置', `${yesNo(peer.stunConfigured)} / ${yesNo(peer.turnConfigured)}`], ['ICE / DC', `${label(peer.iceState)} / ${label(peer.channelState)}`], ['RTC RTT', milliseconds(peer.roundTripMs)], ['结果码', peer.errorCode ?? '暂无']]} /></section>
    <section className="status-panel"><span className="eyebrow">LAST FILE OBSERVATION</span><h2>{label(transfer.route)}</h2><p>{label(transfer.state)} · {label(transfer.direction)}</p><CardFacts primary={["累计应用读取","完整校验字节","本机校验"]} rows={[
      ['累计应用读取', bytes(transfer.bytes)], ['预期逻辑大小', bytes(transfer.expectedBytes)], ['完整校验字节', bytes(transfer.verifiedBytes)], ['本机校验', yesNo(transfer.verified)],
      ['控制 / 丢弃', `${bytes(transfer.controlBytes)} / ${bytes(transfer.discardedBytes)}`], ['耗时', milliseconds(transfer.durationMs)], ['应用速率', transfer.speedBytesPerSecond === null || transfer.speedBytesPerSecond === undefined ? '未知' : `${bytes(transfer.speedBytesPerSecond)}/s`], ['结果码', transfer.errorCode ?? '暂无']]} /></section>
  </div><section className="status-panel nm-diagram"><h2>三条基础链路</h2><p className="nm-muted">角色示意；当前连接与实际文件路径见状态与传输观测。</p>
    <ol><li><strong>{window.location.protocol === 'https:' ? 'WSS' : 'WS'} 控制</strong><span>本机 ⇄ 服务器 ⇄ 同账号设备</span></li><li><strong>RTCDataChannel 文件</strong><span>本机 ⇄ 对端设备（直连 / 配置中继）</span></li><li><strong>云端读取</strong><span>本机 ← 服务器代理 ← 私有S3，或授权后S3直读</span></li></ol></section></>;
}
function Connections({ snapshot, peer, devices }: { snapshot: MonitorSnapshot; peer: ReturnType<typeof safePeerView>; devices: ReturnType<typeof usePeerStatus>['deviceLabels'] }) {
  const http = httpActivity();
  return <div className="nm-stack"><section className="status-panel"><h2>当前活动HTTP · {http.total}</h2><p className="nm-muted">{http.omitted} 条未列出 · 开始点使用本机单调时钟</p>
    {http.requests.length ? <ul className="nm-list">{http.requests.map(request => <li key={request.alias}><strong>{request.alias} · {request.template}</strong><span>{label(request.phase)} · HTTP {count(request.status)} · 开始 {milliseconds(request.startedMs)}</span></li>)}</ul> : <p>暂无列出的活动请求。</p>}</section>
    <section className="status-panel"><h2>本人在线页面</h2>{devices.length ? <ul className="nm-list">{devices.map((device, index) => <li key={index}><strong>{device.name}{device.self ? '（本页）' : ''}</strong><span>{device.alias ?? '暂无传输别名'}{device.channels.length ? ` · ${device.channels.join(' / ')}` : ''}</span></li>)}</ul> : <p>暂无已发现页面。</p>}</section>
    <section className="status-panel"><h2>当前连接</h2><Facts rows={[
      ['WS', label(snapshot.cards.websocket.state)], ['同步', label(snapshot.cards.sync.state)], ['Peer登记', label(snapshot.cards.peer.state)], ['通道 / 接收 / 发送', `${peer.active.length} / ${count(peer.receiving)} / ${count(peer.sending)}`]]} />
      {peer.active.length ? <div className="nm-active-grid">{peer.active.map(item => <article key={item.alias} className="nm-active"><h3>{item.alias} · {label(item.direction)}</h3><Facts rows={[
        ['设备', devices.find(device => device.channels.includes(item.alias))?.name ?? '名称暂无观测'], ['传输设备别名', item.deviceAlias ?? '未知'], ['实际路径', label(item.route)], ['PC / ICE', `${label(item.pcState)} / ${label(item.iceState)}`], ['DTLS / DC', `${label(item.dtlsState)} / ${label(item.channelState)}`],
        ['候选类型', `${label(item.localType)} / ${label(item.remoteType)}`], ['RTC RTT', milliseconds(item.roundTripMs)], ['载荷', bytes(item.payloadBytes)], ['控制', bytes(item.controlBytes)], ['丢弃子集', bytes(item.discardedBytes)],
        ['帧 / ACK进度', `${bytes(item.progressBytes)} / ${bytes(item.expectedBytes)}`], ['续传起始前缀', bytes(item.resumeOffset)], ['本次实例耗时', milliseconds(item.durationMs)], ['本次实例应用速率', item.payloadBytesPerSecond === null ? '未知' : `${bytes(item.payloadBytesPerSecond)}/s`], ['本地DC排队', bytes(item.bufferedAmount)],
        ['预期逻辑大小', bytes(item.expectedBytes)], ['本机校验字节', bytes(item.localVerifiedBytes)], ['源端确认', yesNo(item.sourceConfirmed)], ['DC发送 / 接收', `${bytes(item.channelBytesSent)} / ${bytes(item.channelBytesReceived)}`]]} /></article>)}</div> : <p>暂无活动文件通道。</p>}</section>
    <section className="status-panel"><h2>近期传输观测</h2><p className="nm-muted">最多50条 · 各次尝试及结果，失败前缀不代表成功交付</p>
      {peer.recent.length ? <ol className="nm-list">{peer.recent.map((item, index) => <li key={index}><strong>{label(item.direction)} · {label(item.path)} · {label(item.status)}</strong><span>路径 {label(item.route)} · 应用字节 {bytes(item.bytes)} · {milliseconds(item.milliseconds)}</span><span>控制 {bytes(item.controlBytes)} · 丢弃 {bytes(item.discardedBytes)} · 已保留前缀 {bytes(item.resumeOffset)}</span></li>)}</ol> : <p>暂无近期传输。</p>}</section></div>;
}
function Events({ snapshot }: { snapshot: MonitorSnapshot }) {
  const [filter, setFilter] = useState<EventFilter>({ ...emptyFilter }); const [expanded, setExpanded] = useState<string | null>(null);
  const visible = filterEvents(snapshot.events, filter), groups = new Map<string, typeof visible>();
  for (const event of visible) { const key = eventGroup(event, eventClock(event, snapshot.clockId)); groups.set(key, [...(groups.get(key) ?? []), event]); }
  const devices = [...new Set(snapshot.events.flatMap(event => event.origin === 'client' && event.deviceId ? [event.deviceId] : []))];
  const operations = [...new Set(snapshot.events.flatMap(event => event.origin === 'client' ? event.operationId ? [event.operationId] : [] : [event.spanId]))];
  const change = (key: keyof EventFilter, value: string) => { setFilter(current => ({ ...current, [key]: value })); setExpanded(null); };
  return <section className="status-panel"><h2>事件与Trace</h2><div className="nm-filters">
    <label>协议<select value={filter.protocol} onChange={e => change('protocol', e.target.value)}><option value="">全部协议</option>{protocols.map(protocol => <option key={protocol}>{protocol}</option>)}</select></label>
    <label>状态<select value={filter.status} onChange={e => change('status', e.target.value)}><option value="">全部状态</option>{statuses.map(status => <option key={status} value={status}>{label(status)}</option>)}</select></label>
    <label>设备别名<select value={filter.device} onChange={e => change('device', e.target.value)}><option value="">全部设备</option>{devices.map(device => <option key={device}>{device}</option>)}</select></label>
    <label>操作 / span<select value={filter.operation} onChange={e => change('operation', e.target.value)}><option value="">全部操作</option>{operations.map(operation => <option key={operation}>{operation}</option>)}</select></label>
    <label>各clock起始offset（ms）<input inputMode="decimal" value={filter.from} onChange={e => change('from', e.target.value)} maxLength={24} /></label>
    <label>各clock结束offset（ms）<input inputMode="decimal" value={filter.to} onChange={e => change('to', e.target.value)} maxLength={24} /></label>
    <button className="secondary-button" onClick={() => { setFilter({ ...emptyFilter }); setExpanded(null); }}>重置筛选</button>
  </div>{timeFilterInvalid(filter) && <p role="status">offset需填写非负数或留空。</p>}<p className="nm-muted">{visible.length} / {snapshot.events.length} 条保留事件 · 各clock独立，不比较跨时钟时长</p>
    {groups.size ? <div className="nm-groups">{[...groups].map(([key, items]) => { const first = items[0]; return <section key={key} className="nm-group"><button className="nm-group-button" aria-expanded={expanded === key} onClick={() => setExpanded(current => current === key ? null : key)}><strong>{first.event} · {items.length} 条</strong><span>{key.startsWith('trace:') ? `Trace ${key.slice(6)}` : first.origin === 'server' && first.coalesced ? '合并 · 未关联' : first.origin === 'client' && first.operationId ? first.operationId : '未关联操作'}</span><span>{expanded === key ? '收起' : '展开操作'}</span></button>{expanded === key && <><p className="nm-muted">完整已知关联链 · {relatedEvents(snapshot.events, first, snapshot.clockId).length} 条（含筛选外关联事件）</p><Timeline events={relatedEvents(snapshot.events, first, snapshot.clockId)} clockId={snapshot.clockId} /></>}</section>; })}</div> : <p role="status">暂无匹配事件。</p>}
  </section>;
}
function MonitorPage({ snapshot }: { snapshot: MonitorSnapshot }) {
  const peerStatus = usePeerStatus(250), peer = safePeerView(peerStatus);
  const [view, setView] = useState<'overview' | 'connections' | 'events'>('overview');
  const [message, setMessage] = useState(''); const [format, setFormat] = useState<ReportFormat>('json');
  const [clearing, setClearing] = useState(false);
  const [, setControlRevision] = useState(0);
  const actions = useMemo(() => new MonitorViewActions(networkMonitor, serverTraceClient, snapshot.sessionId, setClearing), [snapshot.sessionId]);
  const controls = actions.valid() ? networkMonitor.snapshot() : snapshot;
  const download = useMemo(() => new ReportDownload(networkMonitor, undefined, snapshot.sessionId), [snapshot.sessionId]);
  const actionOwner = useRef(0);
  useEffect(() => { actions.activate(); return () => { actionOwner.current++; download.dispose(); actions.dispose(); }; }, [download, actions]);
  const history = networkMonitor.exportLease()?.history;
  useEffect(() => { setMessage(''); }, [history]);
  const action = async (kind: 'refresh' | 'clear') => {
    if (!actions.canRun()) return;
    const owner = ++actionOwner.current; setMessage('');
    const pending = actions.run(kind), lease = networkMonitor.exportLease();
    const result = await pending;
    if (result !== null && actions.valid() && owner === actionOwner.current && networkMonitor.validExport(lease)) setMessage(kind === 'clear' ? result === 'ready' ? '历史已清空。' : `本机历史已清空；服务器清理${label(result)}。` : `服务器Trace：${label(result)}`);
  };
  const samples = requestSamples(snapshot);
  return <div className="network-monitor"><Link to="/devices" className="back-link">← 返回设备与状态</Link><header className="page-heading"><div><span className="eyebrow">NETWORK MONITOR</span><h1>网络监控</h1><p>当前轻状态与最近观测</p></div></header>
    <details className="status-panel nm-toolbar"><summary className="nm-tools-summary">采集与报告<span className="nm-muted">{controls.detailed ? controls.paused ? '详细已暂停' : '详细采集中' : '详细关闭'} · 服务端 {label(snapshot.server.state)}</span></summary><div className="nm-toolbar-content"><div className="nm-actions">
      <label className="nm-toggle"><input type="checkbox" checked={controls.detailed} onChange={e => { actions.setDetailed(e.target.checked); setControlRevision(value => value + 1); }} />详细Trace</label>
      <button className="secondary-button" disabled={!controls.detailed} onClick={() => { actions.togglePaused(); setControlRevision(value => value + 1); }}>{controls.paused ? '继续采集' : '暂停采集'}</button>
      <button className="secondary-button" disabled={clearing} onClick={() => void action('clear')}>{clearing ? '正在清理…' : '清空历史'}</button>
      <button className="secondary-button" disabled={clearing || !controls.detailed || controls.paused || snapshot.server.state === 'loading'} onClick={() => void action('refresh')}>读取服务端Trace</button>
    </div><p className="nm-muted">{controls.detailed ? controls.paused ? '详细采集已暂停' : '详细采集中' : '详细采集关闭'} · 轻状态持续更新 · 服务端 {label(snapshot.server.state)}</p>
    <div className="nm-actions"><label>报告格式<select value={format} onChange={e => setFormat(e.target.value as ReportFormat)}><option value="json">JSON安全快照</option><option value="jsonl">JSONL逐事件</option><option value="text">可读文本报告</option></select></label><button className="secondary-button" onClick={() => { if (!actions.valid()) return; setMessage(download.download(format, { protocol: window.location.protocol, rtcSupported: typeof RTCPeerConnection !== 'undefined', socket: socketActivity(), http: httpActivity(), peer: peerStatus }) ? '报告下载已发起。' : '捕获已变化，请重新导出。'); }}>导出报告</button></div>
    {message && <p role="status">{message}</p>}</div></details>
    <nav className="nm-tabs" aria-label="网络监控视图">{([['overview', '总览'], ['connections', '连接与传输'], ['events', '事件Trace']] as const).map(([key, text]) => <button key={key} type="button" aria-current={view === key ? 'page' : undefined} onClick={() => setView(key)}>{text}</button>)}</nav>
    {view === 'overview' ? <Overview snapshot={snapshot} wanted={peerStatus.wanted} /> : view === 'connections' ? <Connections snapshot={snapshot} peer={peer} devices={peerStatus.deviceLabels} /> : <Events key={history} snapshot={snapshot} />}
    <details className="status-panel nm-measurements"><summary>容量与测量范围</summary><Facts rows={[
      ['当前保留', `${snapshot.events.length} / ${snapshot.limits.events} · ${bytes(snapshot.metrics.retainedBytes)} / ${bytes(snapshot.limits.bytes)}`], ['本机账号累计记录', count(snapshot.metrics.recorded)], ['本机账号累计丢弃', count(snapshot.metrics.dropped)], ['本机账号累计拒绝', count(snapshot.metrics.rejected)],
      ['collector采集计时', milliseconds(snapshot.metrics.captureCostMs)], ['本机成功请求P50', `${milliseconds(samples.p50Ms)} · n=${samples.n}`], ['服务器clock已见', count(snapshot.metrics.serverClocks)],
      ['服务器当前capture累计', snapshot.server.source ? `${snapshot.server.source.metrics.recorded} 记录 / ${snapshot.server.source.metrics.dropped} 丢弃 / ${milliseconds(snapshot.server.source.metrics.captureCostMs)}` : '暂无观测']]} />
      <p>本机计数按本账号累计，清空保留；服务器按当前capture/clock另计。父media累计读取、attempt、HTTP和服务器IO可能重叠，不相加。完整校验字节表示已验证的逻辑文件大小。详细范围、clock与缺测随报告导出。</p></details>
  </div>;
}
export default function NetworkMonitorPage() {
  const snapshot = useSyncExternalStore(subscribe, networkMonitor.snapshot);
  return <MonitorPage key={snapshot.sessionId} snapshot={snapshot} />;
}

import { usePeerStatus } from '../../MediaProvider';

const size = (bytes: number) => bytes < 1024 ? `${bytes} B` : bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KiB` : `${(bytes / 1024 / 1024).toFixed(2)} MiB`;
const paths = { peer: '本人页面传输', s3: 'S3 直读', server: '服务器读取', cache: '本机缓存' };
const routes = { direct: '直连', relay: 'TURN 中继', unknown: '路径未确定', mixed: '路径发生变化' };
const statuses = { active: '传输中', complete: '已完成', failed: '未完成', cancelled: '已取消' };
export default function DeviceStatus() {
  const state = usePeerStatus();
  return <>
    <h2>本人在线页面</h2>
    <label className="peer-toggle"><input type="checkbox" checked={state.wanted} onChange={event => state.setWanted(event.target.checked)} />本页启用 P2P 加速</label>
    <p role="status" data-testid="peer-status" data-enabled={state.enabled}>{state.reason}</p>
    <p>每个在线页面独立登记；刷新或重连后重新登记，不代表持久安装的设备。</p>
    <p data-testid="peer-inventory" data-count={state.inventoryCount}>本页可分享 {state.inventoryCount} 张已缓存图片；接收 {state.receiving} / 发送 {state.sending}。</p>
    <button className="text-button" disabled={!state.enabled} onClick={() => void state.refresh()}>刷新在线页面</button>
    <ul className="peer-list">{state.peers.map(peer => <li key={peer.peerId}><span className="peer-name">{peer.name}{peer.peerId === state.peerId ? '（本页）' : ''}</span><span className="peer-online">在线</span></li>)}</ul>
    {state.enabled && state.peers.filter(peer => peer.peerId !== state.peerId).length === 0 && <p>当前没有其他可用页面，缺少的图片从云端获取。</p>}
    <p className="field-hint">当前仅支持可直接连接的在线页面；跨网络连接失败时改从云端读取。尚未启用中继服务。页面进入后台会暂停设备传输。</p>
    <h3>本页传输观测</h3>
    <dl className="peer-stat-grid">
      <div><dt>本机缓存读取</dt><dd>{size(state.totals.cache)}</dd></div><div><dt>P2P 数据</dt><dd>{size(state.totals.peer)}</dd></div>
      <div><dt>已确认直连</dt><dd>{size(state.totals.direct)}</dd></div><div><dt>TURN / 未确定 / 变化</dt><dd>{size(state.totals.relay)} / {size(state.totals.unknown)} / {size(state.totals.mixed)}</dd></div>
      <div><dt>S3 直读</dt><dd>{size(state.totals.s3)}</dd></div><div><dt>服务器读取</dt><dd>{size(state.totals.server)}</dd></div>
      <div><dt>P2P 控制数据</dt><dd>{size(state.totals.control)}</dd></div><div><dt>丢弃数据</dt><dd>{size(state.totals.discarded)}</dd></div>
    </dl>
    <p className="field-hint">字节为本页应用层实际收发，不含网络协议开销；路径根据连接统计确认。仅保留本页最近 50 次，不保存地址或凭据。</p>
    <ol className="transfer-list">{state.recent.map(record => <li key={record.id} data-testid="peer-transfer" data-direction={record.direction} data-path={record.path} data-route={record.route} data-status={record.status} data-bytes={record.bytes} data-control-bytes={record.controlBytes} data-discarded-bytes={record.discardedBytes} data-discarded-prefix={record.discardedPrefixBytes ?? 0}
      data-timing={JSON.stringify({ milliseconds: record.milliseconds, discoveryMs: record.discoveryMs, authorizationMs: record.authorizationMs, connectionMs: record.connectionMs, firstByteMs: record.firstByteMs, verificationMs: record.verificationMs, sourceReadMs: record.sourceReadMs, resumeOffset: record.resumeOffset })}>
      <strong>{record.direction === 'send' ? '发送' : '接收'} · {paths[record.path]}{record.route ? ` · ${routes[record.route]}` : ''}</strong>
      <span>{statuses[record.status]} · {size(record.bytes)} · {(record.milliseconds / 1000).toFixed(2)} 秒</span>
      {record.reason && <small>{record.reason}</small>}
    </li>)}</ol>
  </>;
}

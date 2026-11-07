import { NetworkMonitor, type ExportLease } from './monitor';
import { LIMITS, hexId, member, number, own, sanitizeCard, sanitizeEvent, templates, type Card } from './schema';
import { sanitizeServerEvent, sanitizeServerState } from './serverTrace';
import { activePeerProjection } from './peerRuntime';
import { bytes, eventInterval, label, milliseconds, requestSamples } from './presentation';

export type ReportFormat = 'json' | 'jsonl' | 'text';
const safeNumber = (value: unknown) => number(value) ? value : null;
function array(input: unknown, limit: number): unknown[] {
  try { if (!Array.isArray(input)) return []; } catch { return []; }
  const length = own(input, 'length'); if (!number(length, true)) return [];
  return Array.from({ length: Math.min(length, limit) }, (_, i) => own(input, String(i)));
}
export function safeHttpActivity(input: unknown) {
  const requests = array(own(input, 'requests'), 32).flatMap(item => {
    const alias = own(item, 'alias'), template = own(item, 'template'), phase = own(item, 'phase'), status = own(item, 'status');
    if (typeof alias !== 'string' || !/^request-[1-9][0-9]{0,14}$/.test(alias) || !member(template, templates) || !member(phase, ['requesting', 'headers'])) return [];
    return [{ alias, template, phase, startedMs: safeNumber(own(item, 'startedMs')), status: number(status, true) && status >= 100 && status <= 599 ? status : null }];
  });
  const total = own(input, 'total');
  return { total: number(total, true) ? total : null, omitted: number(total, true) ? Math.max(0, total - requests.length) : null, requests };
}
export function safePeerView(input: unknown) {
  const recent = array(own(input, 'recent'), 50).flatMap(item => {
    const direction = own(item, 'direction'), path = own(item, 'path'), status = own(item, 'status'), route = own(item, 'route');
    if (!member(direction, ['send', 'receive']) || !member(path, ['peer', 's3', 'server', 'cache']) || !member(status, ['active', 'complete', 'failed', 'cancelled'])) return [];
    const result: Record<string, string | number | null> = { direction, path, status, route: member(route, ['direct', 'relay', 'unknown', 'mixed']) ? route : null };
    for (const key of ['bytes', 'controlBytes', 'discardedBytes', 'milliseconds', 'discoveryMs', 'authorizationMs', 'connectionMs', 'firstByteMs', 'verificationMs', 'sourceReadMs', 'resumeOffset', 'discardedPrefixBytes']) result[key] = safeNumber(own(item, key));
    return [result];
  });
  const flag = (key: string) => typeof own(input, key) === 'boolean' ? own(input, key) as boolean : null;
  return { wanted: flag('wanted'), connected: flag('connected'), enabled: flag('enabled'), receiving: safeNumber(own(input, 'receiving')), sending: safeNumber(own(input, 'sending')),
    active: array(own(input, 'active'), 2).flatMap(item => { const safe = activePeerProjection(item); return safe ? [safe] : []; }), recent };
}
export const measurementScope = Object.freeze([
  '本机 recorded / dropped / rejected / captureCostMs 为本账号累计，clear 保留；retainedBytes 仅当前保留历史。recorded 含本机记录及成功导入的服务器事件，不代表业务请求数。',
  '服务器计数仅当前 source 的 captureId / clockId 累计；其他泳道保留事件不代表其完整累计计数。服务器与本机计数分别列出，不相加。',
  'media 父 bytes 是缓存、失败及最终 attempt 的累计应用读取；verifiedBytes 是实际完整校验的逻辑文件字节。attempt、HTTP、服务器IO与通道计数可能重叠，不相加；discarded 是已读中的丢弃子集。',
  'P50 仅保留历史内客户端 completed http.request 的逐请求 durationMs，附样本数；无样本为未知。失败、取消、started、父media、批平均与服务器IO不混入。',
  '速度来自同一实际传输的应用字节与单调耗时；RTT仅实际RTC stats。未知值保留null，0有效。未计算计费节省或未观测的反事实。',
  'client与各server clockId分泳道；不跨clock或墙钟相减。服务器缺startOffset仅画时点；客户端仅明确终态操作时长可反推起点。合并事件不猜父。',
  '保留事件可能早于当前captureId；各服务器事件自带capture/clock，本机保留事件按独立Trace标识关联，不把当前捕获设置倒推到全部旧事件。',
  'active通道 durationMs 自该Transfer实例实际创建点起，payloadBytesPerSecond 为该实例实际载荷 / 同单调区间，含建连等待，不含续传前缀。progressBytes 接收端为通过顺序帧校验的位置，发送端为实际请求起始位置及后续有效ACK位置；均非完整文件校验。resumeOffset 是本次实际起始前缀，bufferedAmount 是本地DC排队快照，非对端收到或计费流量。',
  '采集成本仅collector measured路径的实际单调计时，不含全部UI、网络、导出或RTC采样开销。详细关闭/暂停时仍更新轻状态与真实活动项。',
]);
export function createReport(monitor: NetworkMonitor, lease: ExportLease | null, extras: unknown) {
  if (!monitor.validExport(lease)) return null;
  const source = monitor.exportSnapshot(lease); if (!source) return null;
  const cards = Object.fromEntries((['api', 'websocket', 'sync', 'peer', 'transfer'] as Card[]).map(card => [card, sanitizeCard(card, source.cards[card])]));
  const events = source.events.flatMap(event => { const safe = event.origin === 'server' ? sanitizeServerEvent(event) : sanitizeEvent(event); return safe ? [safe] : []; });
  const metrics: Record<string, number | null> = {};
  for (const key of ['recorded', 'dropped', 'rejected', 'retainedBytes', 'activeOperations', 'deviceAliases', 'serverClocks', 'captureCostMs']) metrics[key] = safeNumber(own(source.metrics, key));
  const protocol = own(extras, 'protocol');
  const report = { v: 1, capturedAt: safeNumber(source.capturedAt), environment: { platform: 'web', api: 'same-origin /api',
    protocol: member(protocol, ['https:', 'http:']) ? protocol : null, rtcSupported: typeof own(extras, 'rtcSupported') === 'boolean' ? own(extras, 'rtcSupported') as boolean : null },
  capture: { active: source.active === true, detailed: source.detailed === true, paused: source.paused === true,
    clockId: hexId(source.clockId) ? source.clockId : null, captureId: hexId(source.captureId) ? source.captureId : null },
  cards, socket: { connectedDurationMs: safeNumber(own(own(extras, 'socket'), 'connectedDurationMs')) }, http: safeHttpActivity(own(extras, 'http')), peer: safePeerView(own(extras, 'peer')), events,
  server: sanitizeServerState(source.server), limits: LIMITS, metrics, samples: requestSamples({ events }), measurementScope };
  return monitor.validExport(lease) ? report : null;
}
export type NetworkReport = NonNullable<ReturnType<typeof createReport>>;
function readable(report: NetworkReport): string {
  const rows = ['keepsite 网络监控报告', `生成时间（墙钟毫秒）: ${report.capturedAt ?? '未知'}`, '', '环境与捕获',
    JSON.stringify(report.environment), JSON.stringify(report.capture), `WS同clock持续时长: ${milliseconds(report.socket.connectedDurationMs)}`, '', '五类状态'];
  for (const [key, values] of Object.entries(report.cards)) rows.push(`${key}: ${Object.entries(values).map(([field, value]) => `${field}=${typeof value === 'string' ? label(value) : value ?? '未知'}`).join(' · ') || '暂无观测'}`);
  rows.push('', '当前HTTP（独立活动请求，不依赖历史）', JSON.stringify(report.http), '', '当前通道与近期传输（别名、安全字段）', JSON.stringify(report.peer, null, 2), '', '事件与操作时间线');
  for (const event of report.events) {
    const interval = eventInterval(event), clock = event.origin === 'server' ? event.clockId : report.capture.clockId ?? 'unknown';
    rows.push(`[${event.origin}:${clock}] #${event.sequence} ${event.event} · ${label(event.status)} · offset ${milliseconds(interval.end)} · start ${milliseconds(interval.start)} · duration ${milliseconds(event.durationMs)} · bytes ${bytes(event.bytes)}`,
      `  ${JSON.stringify(event)}`);
  }
  if (!report.events.length) rows.push('暂无保留事件。');
  rows.push('', '容量、丢弃与实际采集成本', `本机账号累计及当前容量: ${JSON.stringify(report.metrics)}`, `服务器当前capture/clock: ${JSON.stringify(report.server)}`,
    `限制: ${JSON.stringify(report.limits)}`, `客户端成功HTTP P50: ${milliseconds(report.samples.p50Ms)} · n=${report.samples.n} · 缺测=${report.samples.missingDuration}`, '', '测量范围与缺测', ...report.measurementScope);
  return rows.join('\n') + '\n';
}
export function serializeReport(report: NetworkReport, format: ReportFormat): string {
  if (format === 'text') return readable(report);
  if (format === 'jsonl') { const { events, ...metadata } = report; return [JSON.stringify({ type: 'metadata', ...metadata }), ...events.map(event => JSON.stringify({ type: 'event', ...event }))].join('\n') + '\n'; }
  return JSON.stringify(report, null, 2) + '\n';
}
export interface DownloadPorts {
  create(blob: Blob): string; revoke(url: string): void;
  deliver(url: string, filename: string, valid: () => boolean): void;
}
const browserPorts: DownloadPorts = {
  create: blob => URL.createObjectURL(blob), revoke: url => URL.revokeObjectURL(url),
  deliver(url, filename, valid) {
    const link = document.createElement('a'); link.href = url; link.download = filename; link.hidden = true;
    try { document.body.append(link); if (valid()) link.click(); } finally { link.remove(); }
  },
};
/** A lease is checked before projection, after serialization and at the actual delivery boundary. */
export class ReportDownload {
  private url?: string; private unsubscribe?: () => void; private timer?: ReturnType<typeof setTimeout>;
  private generation = 0;
  private monitor: NetworkMonitor; private ports: DownloadPorts;
  private ownerSessionId: string | null;
  constructor(monitor: NetworkMonitor, ports: DownloadPorts = browserPorts, ownerSessionId: string | null = monitor.snapshot().sessionId) {
    this.monitor = monitor; this.ports = ports; this.ownerSessionId = ownerSessionId;
  }
  dispose = () => {
    this.generation++;
    if (this.timer) clearTimeout(this.timer); this.timer = undefined;
    this.unsubscribe?.(); this.unsubscribe = undefined;
    const url = this.url; this.url = undefined; if (url) this.ports.revoke(url);
  };
  download(format: ReportFormat, extras: unknown): boolean {
    if (!this.ownerSessionId || this.monitor.snapshot().sessionId !== this.ownerSessionId) return false;
    this.dispose(); const generation = this.generation;
    const lease = this.monitor.exportLease(), report = createReport(this.monitor, lease, extras); if (!report) return false;
    const text = serializeReport(report, format); if (!this.monitor.validExport(lease)) return false;
    const valid = () => this.generation === generation && this.monitor.validExport(lease);
    this.unsubscribe = this.monitor.onInvalidated(() => { if (!valid()) this.dispose(); });
    if (!this.unsubscribe) return false;
    try {
      const url = this.ports.create(new Blob([text], { type: format === 'json' ? 'application/json;charset=utf-8' : format === 'jsonl' ? 'application/x-ndjson;charset=utf-8' : 'text/plain;charset=utf-8' }));
      if (!valid()) { this.ports.revoke(url); if (this.generation === generation) this.dispose(); return false; }
      this.url = url;
      if (!valid()) { this.dispose(); return false; }
      this.ports.deliver(url, `keepsite-network.${format === 'text' ? 'txt' : format}`, () => this.url === url && valid());
      if (!valid() || this.url !== url) { if (this.generation === generation) this.dispose(); return false; }
      this.timer = setTimeout(this.dispose, 30000); return true;
    } catch { if (this.generation === generation) this.dispose(); return false; }
  }
}

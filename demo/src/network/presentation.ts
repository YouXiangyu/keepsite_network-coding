import type { MonitorEvent, MonitorSnapshot } from './monitor';
import { number, own } from './schema';

const names: Record<string, string> = {
  unknown: '未知', checking: '检查中', reachable: '最近可达', degraded: '最近异常', offline: '最近不可达',
  closed: '已关闭', connecting: '连接中', open: '已打开', ready: '已就绪', reconnecting: '重连中', error: '异常',
  idle: '空闲', snapshot: '初始同步', delta: '增量同步', catching_up: '追赶中', current: '已追平', failed: '失败',
  disabled: '已关闭', unavailable: '不可用', registering: '登记中', registered: '已登记', discovering: '发现中',
  signaling: '信令中', transferring: '传输中', completed: '完成', started: '开始', cancelled: '已取消', retrying: '等待重试',
  cache: '本机缓存', p2p_direct: 'P2P直连', p2p_relay: 'P2P中继', p2p_unknown: 'P2P路径未知', p2p_mixed: 'P2P混合路径',
  server_proxy: '服务器代理', direct_s3: 'S3直读', direct: '直连', relay: '中继', mixed: '混合',
  send: '发送', receive: '接收', requesting: '等待响应', headers: '已收响应头', loading: '读取中',
  invalid: '数据无效', capacity: '达到容量', stale: '旧响应', completed_ice: '检查完成',
  complete: '完成', active: '进行中', peer: 'P2P', s3: 'S3直读', server: '服务器代理',
  ok: '已认证', required: '需要登录', forbidden: '无权限', new: '尚未开始', connected: '已连接',
  disconnected: '已断开', checking_ice: '检查中', closing: '关闭中',
};
export function label(value: unknown): string { return typeof value === 'string' ? names[value] ?? value : '未知'; }
export const numeric = (value: unknown): number | null => number(value) ? value : null;
export const count = (value: unknown): string => number(value) ? String(value) : '未知';
export const yesNo = (value: unknown): string => value === true ? '是' : value === false ? '否' : '未知';
export function milliseconds(value: unknown): string { return number(value) ? `${value.toFixed(value < 10 ? 2 : 0)} ms` : '未知'; }
export function bytes(value: unknown): string {
  if (!number(value)) return '未知';
  if (value < 1024) return `${Number.isInteger(value) ? value : value.toFixed(1)} B`;
  return value < 1024 ** 2 ? `${(value / 1024).toFixed(1)} KiB` : `${(value / 1024 ** 2).toFixed(2)} MiB`;
}
export function instant(value: unknown): string {
  if (!number(value)) return '暂无观测';
  try { return new Date(value).toLocaleString('zh-CN', { hour12: false }); } catch { return '未知'; }
}
export function eventClock(event: MonitorEvent, clientClock: string | null): string {
  return event.origin === 'server' ? `server:${event.clockId}` : `client:${clientClock ?? 'unknown'}`;
}
export function eventInterval(event: MonitorEvent): { start: number | null; end: number | null } {
  const end = numeric(event.offsetMs);
  if (end === null) return { start: null, end };
  if (event.origin === 'server') return { start: numeric(event.startOffsetMs), end };
  const duration = numeric(event.durationMs);
  const terminal = event.status === 'completed' || event.status === 'failed' || event.status === 'cancelled';
  return { start: terminal && duration !== null && duration <= end ? end - duration : null, end };
}
export function eventGroup(event: MonitorEvent, clock: string): string {
  if (event.origin === 'server' && (event.coalesced || !event.traceId)) return eventIdentity(event);
  if (event.traceId) return `trace:${event.traceId}`;
  return `${clock}:${event.origin === 'client' ? event.operationId ?? `event-${event.sequence}` : event.spanId}`;
}
export function eventIdentity(event: MonitorEvent): string {
  return event.origin === 'server' ? `server:${event.captureId}:${event.clockId}:${event.sequence}` : `client:${event.sequence}`;
}
export function relatedEvents(events: readonly MonitorEvent[], event: MonitorEvent, clientClock: string | null) {
  const key = eventGroup(event, eventClock(event, clientClock));
  return events.filter(candidate => eventGroup(candidate, eventClock(candidate, clientClock)) === key);
}
export interface EventFilter { protocol: string; status: string; device: string; operation: string; from: string; to: string }
export const emptyFilter: EventFilter = { protocol: '', status: '', device: '', operation: '', from: '', to: '' };
export function timeFilterInvalid(filter: EventFilter): boolean {
  return [filter.from, filter.to].some(value => value.trim() !== '' && (!/^\d+(?:\.\d+)?$/.test(value) || !number(Number(value))));
}
export function filterEvents(events: readonly MonitorEvent[], filter: EventFilter): readonly MonitorEvent[] {
  if (timeFilterInvalid(filter)) return [];
  const bound = (value: string) => value.trim() !== '' && /^\d+(?:\.\d+)?$/.test(value) && number(Number(value)) ? Number(value) : null;
  const from = bound(filter.from), to = bound(filter.to);
  return events.filter(event => {
    const operation = event.origin === 'client' ? event.operationId : event.spanId;
    return (!filter.protocol || event.protocol === filter.protocol) && (!filter.status || event.status === filter.status)
      && (!filter.device || own(event, 'deviceId') === filter.device)
      && (!filter.operation || operation === filter.operation || own(event, 'parentOperationId') === filter.operation)
      && (from === null || event.offsetMs !== null && event.offsetMs >= from)
      && (to === null || event.offsetMs !== null && event.offsetMs <= to);
  });
}
export function timelineLanes(events: readonly MonitorEvent[], clientClock: string | null) {
  const lanes = new Map<string, MonitorEvent[]>();
  for (const event of events) { const key = eventClock(event, clientClock); const lane = lanes.get(key) ?? []; lane.push(event); lanes.set(key, lane); }
  return [...lanes].map(([clock, items]) => {
    const endpoints = items.flatMap(event => { const { start, end } = eventInterval(event); return [start, end].filter((n): n is number => n !== null); });
    const minimum = endpoints.length ? Math.min(...endpoints) : 0, maximum = endpoints.length ? Math.max(...endpoints) : 0;
    return { clock, items, minimum, maximum, span: maximum - minimum };
  });
}
/** Individual terminal HTTP measurements; never mix parents, attempts, server IO or batch means. */
export function requestSamples(snapshot: Pick<MonitorSnapshot, 'events'>) {
  const completed = snapshot.events.filter(event => event.origin === 'client' && event.event === 'http.request' && event.status === 'completed');
  const durations = completed
    .map(event => numeric(event.durationMs)).filter((value): value is number => value !== null).sort((a, b) => a - b);
  const middle = Math.floor(durations.length / 2);
  return { n: durations.length, missingDuration: completed.length - durations.length, p50Ms: durations.length ? durations.length % 2 ? durations[middle] : (durations[middle - 1] + durations[middle]) / 2 : null };
}

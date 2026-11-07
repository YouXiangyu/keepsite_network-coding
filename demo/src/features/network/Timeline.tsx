import type { MonitorEvent } from '../../network/monitor';
import { bytes, count, eventIdentity, eventInterval, label, milliseconds, timelineLanes } from '../../network/presentation';

export default function Timeline({ events, clockId }: { events: readonly MonitorEvent[]; clockId: string | null }) {
  return <div className="nm-timeline">{timelineLanes(events, clockId).map(lane => <section className="nm-lane" key={lane.clock} aria-label={`${lane.clock} 时间线`}>
    <h4>{lane.clock.startsWith('client:') ? '本机' : '服务器'} clock <code>{lane.clock.split(':')[1]}</code></h4>
    <p className="nm-muted">独立单调时间 · {milliseconds(lane.minimum)} — {milliseconds(lane.maximum)}</p>
    <ol>{lane.items.map(event => {
      const interval = eventInterval(event), left = interval.end === null ? 0 : lane.span ? ((interval.start ?? interval.end) - lane.minimum) / lane.span * 100 : 0;
      const width = interval.start !== null && interval.end !== null && lane.span ? (interval.end - interval.start) / lane.span * 100 : 0;
      return <li key={eventIdentity(event)}>
        <div className="nm-event-heading"><strong>{event.event}</strong><span>{label(event.status)}</span><span>#{event.sequence}</span></div>
        <div className="nm-track" aria-hidden="true"><span className={interval.start === null ? 'nm-point' : 'nm-bar'} style={{ left: `${left}%`, width: `${width}%` }} /></div>
        <dl className="nm-facts"><div><dt>时点</dt><dd>{milliseconds(interval.end)}</dd></div><div><dt>实际起点</dt><dd>{milliseconds(interval.start)}</dd></div><div><dt>耗时</dt><dd>{milliseconds(event.durationMs)}</dd></div>
          {event.bytes !== undefined && <div><dt>应用字节</dt><dd>{bytes(event.bytes)}</dd></div>}{event.origin === 'client' && event.verifiedBytes !== undefined && <div><dt>完整校验</dt><dd>{bytes(event.verifiedBytes)}</dd></div>}
          {event.origin === 'client' && event.controlBytes !== undefined && <div><dt>控制字节</dt><dd>{bytes(event.controlBytes)}</dd></div>}{event.origin === 'client' && event.discardedBytes !== undefined && <div><dt>丢弃子集</dt><dd>{bytes(event.discardedBytes)}</dd></div>}
          {event.origin === 'client' && event.route && <div><dt>路径</dt><dd>{label(event.route)}</dd></div>}{event.attempt !== undefined && <div><dt>尝试</dt><dd>{count(event.attempt)}</dd></div>}
          {event.errorCode && <div><dt>结果码</dt><dd>{event.errorCode}</dd></div>}{event.origin === 'client' && event.template && <div><dt>请求模板</dt><dd>{event.template}</dd></div>}
          {event.origin === 'client' && event.httpStatus !== undefined && <div><dt>HTTP状态</dt><dd>{event.httpStatus}</dd></div>}
          <div><dt>关联</dt><dd>{event.origin === 'server' ? event.coalesced ? '合并 · 未关联' : event.parentSpanId ? `父span ${event.parentSpanId}` : event.traceId ? '已知Trace · 无父span' : '未关联' : event.parentOperationId ? `父操作 ${event.parentOperationId}` : event.operationId ?? '未关联'}</dd></div>
        </dl>
      </li>;
    })}</ol>
  </section>)}</div>;
}

import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
const hook = registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL?.includes('/src/') && specifier.startsWith('.') && !specifier.endsWith('.ts')) return { url: new URL(specifier + '.ts', context.parentURL).href, shortCircuit: true };
  return nextResolve(specifier, context);
} });
const { NetworkMonitor } = await import('../src/network/monitor.ts');
const runtime = await import('../src/network/runtime.ts');
const presentation = await import('../src/network/presentation.ts');
const { createReport, serializeReport, ReportDownload, safePeerView, safeHttpActivity } = await import('../src/network/report.ts');
const { ServerTraceClient, MonitorViewActions } = await import('../src/network/serverTraceClient.ts');
hook.deregister();
afterEach(() => runtime.accountMonitoring(false));
function monitor() {
  let t = 100, id = 0;
  const result = new NetworkMonitor({ monotonic: () => ++t, wall: () => 1700000000000, id: () => (++id).toString(16).padStart(32, '0'), clockId: 'f'.repeat(32) });
  result.startAccount(); return result;
}
const client = overrides => ({ origin: 'client', protocol: 'http', event: 'http.request', status: 'completed', sequence: 1, timestamp: 1234, offsetMs: 20, durationMs: 5, operationId: 'op-1', ...overrides });
const server = overrides => ({ origin: 'server', protocol: 'http', event: 'http.request', status: 'completed', sequence: 1, timestamp: 999999, offsetMs: 120, startOffsetMs: 100, durationMs: 7, clockId: 'a'.repeat(32), captureId: 'b'.repeat(32), spanId: 'c'.repeat(32), traceId: null, parentSpanId: null, ...overrides });

test('HTTP activity is real owner state in default-off and survives clear/history rotation with bounded display', () => {
  runtime.accountMonitoring(true); const m = runtime.networkMonitor;
  const owners = Array.from({ length: 35 }, () => runtime.startHttp('tickets'));
  let activity = runtime.httpActivity(); assert.equal(activity.total, 35); assert.equal(activity.requests.length, 32); assert.equal(activity.omitted, 3);
  assert.equal(m.snapshot().events.length, 0); const alias = activity.requests[0].alias;
  runtime.httpHeaders(owners[0], 200); m.setDetailed(true); m.clearHistory(); m.setPaused(true); m.setDetailed(false);
  activity = runtime.httpActivity(); assert.equal(activity.requests[0].alias, alias); assert.equal(activity.requests[0].phase, 'headers'); assert.equal(activity.requests[0].status, 200);
  owners.forEach(owner => runtime.finishHttp(owner, 'completed')); activity = runtime.httpActivity();
  assert.deepEqual(activity, { total: 0, omitted: 0, requests: [] }); assert.equal(m.snapshot().cards.api.active, 0);
});

test('old HTTP account completion cannot remove or decrement new owners; diagnostic update exceptions do not leak owners', () => {
  runtime.accountMonitoring(true); const old = runtime.startHttp('upload'); runtime.accountMonitoring(true);
  const fresh = runtime.startHttp('tickets'); runtime.finishHttp(old, 'failed', 'network');
  assert.equal(runtime.httpActivity().total, 1); assert.equal(runtime.httpActivity().requests[0].template, 'tickets');
  const m = runtime.networkMonitor, update = m.update; m.update = () => { throw new Error('observer'); };
  try { runtime.finishHttp(fresh, 'completed'); } finally { m.update = update; }
  assert.equal(runtime.httpActivity().total, 0); assert.equal(runtime.httpActivity().requests.length, 0);
});

test('formatting keeps zero, null and unknown separate and timelines use actual same-clock starts', () => {
  assert.equal(presentation.bytes(0), '0 B'); assert.equal(presentation.count(0), '0'); assert.equal(presentation.milliseconds(0), '0.00 ms');
  for (const n of [null, undefined, NaN, Infinity, -1]) assert.equal(presentation.bytes(n), '未知');
  assert.deepEqual(presentation.eventInterval(client({})), { start: 15, end: 20 });
  assert.deepEqual(presentation.eventInterval(client({ status: 'retrying' })), { start: null, end: 20 });
  assert.deepEqual(presentation.eventInterval(client({ durationMs: 21 })), { start: null, end: 20 });
  assert.deepEqual(presentation.eventInterval(server({})), { start: 100, end: 120 });
  assert.deepEqual(presentation.eventInterval(server({ startOffsetMs: undefined })), { start: null, end: 120 });
  const lanes = presentation.timelineLanes([client({}), server({}), server({ clockId: 'd'.repeat(32), offsetMs: 800, startOffsetMs: 700 })], 'f'.repeat(32));
  assert.deepEqual(lanes.map(lane => [lane.minimum, lane.maximum]), [[15, 20], [100, 120], [700, 800]]);
  const trace = 'e'.repeat(32); assert.equal(presentation.eventGroup(client({ traceId: trace }), 'client:x'), presentation.eventGroup(server({ traceId: trace }), 'server:y'));
  assert.notEqual(presentation.eventGroup(server({ coalesced: true, traceId: null }), 'server:y'), `trace:${trace}`);
});

test('filters combine protocol/status/device/operation/clock offset and reject invalid numeric bounds', () => {
  const events = [client({ deviceId: 'device-1' }), client({ sequence: 2, protocol: 'p2p', event: 'media.attempt', parentOperationId: 'op-1', operationId: 'op-2', status: 'failed', offsetMs: 30 }), server({})];
  const base = presentation.emptyFilter;
  assert.equal(presentation.filterEvents(events, { ...base, protocol: 'http', status: 'completed', device: 'device-1', operation: 'op-1', from: '0', to: '20' }).length, 1);
  assert.equal(presentation.filterEvents(events, { ...base, operation: 'op-1' }).length, 2);
  assert.equal(presentation.filterEvents(events, { ...base, from: 'abc' }).length, 0);
  assert.equal(presentation.timeFilterInvalid({ ...base, to: '-1' }), true);
  assert.equal(presentation.filterEvents(events, { ...base, from: '31', to: '20' }).length, 0);
});

test('filtered local operation expansion restores only its actual known cross-module trace across independent clocks', () => {
  const trace = 'e'.repeat(32), otherTrace = 'd'.repeat(32), local = client({ traceId: trace });
  const events = [local, client({ sequence: 2, traceId: trace, operationId: 'op-2', parentOperationId: 'op-1', protocol: 'p2p' }), server({ traceId: trace }), server({ sequence: 2, traceId: trace, parentSpanId: 'c'.repeat(32), spanId: 'a'.repeat(32), protocol: 'sql', event: 'sql.commit' }), server({ sequence: 3, traceId: otherTrace }), server({ sequence: 4, coalesced: true, traceId: null })];
  const filtered = presentation.filterEvents(events, { ...presentation.emptyFilter, protocol: 'http', operation: 'op-1' }); assert.deepEqual(filtered, [local]);
  const expanded = presentation.relatedEvents(events, filtered[0], 'f'.repeat(32)); assert.equal(expanded.length, 4); assert.equal(expanded.filter(event => event.origin === 'server').length, 2);
  assert.equal(presentation.timelineLanes(expanded, 'f'.repeat(32)).length, 2);
  assert.equal(presentation.relatedEvents(events, events[5], 'f'.repeat(32)).length, 1);
  const m = monitor(); m.setDetailed(true); m.record(m.scope('http'), local); const lease = m.exportLease(); m.clearHistory(); assert.equal(m.validExport(lease), false); assert.deepEqual(presentation.relatedEvents(m.snapshot().events, local, m.snapshot().clockId), []);
});

test('WS connected duration uses original socket monotonic origin through history rotation and rejects old sockets', context => {
  let clock = 100; context.mock.method(performance, 'now', () => clock); runtime.accountMonitoring(true);
  const socket = runtime.startSocket(); assert.equal(runtime.socketActivity().connectedDurationMs, null); runtime.socketOpen(socket);
  clock = 200; context.mock.method(Date, 'now', () => 1); assert.equal(runtime.socketActivity().connectedDurationMs, 100);
  runtime.networkMonitor.setDetailed(true); runtime.networkMonitor.clearHistory(); runtime.networkMonitor.setDetailed(false); assert.equal(runtime.socketActivity().connectedDurationMs, 100);
  const fresh = runtime.startSocket(); runtime.socketClose(socket); assert.equal(runtime.socketActivity().connectedDurationMs, null);
  runtime.socketOpen(fresh); clock = 250; runtime.socketError(fresh); assert.equal(runtime.socketActivity().connectedDurationMs, 50); assert.equal(runtime.networkMonitor.snapshot().cards.websocket.errorCode, 'network');
  runtime.socketClose(fresh); assert.equal(runtime.socketActivity().connectedDurationMs, null); runtime.accountMonitoring(true); runtime.socketOpen(socket); assert.equal(runtime.socketActivity().connectedDurationMs, null);
});

test('request P50 uses only individual completed client HTTP durations and reports missing samples', () => {
  const samples = presentation.requestSamples({ events: [client({ durationMs: 0 }), client({ durationMs: 10 }), client({ durationMs: undefined }), client({ status: 'failed', durationMs: 1000 }), client({ event: 'media.read', durationMs: 2000 }), server({ durationMs: 5000 })] });
  assert.deepEqual(samples, { n: 2, missingDuration: 1, p50Ms: 5 });
  assert.deepEqual(presentation.requestSamples({ events: [] }), { n: 0, missingDuration: 0, p50Ms: null });
});

test('report formats have equivalent safe data, JSONL metadata first, and no business secrets or hostile getters', () => {
  const m = monitor(); m.setDetailed(true); const scope = m.scope('http');
  m.record(scope, { protocol: 'http', event: 'http.request', status: 'completed', durationMs: 0, bytes: 0, url: 'https://secret.example/signed' });
  const secret = 'secret@example.test Bearer token 192.0.2.1 v=0 SDP ' + 'a'.repeat(64); let accessed = 0;
  const recent = { id: secret, reason: secret, direction: 'receive', path: 'peer', status: 'complete', route: 'mixed', bytes: 0, controlBytes: 1, discardedBytes: 0, milliseconds: 0 };
  Object.defineProperty(recent, 'sourceReadMs', { get() { accessed++; throw Error(secret); } });
  const extras = { protocol: 'https:', origin: secret, rtcSupported: true, http: { total: 1, requests: [{ alias: 'request-1', template: 'tickets', phase: 'requesting', startedMs: 0, url: secret }] }, peer: { peerId: secret, name: secret, reason: secret, recent: [recent], active: [] } };
  const report = createReport(m, m.exportLease(), extras); assert.ok(report); assert.equal(accessed, 0);
  assert.equal(report.http.requests[0].startedMs, 0); assert.equal(report.peer.recent[0].bytes, 0); assert.equal(report.samples.p50Ms, 0);
  for (const format of ['json', 'jsonl', 'text']) { const text = serializeReport(report, format); assert.doesNotMatch(text, /secret@example|Bearer token|192\.0\.2\.1|v=0 SDP|a{64}|secret\.example/); }
  const json = JSON.parse(serializeReport(report, 'json'));
  const lines = serializeReport(report, 'jsonl').trim().split('\n').map(line => JSON.parse(line));
  assert.equal(lines[0].type, 'metadata'); assert.equal(lines.length, report.events.length + 1);
  assert.deepEqual(lines[0].cards, json.cards); assert.deepEqual(lines[0].peer, json.peer); assert.deepEqual(lines[0].samples, json.samples);
  assert.equal(lines[1].type, 'event'); assert.equal(lines[1].durationMs, 0);
  assert.match(serializeReport(report, 'text'), /P50: 0\.00 ms · n=1/);
  const lease = m.exportLease(); m.clearHistory(); assert.equal(createReport(m, lease, extras), null);
});

test('second projection bounds arrays and rejects raw names/routes/accessors and revoked proxies', () => {
  let accessed = 0; const raw = { alias: 'request-1', template: 'tickets', phase: 'headers', startedMs: 0, status: 200 };
  Object.defineProperty(raw, 'url', { get() { accessed++; throw Error('private'); } });
  const http = safeHttpActivity({ total: 35, requests: Array(40).fill(raw) }); assert.equal(http.requests.length, 32); assert.equal(http.omitted, 3); assert.equal(accessed, 0);
  assert.equal(safeHttpActivity({ total: Infinity, requests: [{ alias: 'https://private', template: 'tickets', phase: 'headers' }] }).total, null);
  const revocable = Proxy.revocable([], {}); revocable.revoke(); assert.deepEqual(safePeerView({ recent: revocable.proxy }).recent, []);
  assert.equal(safePeerView({ recent: Array(60).fill({ direction: 'receive', path: 'cache', status: 'complete' }) }).recent.length, 50);
});

function downloads(m, overrides = {}) {
  const created = [], revoked = [], delivered = [], bodies = [];
  const ports = { create(blob) { bodies.push(blob); const url = `blob:owned-${created.length}`; created.push(url); return url; }, revoke(url) { revoked.push(url); }, deliver(url, filename, valid) { if (valid()) delivered.push([url, filename]); }, ...overrides };
  return { instance: new ReportDownload(m, ports), created, revoked, delivered, bodies };
}
test('download revokes immediately on clear/account change and replacement/unmount, all formats are UTF8 artifacts', async () => {
  const m = monitor(), d = downloads(m);
  assert.equal(d.instance.download('json', {}), true); assert.equal(d.delivered.length, 1); m.clearHistory(); assert.deepEqual(d.revoked, ['blob:owned-0']);
  assert.equal(d.instance.download('jsonl', {}), true); assert.equal(d.instance.download('text', {}), true); assert.deepEqual(d.revoked, ['blob:owned-0', 'blob:owned-1']);
  assert.match(await d.bodies[2].text(), /keepsite 网络监控报告/); m.startAccount(); assert.deepEqual(d.revoked, d.created);
  assert.equal(d.instance.download('json', {}), false);
  const replacement = downloads(m); assert.equal(replacement.instance.download('json', {}), true); replacement.instance.dispose(); assert.deepEqual(replacement.revoked, replacement.created);
  d.instance.dispose(); assert.deepEqual(d.revoked, d.created);
});

test('account/history changes during report generation, URL creation or final delivery cannot deliver stale files', () => {
  const m = monitor(); let delivered = 0, revoked = 0;
  let d = new ReportDownload(m, { create() { m.startAccount(); return 'blob:old'; }, revoke() { revoked++; }, deliver() { delivered++; } });
  assert.equal(d.download('json', {}), false); assert.equal(delivered, 0); assert.equal(revoked, 1); d.dispose();
  d = new ReportDownload(m, { create: () => 'blob:old', revoke() { revoked++; }, deliver(_url, _filename, valid) { m.clearHistory(); if (valid()) delivered++; } });
  assert.equal(d.download('text', {}), false); assert.equal(delivered, 0); assert.equal(revoked, 2); d.dispose();
  const oldSnapshot = m.exportSnapshot.bind(m); m.exportSnapshot = lease => { const snapshot = oldSnapshot(lease); m.clearHistory(); return snapshot; };
  assert.equal(d.download('jsonl', {}), false); assert.equal(delivered, 0); m.exportSnapshot = oldSnapshot;
});

test('download observer capacity and creation/delivery failures are closed and release resources', () => {
  const m = monitor(), listeners = Array.from({ length: 32 }, () => m.onInvalidated(() => {}));
  const d = downloads(m); assert.equal(d.instance.download('json', {}), false); assert.equal(d.created.length, 0); listeners.forEach(stop => stop());
  const failing = downloads(m, { deliver() { throw Error('browser'); } }); assert.equal(failing.instance.download('json', {}), false); assert.deepEqual(failing.revoked, failing.created);
  d.instance.dispose(); failing.instance.dispose();
});

test('unassociated server identities separate capture/clock/sequence while actual trace still spans clocks', () => {
  for (const coalesced of [true, false]) {
    const a = server({ coalesced, traceId: null }), b = server({ coalesced, traceId: null, captureId: 'd'.repeat(32) });
    assert.notEqual(presentation.eventGroup(a, presentation.eventClock(a, null)), presentation.eventGroup(b, presentation.eventClock(b, null)));
    assert.notEqual(presentation.eventIdentity(a), presentation.eventIdentity(b));
    assert.deepEqual(presentation.relatedEvents([a, b], a, null), [a]);
  }
  const traceId = 'e'.repeat(32), a = server({ traceId }), b = server({ traceId, captureId: 'd'.repeat(32), clockId: '9'.repeat(32) });
  assert.equal(presentation.relatedEvents([a, b], a, null).length, 2);
});

const deferred = () => { let resolve; const promise = new Promise(yes => { resolve = yes; }); return { promise, resolve }; };
function pageActions(m) {
  const reads = [], clears = [], busy = [];
  const client = new ServerTraceClient(m, {
    read(capture, signal) { const gate = deferred(); reads.push({ capture, signal, ...gate }); return gate.promise; },
    clear(capture, epoch, signal) { const gate = deferred(); clears.push({ capture, epoch, signal, ...gate }); return gate.promise; }, epoch: () => 7,
  });
  const view = new MonitorViewActions(m, client, m.snapshot().sessionId, value => busy.push(value));
  return { client, view, reads, clears, busy };
}

test('page clear has a synchronous busy owner, preserves original DELETE and unlocks only on completion', async () => {
  const m = monitor(); m.setDetailed(true); const h = pageActions(m), capture = m.snapshot().captureId;
  const clearing = h.view.run('clear'); assert.deepEqual(h.busy, [true]); assert.equal(h.clears.length, 1); assert.equal(h.clears[0].capture, capture); assert.equal(h.clears[0].epoch, 7);
  const history = m.exportLease().history;
  assert.equal(await h.view.run('refresh'), null); assert.equal(await h.view.run('clear'), null);
  assert.equal(h.reads.length, 0); assert.equal(h.clears.length, 1); assert.equal(h.clears[0].signal.aborted, false); assert.equal(m.exportLease().history, history);
  h.clears[0].resolve(); assert.equal(await clearing, 'ready'); assert.deepEqual(h.busy, [true, false]);
  const refresh = h.view.run('refresh'); assert.equal(h.reads.length, 1); h.reads[0].resolve({}); assert.equal(await refresh, 'invalid'); h.view.dispose();
});

for (const invalidate of ['off', 'pause', 'account', 'dispose']) {
  test(`clear invalidates on ${invalidate} and old finally cannot unlock a newer clear owner`, async () => {
    const m = monitor(); m.setDetailed(true); const h = pageActions(m), old = h.view.run('clear');
    if (invalidate === 'off') h.view.setDetailed(false);
    else if (invalidate === 'pause') h.view.togglePaused();
    else if (invalidate === 'account') m.startAccount();
    else h.view.dispose();
    assert.equal(h.clears[0].signal.aborted, true);
    let view = h.view;
    if (invalidate === 'account' || invalidate === 'dispose') view = new MonitorViewActions(m, h.client, m.snapshot().sessionId, value => h.busy.push(value));
    m.setDetailed(true); m.setPaused(false);
    const next = view.run('clear'); assert.equal(h.clears.length, 2); const before = [...h.busy];
    h.clears[0].resolve(); assert.equal(await old, null); assert.deepEqual(h.busy, before);
    assert.equal(await view.run('refresh'), null); assert.equal(h.clears[1].signal.aborted, false);
    h.clears[1].resolve(); assert.equal(await next, 'ready'); assert.equal(h.busy.at(-1), false); view.dispose(); h.view.dispose();
  });
}

test('old page controls and cleanup cannot mutate or cancel a replacement page even before React unmount', async () => {
  const m = monitor(); const sessionA = m.snapshot().sessionId, h = pageActions(m); m.startAccount(); m.setDetailed(true);
  const viewB = new MonitorViewActions(m, h.client, m.snapshot().sessionId), history = m.exportLease().history;
  h.view.setDetailed(false); h.view.togglePaused(); assert.equal(await h.view.run('clear'), null); assert.equal(await h.view.run('refresh'), null);
  assert.equal(m.snapshot().detailed, true); assert.equal(m.snapshot().paused, false); assert.equal(m.exportLease().history, history); assert.equal(h.reads.length + h.clears.length, 0);
  const lateRenderA = new MonitorViewActions(m, h.client, sessionA); lateRenderA.activate(); assert.equal(lateRenderA.valid(), false);
  const lateDownloadA = new ReportDownload(m, { create() { assert.fail('old render cannot create a file'); }, revoke() {}, deliver() { assert.fail('old render cannot deliver'); } }, sessionA);
  assert.equal(lateDownloadA.download('json', {}), false);
  const current = viewB.run('refresh'); assert.equal(h.reads.length, 1); h.view.dispose(); lateRenderA.dispose(); lateDownloadA.dispose(); assert.equal(h.reads[0].signal.aborted, false);
  h.reads[0].resolve({}); assert.equal(await current, 'invalid');
  viewB.dispose(); viewB.activate(); assert.equal(viewB.valid(), true, 'same-owner effect replay remains usable'); viewB.setDetailed(false); assert.equal(m.snapshot().detailed, false); viewB.dispose();
  const anonymous = new MonitorViewActions(m, h.client, null); anonymous.setDetailed(true); assert.equal(m.snapshot().detailed, false); assert.equal(await anonymous.run('clear'), null); anonymous.dispose();
});

test('a download instance rejects a later account at entry but permits its own account after clear', () => {
  const m = monitor(), a = downloads(m); m.startAccount(); m.setDetailed(true);
  assert.equal(a.instance.download('json', { peer: { recent: [{ direction: 'receive', path: 'server', status: 'complete', bytes: 777 }] } }), false);
  assert.equal(a.created.length, 0); assert.equal(a.delivered.length, 0); a.instance.dispose();
  const b = downloads(m); assert.equal(b.instance.download('json', {}), true); m.clearHistory(); assert.equal(b.instance.download('json', {}), true); b.instance.dispose();
});

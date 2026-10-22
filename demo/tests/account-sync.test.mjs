import { test, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { signedSession, snapshotPage, sampleItem } from './web-fixtures.mjs';
const hook = registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL?.includes('/src/') && specifier.startsWith('.') && !specifier.endsWith('.ts')) return { url: new URL(specifier + '.ts', context.parentURL).href, shortCircuit: true };
  return nextResolve(specifier, context);
} });
const api = await import('../src/api.ts');
const { AccountSync } = await import('../src/app/accountSync.ts'); hook.deregister();
const actors = [];
afterEach(() => { for (const actor of actors.splice(0)) actor.stop(); api.installSession(null); });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); };
const emptyChanges = (serverId, after) => ({ serverId, userId: 'a', fromCursor: after, nextCursor: after, headCursor: after, hasMore: false, changes: [], capabilities: snapshotPage().capabilities });
function setup(overrides = {}) {
  api.installSession(signedSession()); const connections = [], reads = []; let count = 0, maximum = 0;
  const transport = {
    snapshot: overrides.snapshot ?? (async () => snapshotPage()),
    changes: async (...args) => { reads.push(args.slice(0, 2)); count++; maximum = Math.max(maximum, count); try { return await (overrides.changes ?? emptyChanges)(...args); } finally { count--; } },
    events: (hint, close) => { const connection = { hint, close, stopped: false }; connections.push(connection); return () => { connection.stopped = true; }; },
  };
  const actor = new AccountSync('a', transport); actors.push(actor); actor.start();
  return { actor, connections, reads, maximum: () => maximum };
}
test('a complete paginated snapshot and changes in the subscription gap publish atomically', async () => {
  const second = deferred(), a = sampleItem('a'), b = sampleItem('b'), expiresAt = Date.now() + 600000;
  const h = setup({ snapshot: async token => token ? second.promise : { ...snapshotPage(), cursor: 5, total: 2, expiresAt, tickets: [a], nextPageToken: 'next' },
    changes: async (serverId, after) => after === 5 ? { ...emptyChanges(serverId, after), nextCursor: 7, headCursor: 7, changes: [{ cursor: 6, type: 'upsert', ticket: { ...a, version: 2, fields: { ...a.fields, title: 'new' } } }, { cursor: 7, type: 'delete', id: 'b', version: 2, deletedAt: 1 }] } : emptyChanges(serverId, after) });
  await settle(); assert.equal(h.actor.snapshot().ready, false); assert.deepEqual(h.actor.snapshot().data.tickets, []);
  second.resolve({ ...snapshotPage(), cursor: 5, total: 2, expiresAt, offset: 1, tickets: [b], nextPageToken: null });
  // All pages share the same server expiry, independent of local time.
  await h.actor.catchUp(7); assert.equal(h.actor.snapshot().cursor, 7); assert.deepEqual(h.actor.snapshot().data.tickets.map(item => item.fields.title), ['new']);
});
test('snapshot expiration is decided by the server rather than the device wall clock', async () => {
  const h = setup({ snapshot: async () => ({ ...snapshotPage(), expiresAt: 1 }) });
  await h.actor.catchUp(); assert.equal(h.actor.snapshot().ready, true);
});
test('expired cursors rebuild without exposing partial pages or emptying current data', async () => {
  let snapshots = 0, expire = false; const second = deferred(); const expiry = Date.now() + 100000;
  const h = setup({ snapshot: async token => { snapshots++; if (snapshots === 1) return snapshotPage(); return token ? second.promise : { ...snapshotPage(), cursor: 20, total: 2, expiresAt: expiry, tickets: [sampleItem('new-a')], nextPageToken: 'page2' }; },
    changes: async (serverId, after) => { if (expire) { expire = false; throw new api.ApiError('expired', 410, 'cursor_expired'); } return emptyChanges(serverId, after); } });
  await h.actor.catchUp(); expire = true; const rebuilding = h.actor.catchUp(); await settle();
  assert.equal(h.actor.snapshot().rebuilding, true); assert.equal(h.actor.snapshot().data.tickets[0].id, 'item-1');
  second.resolve({ ...snapshotPage(), cursor: 20, total: 2, offset: 1, expiresAt: expiry, tickets: [sampleItem('new-b')], nextPageToken: null });
  await rebuilding; assert.deepEqual(h.actor.snapshot().data.tickets.map(item => item.id), ['new-a', 'new-b']);
});
test('a write barrier drains earlier changes and an older in-flight head does not reject a later commit', async () => {
  const gate = deferred(); let held = false, updated = false;
  const h = setup({ changes: async (serverId, after) => {
    if (held) { held = false; return gate.promise; }
    if (updated && after === 7) return { ...emptyChanges(serverId, after), nextCursor: 9, headCursor: 9, changes: [{ cursor: 8, type: 'upsert', ticket: sampleItem('remote') }, { cursor: 9, type: 'upsert', ticket: { ...sampleItem(), version: 2 } }] };
    return emptyChanges(serverId, after);
  } });
  await h.actor.catchUp(); held = true; const read = h.actor.catchUp(); await settle(); updated = true;
  const write = h.actor.catchUp(9, false, 'server'); gate.resolve(emptyChanges('server', 7));
  await Promise.all([read, write]); assert.equal(h.actor.snapshot().cursor, 9); assert.equal(h.maximum(), 1); assert.equal(h.actor.snapshot().data.tickets.length, 2);
});
test('duplicate and reversed hints do not issue HTTP at an already confirmed cursor', async () => {
  const h = setup(); await h.actor.catchUp(); h.connections[0].hint({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'server', cursor: 7 }); await settle();
  const before = h.reads.length;
  for (const cursor of [7, 5, 7, 0]) h.connections[0].hint({ v: 1, type: 'sync.hint', userId: 'a', serverId: 'server', cursor });
  await settle(); assert.equal(h.reads.length, before);
});
test('an HTTP failure while WebSocket is ready recovers on the same-cursor heartbeat', async () => {
  let fail = true;
  const h = setup({ snapshot: async () => { if (fail) throw new api.ApiError('temporary', 503); return { ...snapshotPage(), cursor: 0 }; } });
  h.connections[0].hint({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'server', cursor: 0 }); await settle();
  assert.equal(h.actor.snapshot().ready, false); fail = false;
  h.connections[0].hint({ v: 1, type: 'sync.hint', userId: 'a', serverId: 'server', cursor: 0 }); await settle();
  assert.equal(h.actor.snapshot().ready, true); assert.equal(h.actor.snapshot().error, '');
});
test('stopped generations cannot publish delayed snapshot pages or old socket frames', async () => {
  const gate = deferred(); const h = setup({ snapshot: async () => gate.promise }); await settle();
  const old = h.connections[0]; h.actor.stop(); api.installSession(signedSession('b'));
  gate.resolve(snapshotPage()); old.hint({ v: 1, type: 'sync.ready', userId: 'a', serverId: 'server', cursor: 9 }); await settle();
  assert.equal(h.actor.snapshot().ready, false); assert.deepEqual(h.actor.snapshot().data.tickets, []); assert.equal(old.stopped, true);
});

test('a pending retry does not disable fallback after a same-account stop/start', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] }); let requests = 0;
  const h = setup({ snapshot: async () => { requests++; throw new api.ApiError('temporary', 503); } });
  await settle(); assert.equal(requests, 1); h.actor.stop(); api.installSession(signedSession()); h.actor.start();
  await settle(); assert.equal(requests, 2);
  context.mock.timers.tick(1001); await settle(); assert.equal(requests, 3);
  context.mock.timers.tick(2001); await settle(); assert.equal(requests, 4);
});
test('a silent ready socket triggers the heartbeat watchdog and incremental fallback', async context => {
  context.mock.timers.enable({ apis: ['setTimeout'] }); const h = setup(); await h.actor.catchUp();
  const socket = h.connections[0]; socket.hint({ v: 1, type: 'sync.ready', serverId: 'server', userId: 'a', cursor: 7 }); await settle();
  const before = h.reads.length; context.mock.timers.tick(45001); await settle(); assert.equal(socket.stopped, true);
  context.mock.timers.tick(1001); await settle(); assert.ok(h.reads.length > before); assert.equal(h.connections.length, 2);
});

test('a receipt in B10 cannot be satisfied by A100 and closes the previous domain socket', async () => {
  let server = 'A'; const gate = deferred();
  const h = setup({ snapshot: async () => server === 'A' ? { ...snapshotPage(), serverId: 'A', cursor: 100 } : gate.promise });
  await h.actor.catchUp(); const old = h.connections[0];
  old.hint({ v: 1, type: 'sync.ready', serverId: 'A', userId: 'a', cursor: 100 }); await settle();
  server = 'B'; let confirmed = false;
  const write = h.actor.catchUp(10, false, 'B').then(data => { confirmed = true; return data; });
  await settle(); assert.equal(confirmed, false); assert.equal(h.actor.snapshot().serverId, 'A');
  gate.resolve({ ...snapshotPage(), serverId: 'B', cursor: 10, tickets: [sampleItem('new-domain')] });
  assert.equal((await write).tickets[0].id, 'new-domain'); assert.equal(h.actor.snapshot().serverId, 'B'); assert.equal(old.stopped, true);
  const before = h.reads.length; old.hint({ v: 1, type: 'sync.hint', serverId: 'A', userId: 'a', cursor: 200 }); await settle();
  assert.equal(h.actor.snapshot().serverId, 'B'); assert.equal(h.reads.length, before);
});

test('an old A receipt cannot confirm after HTTP has already moved to B', async () => {
  const h = setup({ snapshot: async () => ({ ...snapshotPage(), serverId: 'B', cursor: 100 }) });
  await h.actor.catchUp();
  await assert.rejects(h.actor.catchUp(10, false, 'A'), /同步服务已变更/);
  assert.equal(h.actor.snapshot().serverId, 'B');
});

test('duplicate records across snapshot pages never publish a partial replacement', async () => {
  const expiry = Date.now(), base = { ...snapshotPage(), total: 2, expiresAt: expiry };
  const h = setup({ snapshot: async token => token ? { ...base, offset: 1, nextPageToken: null } : { ...base, nextPageToken: 'next' } });
  await assert.rejects(h.actor.catchUp(), /重复票根/); assert.equal(h.actor.snapshot().ready, false);
});

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { registerHooks } from 'node:module';
import { validShowtime, validateData } from '../src/data.ts';
import { sampleItem } from './web-fixtures.mjs';

const hook = registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL?.includes('/src/') && specifier.startsWith('.') && !specifier.endsWith('.ts')) return { url: new URL(specifier + '.ts', context.parentURL).href, shortCircuit: true };
  return nextResolve(specifier, context);
} });
const { ticketMonth, monthLabel, orderedTickets, ticketCollections, RECENT_TICKET_LIMIT, HOME_MONTH_LIMIT, MONTH_PREVIEW_LIMIT } = await import('../src/features/tickets/collections.ts');
hook.deregister();
const item = (id, showtime = '2026-10-22T19:30', createdAt = 100) => ({ ...sampleItem(id), fields: { ...sampleItem(id).fields, showtime }, createdAt });
const ids = tickets => tickets.map(ticket => ticket.id);
const freeze = value => {
  if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); }
  return value;
};

test('valid wall-clock showtimes take priority over creation month, including early years and leap days', () => {
  for (const [showtime, month] of [['0001-01-01T00:00', '0001-01'], ['0099-12-31T23:59', '0099-12'],
    ['2000-02-29T12:00', '2000-02'], ['2024-02-29T23:59', '2024-02'], ['9999-12-31T23:59', '9999-12']]) {
    assert.equal(validShowtime(showtime), true);
    assert.equal(ticketMonth(showtime, Date.parse('2026-06-15T12:00:00Z')), month);
    assert.equal(ticketMonth(showtime, Number.NaN), month);
  }
});

test('empty or invalid showtimes fall back instead of normalizing invalid calendar fields', () => {
  const createdAt = Date.parse('2026-06-15T12:00:00Z');
  for (const showtime of ['', 'bad', '2026-02-30T12:00', '1900-02-29T12:00', '2026-00-01T12:00', '2026-13-01T12:00',
    '2026-04-31T12:00', '2026-01-01T24:00', '2026-01-01T12:60', '0000-01-01T00:00',
    '2026-6-15T12:00', '2026-06', '2026-06-15', '2026-06-15T12:00Z', '2026-06-15T12:00+08:00', '2026-06-15T12:00:00']) {
    if (showtime) assert.equal(validShowtime(showtime), false, showtime);
    assert.equal(ticketMonth(showtime, createdAt), '2026-06', showtime);
  }
  assert.equal(validShowtime(''), true); // Existing optional-field contract is unchanged.
});

for (const [zone, expected] of [
  ['UTC', ['2026-11', '2026-03', '2026-02']],
  ['Asia/Macau', ['2026-11', '2026-03', '2026-03']],
  ['America/Los_Angeles', ['2026-10', '2026-02', '2026-02']],
]) test(`creation fallback uses the actual client timezone in ${zone}`, () => {
  const script = `
    import { registerHooks } from 'node:module';
    const hook = registerHooks({ resolve(specifier, context, nextResolve) {
      if (context.parentURL?.includes('/src/') && specifier.startsWith('.') && !specifier.endsWith('.ts')) return { url: new URL(specifier + '.ts', context.parentURL).href, shortCircuit: true };
      return nextResolve(specifier, context);
    } });
    const { ticketMonth } = await import(${JSON.stringify(new URL('../src/features/tickets/collections.ts', import.meta.url).href)});
    hook.deregister();
    const boundary = ['2026-11-01T00:30:00Z', '2026-03-01T00:30:00Z', '2026-02-28T17:30:00Z'].map(value => ticketMonth('', Date.parse(value)));
    const dstDates = ['2026-03-08T09:59:00Z', '2026-03-08T10:01:00Z', '2026-11-01T08:59:00Z', '2026-11-01T09:01:00Z'];
    const dst = dstDates.map(value => ticketMonth('', Date.parse(value)));
    const offsets = dstDates.map(value => new Date(value).getTimezoneOffset());
    const wallClock = ['2026-03-08T02:30', '2026-11-01T01:30', '2026-03-01T00:01'].map(value => ticketMonth(value, 0));
    console.log(JSON.stringify({ boundary, dst, offsets, wallClock, earlyFallback: ticketMonth('', Date.parse('0099-07-15T12:00:00Z')) }));
  `;
  const result = JSON.parse(execFileSync(process.execPath, ['--input-type=module', '-e', script], { env: { ...process.env, TZ: zone }, encoding: 'utf8' }));
  assert.deepEqual(result.boundary, expected);
  assert.deepEqual(result.dst, ['2026-03', '2026-03', '2026-11', '2026-11']);
  assert.deepEqual(result.wallClock, ['2026-03', '2026-11', '2026-03']);
  assert.equal(result.earlyFallback, '0099-07');
  if (zone === 'America/Los_Angeles') assert.deepEqual(result.offsets, [480, 420, 420, 480]);
});

test('unrepresentable creation dates do not throw, invent a month or leave the library', () => {
  const invalid = [Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY, Number.MAX_SAFE_INTEGER,
    8_640_000_000_000_001, 1.5, Date.parse('0000-06-15T12:00:00Z'), Date.parse('+010000-06-15T12:00:00Z')];
  const tickets = invalid.map((createdAt, index) => item(`invalid-${index}`, '', createdAt));
  for (const createdAt of invalid) assert.equal(ticketMonth('', createdAt), null);
  const result = ticketCollections(tickets);
  assert.equal(result.tickets.length, invalid.length); assert.equal(result.recent.length, invalid.length);
  assert.equal(result.undated.length, invalid.length); assert.deepEqual(result.months, []);
  const contractInvalid = item('contract-invalid', '', Number.MAX_SAFE_INTEGER);
  validateData({ tickets: [contractInvalid] }); // Existing accepted integer range is not tightened.
  assert.equal(ticketCollections([contractInvalid]).undated[0], contractInvalid);
  assert.deepEqual(ids(orderedTickets([item('b', '', NaN), item('a', '', Infinity)])), ['a', 'b']);
});

test('month labels retain padded early years and reject invalid month keys', () => {
  assert.equal(monthLabel('2026-10'), '2026年10月'); assert.equal(monthLabel('2026-01'), '2026年1月');
  assert.equal(monthLabel('0001-02'), '0001年2月');
  for (const month of ['', '0000-01', '2026-00', '2026-13', '26-01', '2026-1', '2026-01/extra']) assert.equal(monthLabel(month), null);
});

test('both creation-time orders use Android ASCII id ties, independent of input order', () => {
  const tied = ['a', '_', '9', 'Z', '-', 'A', '0', 'z'].map(id => item(id));
  const expected = ['-', '0', '9', 'A', 'Z', '_', 'a', 'z'];
  const tickets = [...tied, item('newer', undefined, 200), item('older', undefined, 50)];
  assert.deepEqual(ids(orderedTickets(tickets)), ['newer', ...expected, 'older']);
  assert.deepEqual(ids(orderedTickets([...tickets].reverse(), 'oldest')), ['older', ...expected, 'newer']);
  assert.deepEqual(ids(ticketCollections(tied).months[0].tickets), expected);
});

test('collections omit tombstones and derive newest months and all active tickets without mutation', () => {
  const tickets = freeze([item('late-added-old-month', '2025-12-01T12:00', 300), item('second', '2026-11-01T12:00', 100),
    { ...item('deleted', '2027-01-01T00:00', 400), deletedAt: 500 }, item('first', '2026-11-01T12:00', 200)]);
  const result = ticketCollections(tickets);
  assert.deepEqual(result.months.map(({ month, label }) => [month, label]), [['2026-11', '2026年11月'], ['2025-12', '2025年12月']]);
  assert.deepEqual(ids(result.months[0].tickets), ['first', 'second']);
  assert.deepEqual(ids(result.recent), ['late-added-old-month', 'first', 'second']);
  assert.deepEqual(ids(result.tickets), ids(result.recent)); assert.deepEqual(result.undated, []);
  assert.deepEqual(ids(tickets), ['late-added-old-month', 'second', 'deleted', 'first']);
  assert.equal(result.tickets[0], tickets[0]); // Preserve original ticket/media identity for existing consumers.
});

test('more than six months remain available and recent items have their own ten-ticket limit', () => {
  const tickets = Array.from({ length: 14 }, (_, index) => item(`ticket-${index}`, `2026-${String(index % 7 + 1).padStart(2, '0')}-15T12:00`, index));
  const result = ticketCollections(tickets.reverse());
  assert.equal(RECENT_TICKET_LIMIT, 10); assert.equal(HOME_MONTH_LIMIT, 6); assert.equal(MONTH_PREVIEW_LIMIT, 2);
  assert.equal(result.recent.length, 10); assert.equal(result.tickets.length, 14); assert.equal(result.months.length, 7);
  assert.deepEqual(result.months.map(group => group.month), ['2026-07', '2026-06', '2026-05', '2026-04', '2026-03', '2026-02', '2026-01']);
  assert.equal(result.months.flatMap(group => group.tickets).length, 14);
  assert.equal(new Set(result.months.flatMap(group => ids(group.tickets))).size, 14);
});

test('new snapshots immediately derive additions, date migrations, deletion and separate accounts', () => {
  const original = freeze([item('a', '2026-10-01T12:00', 100)]);
  const before = ticketCollections(original);
  const migrated = { ...original[0], version: 2, fields: { ...original[0].fields, showtime: '2026-11-01T12:00' } };
  const after = ticketCollections([migrated, item('new', '2026-12-01T12:00', 200)]);
  assert.deepEqual(after.months.map(group => group.month), ['2026-12', '2026-11']);
  assert.deepEqual(ids(after.recent), ['new', 'a']);
  assert.deepEqual(ticketCollections([{ ...migrated, deletedAt: 300 }]).months, []);
  assert.deepEqual(before.months.map(group => group.month), ['2026-10']);
  assert.deepEqual(ticketCollections([]), { tickets: [], recent: [], months: [], undated: [] });
  assert.deepEqual(ids(ticketCollections([item('account-b', '2025-01-01T12:00')]).tickets), ['account-b']);
});

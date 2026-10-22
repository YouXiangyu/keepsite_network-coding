import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateData } from '../src/data.ts';
import { sampleData, media } from './web-fixtures.mjs';

test('empty ticket libraries and unprocessed saved originals are valid', () => {
  validateData({ tickets: [] }); validateData(sampleData());
  const data = sampleData(); data.tickets[0].media.original = media('not_configured'); validateData(data);
});
test('media cannot claim unrelated origins or malformed hashes', () => {
  const data = sampleData(); data.tickets[0].media.original.url = 'https://evil.test/photo';
  assert.throws(() => validateData(data));
});
test('duplicate identities, missing sources, unsupported processing and invalid tombstones are rejected', () => {
  for (const change of [data => data.tickets.push(data.tickets[0]), data => delete data.tickets[0].fieldSources.title,
    data => { data.tickets[0].processing.status = 'ready'; }, data => { data.tickets[0].deletedAt = -1; }]) {
    const data = sampleData(); change(data); assert.throws(() => validateData(data));
  }
});

test('field lengths, calendar dates and operation identifiers follow the server contract', () => {
  for (const change of [data => { data.tickets[0].fields.title = 'x'.repeat(121); },
    data => { data.tickets[0].fields.cinema = 'x'.repeat(201); }, data => { data.tickets[0].fields.showtime = '2026-02-30T12:00'; },
    data => { data.tickets[0].operationId = '../invalid'; }]) {
    const data = sampleData(); change(data); assert.throws(() => validateData(data));
  }
});

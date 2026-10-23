import { test } from 'node:test';
import assert from 'node:assert/strict';
import { validateData, validPosterSource } from '../src/data.ts';
import { processingActive, processingDescription } from '../src/features/tickets/processing.ts';
import { sampleData, media, posterSource } from './web-fixtures.mjs';

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
    data => { data.tickets[0].processing.status = 'unknown'; }, data => { data.tickets[0].deletedAt = -1; }]) {
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

test('processing stages and published poster provenance are validated together', () => {
  for (const status of ['not_started', 'queued', 'ocr', 'poster', 'no_match', 'failed']) {
    const data = sampleData(); data.tickets[0].processing = { status, error: status === 'failed' ? '可重试的失败' : null }; validateData(data);
  }
  const data = sampleData(); data.tickets[0].processing.status = 'ready';
  assert.throws(() => validateData(data));
  data.tickets[0].media.poster = { ...media(), source: posterSource() }; validateData(data);
  data.tickets[0].media.poster.cloudStatus = 'not_configured'; assert.throws(() => validateData(data));
});
test('no-match explains original-ticket fallback while active processing retains the current image', () => {
  assert.equal(processingActive('no_match'), false);
  assert.match(processingDescription('no_match'), /原票作为封面/);
  assert.doesNotMatch(processingDescription('no_match'), /已有封面|已有海报|旧封面/);
  for (const status of ['queued', 'ocr', 'poster']) assert.equal(processingActive(status), true);
  assert.match(processingDescription('poster'), /新封面发布前保留当前图片/);
});
test('failure description stays accurate when backup restoration retains a verified poster', () => {
  assert.equal(processingActive('failed'), false);
  assert.match(processingDescription('failed'), /原票照片仍被保留/);
  assert.doesNotMatch(processingDescription('failed'), /封面|海报|显示/);
});
test('poster links only accept approved Commons and matching license URLs', () => {
  assert.equal(validPosterSource(posterSource()), true);
  assert.equal(validPosterSource({ ...posterSource('Safety_Last_(1923)_poster.jpg'), sourceFile: 'File:Safety Last (1923) poster.jpg' }), true);
  const publicDomain = { ...posterSource(), licenseName: 'Public domain' };
  assert.equal(validPosterSource({ ...publicDomain, licenseUrl: publicDomain.sourcePage + '#Licensing' }), true);
  assert.equal(validPosterSource({ ...publicDomain, licenseUrl: 'https://creativecommons.org/publicdomain/mark/1.0/' }), true);
  for (const change of [
    { sourcePage: 'https://evil.test/wiki/File:Poster.png' }, { sourcePage: 'javascript:alert(1)' }, { sourceFile: 'File:Other_poster.png' },
    { sourcePage: 'https://commons.wikimedia.org:443/wiki/File:Poster.png' }, { sourcePage: posterSource().sourcePage + '?redirect=evil' },
    { licenseUrl: 'https://creativecommons.org/licenses/by/4.0/' }, { licenseName: 'All rights reserved' },
    { licenseUrl: 'https://creativecommons.org.evil.test/licenses/by-sa/4.0/' }, { sourceFile: 'https://example.test/image.png' },
  ]) assert.equal(validPosterSource({ ...posterSource(), ...change }), false);
});

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseRoute, listContext, listPath, detailPath, editPath } from '../src/app/routes.ts';

const via = from => '/tickets/item-1?' + new URLSearchParams({ from });

test('original routes and unrelated authentication queries keep their existing destinations', () => {
  for (const path of ['/', '/tickets', '/tickets?unknown=value', '/?from=//evil.test']) assert.deepEqual(parseRoute(path), { type: 'tickets' });
  assert.deepEqual(parseRoute('/devices?account_link=success&state=%E9%AA%8C%E8%AF%81'), { type: 'devices' });
  assert.deepEqual(parseRoute('/tickets/new?unrelated=yes'), { type: 'new' });
  assert.deepEqual(parseRoute('/tickets/item-1?unknown=yes'), { type: 'detail', id: 'item-1', from: '/tickets' });
  assert.deepEqual(parseRoute('/tickets/item-1/edit?account_link=error'), { type: 'edit', id: 'item-1', from: '/tickets' });
  for (const path of ['/missing', '/tickets/item-1/extra', '/tickets/item-1/edit/extra', '/tickets/%E0%A4%A']) assert.deepEqual(parseRoute(path), { type: 'not-found' });
});

test('mine, complete months and valid independent month routes are explicit', () => {
  assert.deepEqual(parseRoute('/mine?unknown=yes'), { type: 'mine' });
  assert.deepEqual(parseRoute('/months?account_link=success'), { type: 'months' });
  for (const month of ['0001-01', '0099-12', '2026-10', '9999-12']) {
    assert.deepEqual(parseRoute(`/months/${month}?unrelated=value`), { type: 'month', month });
    assert.deepEqual(listContext(`/months/${month}`), { path: `/months/${month}`, search: '', sort: 'recent' });
  }
  assert.deepEqual(parseRoute('/months/2026-%31%30'), { type: 'month', month: '2026-10' });
  assert.deepEqual(listContext('/months/2026-%31%30?search=%E7%94%B5%E5%BD%B1'), { path: '/months/2026-10', search: '电影', sort: 'recent' });
});

test('invalid months, extra segments and malformed encodings are not valid month routes', () => {
  for (const path of ['/months/0000-01', '/months/2026-00', '/months/2026-13', '/months/26-01', '/months/2026-1',
    '/months/2026-01-extra', '/months/2026-01/extra', '/months/2026-01%2Fextra', '/months/2026-01%0A',
    '/months/%', '/months/%E0%A4%A', '/months//2026-01', '/months/2026-01/', '/mine/extra']) {
    assert.deepEqual(parseRoute(path), { type: 'not-found' }, path);
  }
});

test('month routes and list contexts agree on literal prefixes and encoded month keys', () => {
  for (const path of ['/m%6fnths/2026-10', '/%6donths/2026-10']) {
    assert.deepEqual(parseRoute(path), { type: 'not-found' }, path);
    assert.equal(listContext(path), null, path);
  }
  for (const path of ['/months/2026-10', '/months/2026-%31%30', '/months/%32%30%32%36-10?search=October&sort=oldest']) {
    const route = parseRoute(path), context = listContext(path);
    assert.deepEqual(route, { type: 'month', month: '2026-10' }, path);
    assert.equal(context.path, `/months/${route.month}`, path);
    assert.deepEqual(parseRoute(listPath(context)), route, path);
  }
});

test('list context defaults are independent for the library and each month', () => {
  const library = listContext('/tickets?search=Library&sort=oldest');
  const first = listContext('/months/2026-10?search=October');
  assert.deepEqual(library, { path: '/tickets', search: 'Library', sort: 'oldest' });
  assert.deepEqual(first, { path: '/months/2026-10', search: 'October', sort: 'recent' });
  assert.deepEqual(listContext('/months/2026-09'), { path: '/months/2026-09', search: '', sort: 'recent' });
  assert.deepEqual(listContext('/'), { path: '/tickets', search: '', sort: 'recent' });
  for (const path of ['/mine', '/months', '/devices', '/tickets/item-1', '/months/2026-00']) assert.equal(listContext(path), null);
  first.search = 'changed'; assert.equal(library.search, 'Library');
});

test('list serialization canonically preserves Unicode, whitespace and query punctuation without mutation', () => {
  const context = Object.freeze({ path: '/months/2026-10', search: ' 电影 &影院 + 100%?/#=" 🎬 ', sort: 'oldest' });
  const path = listPath(context), url = new URL(path, 'https://keepsite.example');
  assert.equal(url.pathname, context.path); assert.equal(url.searchParams.get('search'), context.search);
  assert.equal(url.searchParams.get('sort'), 'oldest'); assert.equal(url.hash, '');
  assert.deepEqual(listContext(path), context); assert.equal(listPath(listContext(path)), path);
  assert.equal(listPath({ path: '/tickets', search: '', sort: 'recent' }), '/tickets');
  assert.equal(listPath({ path: '/', search: '', sort: 'oldest' }), '/tickets?sort=oldest');
});

test('unrelated top-level query values do not invalidate or overwrite known list options', () => {
  assert.deepEqual(listContext('/months/2026-10?search=%E5%BD%B1%E9%99%A2&sort=oldest&account_link=success&state=original'),
    { path: '/months/2026-10', search: '影院', sort: 'oldest' });
  assert.deepEqual(parseRoute('/devices?account_link=success&from=https://evil.test'), { type: 'devices' });
  assert.deepEqual(listContext('/tickets?search=a&search=b&sort=recent&sort=oldest'), { path: '/tickets', search: '', sort: 'recent' });
  assert.deepEqual(listContext('/tickets?search=a&sort=not-supported'), { path: '/tickets', search: 'a', sort: 'recent' });
});

test('allowed sources survive detail and edit helpers and fresh parsing with original filters', () => {
  const sources = ['/mine', '/months', '/tickets', '/months/2026-10',
    listPath({ path: '/tickets', search: '片名 & +', sort: 'oldest' }),
    listPath({ path: '/months/2026-10', search: '影院 /?# 🎬', sort: 'oldest' })];
  for (const from of sources) {
    const detail = parseRoute(detailPath('item-1', from));
    assert.deepEqual(detail, { type: 'detail', id: 'item-1', from });
    const edit = parseRoute(editPath(detail.id, detail.from));
    assert.deepEqual(edit, { type: 'edit', id: 'item-1', from });
    assert.deepEqual(parseRoute(detailPath(edit.id, edit.from)), detail);
    assert.equal(parseRoute(new URL(detailPath('item-1', from), 'https://keepsite.example').href).from, from);
  }
  assert.equal(detailPath('item-1'), '/tickets/item-1'); assert.equal(editPath('item-1'), '/tickets/item-1/edit');
});

test('original encoded ticket identities still decode once without becoming path or query delimiters', () => {
  const id = '票根 + ?/#%';
  assert.deepEqual(parseRoute(detailPath(id, '/mine')), { type: 'detail', id, from: '/mine' });
  assert.deepEqual(parseRoute(editPath(id, '/months')), { type: 'edit', id, from: '/months' });
  assert.equal(parseRoute('/tickets/a%252Fb').id, 'a%2Fb');
});

test('unsafe or unsupported source values fall back without rejecting the detail', () => {
  const unsafe = ['', 'https://evil.test/mine', 'https://keepsite.invalid/mine', '//evil.test/mine', 'javascript:alert(1)',
    '\\evil.test\mine', '/\\evil.test/mine', '/mine#fragment', '/mine\n', '/months/2026-01%0A',
    '/tickets/../mine', '/tickets/%2e%2e/mine', '/%2Fmine', '/%5Cmine', '/months/2026-00', '/months/2026-01/extra',
    '/devices', '/tickets/item-1', '/tickets/item-1/edit', '/mine?from=/months', '/tickets?from=/mine',
    '/mine?search=hidden', '/months?sort=oldest', '/tickets?unknown=x', '/tickets?sort=invalid',
    '/tickets?search=a&search=b', '/tickets?sort=recent&sort=oldest'];
  for (const from of unsafe) {
    assert.deepEqual(parseRoute(via(from)), { type: 'detail', id: 'item-1', from: '/tickets' }, from);
    assert.equal(detailPath('item-1', from), '/tickets/item-1', from);
    assert.equal(editPath('item-1', from), '/tickets/item-1/edit', from);
  }
});

test('duplicate, encoded duplicate and nested source parameters cannot override the safe default', () => {
  for (const query of ['from=/mine&from=/months', 'from=/mine&fr%6Fm=/months', 'from=&from=/mine',
    'from=%252Fmine', 'from=%2Ftickets%3Ffrom%3D%2Fmine']) {
    assert.equal(parseRoute('/tickets/item-1?' + query).from, '/tickets');
    assert.equal(parseRoute('/tickets/item-1/edit?' + query).from, '/tickets');
  }
});

test('source options canonicalize once while an unrelated outer query stays unrelated', () => {
  const path = via('/months/2026-10?sort=oldest&search=%E5%BD%B1%E9%99%A2%2B+%26');
  const source = '/months/2026-10?search=%E5%BD%B1%E9%99%A2%2B+%26&sort=oldest';
  assert.equal(parseRoute(path + '&account_link=success&state=original').from, source);
  assert.deepEqual(listContext(source), { path: '/months/2026-10', search: '影院+ &', sort: 'oldest' });
  assert.equal(parseRoute(via('/tickets?search=&sort=recent')).from, '/tickets');
  assert.equal(parseRoute(via('/months/2026-%31%30?sort=oldest')).from, '/months/2026-10?sort=oldest');
});

test('list URLs and source links stay pure without calling navigation or reading browser history', () => {
  assert.equal(typeof globalThis.window, 'undefined');
  const state = Object.freeze({ path: '/months/2026-10', search: '保存筛选', sort: 'oldest' });
  const source = listPath(state), detail = detailPath('a', source), edit = editPath('a', parseRoute(detail).from);
  assert.equal(parseRoute(edit).from, source); assert.deepEqual(listContext(source), state);
  assert.equal(parseRoute(detailPath('a', '/months/2026-10')).from, '/months/2026-10');
});

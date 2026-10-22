import { test } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { media, png } from './web-fixtures.mjs';
const hook = registerHooks({ resolve(specifier, context, nextResolve) {
  if (context.parentURL?.includes('/src/') && ['./api', './data'].includes(specifier)) return { url: new URL(specifier + '.ts', context.parentURL).href, shortCircuit: true };
  return nextResolve(specifier, context);
} });
const { CACHE_LIMIT, planCacheEviction, verifyMediaBlob } = await import('../src/mediaCache.ts');
hook.deregister();

test('the per-account budget evicts least-recent inactive images while keeping the current viewport', () => {
  assert.equal(CACHE_LIMIT, 128 * 1024 * 1024);
  const MiB = 1024 * 1024;
  const records = [
    { sha256: 'visible', size: 12 * MiB, accessedAt: 0 },
    { sha256: 'offscreen-old', size: 12 * MiB, accessedAt: 1 },
    { sha256: 'offscreen-new', size: 104 * MiB, accessedAt: 2 },
  ];
  assert.deepEqual(planCacheEviction(records, MiB, new Set(['visible'])), ['offscreen-old']);
  assert.deepEqual(planCacheEviction(records, 13 * MiB, new Set(['visible'])), ['offscreen-old', 'offscreen-new']);
  assert.deepEqual(planCacheEviction(records, MiB, new Set()), ['visible']);
});
test('an active working set larger than the budget falls back instead of evicting visible images', () => {
  const records = [{ sha256: 'visible', size: CACHE_LIMIT, accessedAt: 0 }];
  assert.throws(() => planCacheEviction(records, 1, new Set(['visible'])), error => error.name === 'QuotaExceededError');
  assert.deepEqual(planCacheEviction(records, 0, new Set(['visible'])), []);
});
test('usable blobs must match content hash, byte size and MIME', async () => {
  await verifyMediaBlob(new Blob([png], { type: 'image/png' }), media());
  await assert.rejects(verifyMediaBlob(new Blob([png], { type: 'image/jpeg' }), media()), /格式校验失败/);
  await assert.rejects(verifyMediaBlob(new Blob([new Uint8Array(png.length)], { type: 'image/png' }), media()), /完整性校验失败/);
  let checks = 0;
  await assert.rejects(verifyMediaBlob(new Blob([png], { type: 'image/png' }), media(), () => { if (++checks === 3) throw new DOMException('cancelled after digest', 'AbortError'); }), error => error.name === 'AbortError');
});

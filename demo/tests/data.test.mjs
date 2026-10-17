import { test } from 'node:test';
import assert from 'node:assert/strict';
import { inferTag, validateData } from '../src/data.ts';
import { sampleData, sampleItem } from './web-fixtures.mjs';

test('new subjects stay untagged instead of receiving an invented default', () => {
  assert.equal(inferTag('从未记录过的收藏', sampleData().collections), undefined);
  assert.equal(inferTag('  ', sampleData().collections), undefined);
});
test('matching names choose the most frequently supplied tag without changing records', () => {
  const item = sampleItem();
  const items = [
    { ...item, id: '1', title: '霸气桃桃', tags: ['果茶'] },
    { ...item, id: '2', title: '霸气桃桃', tags: ['果茶'] },
    { ...item, id: '3', title: '霸气桃桃', tags: ['饮品'] },
  ];
  assert.equal(inferTag('霸气桃桃', items), '果茶');
  assert.deepEqual(items[2].tags, ['饮品']);
});
test('real model and source-image fields are accepted for community and collections', () => {
  assert.doesNotThrow(() => validateData(sampleData()));
});
test('ready content without a model URL is rejected', () => {
  const data = sampleData();
  delete data.community[0].modelUrl;
  assert.throws(() => validateData(data), /格式异常/);
});
test('account asset URLs cannot point to an unrelated origin', () => {
  const data = sampleData();
  data.collections[0].customImage = 'https://unrelated.example/image';
  assert.throws(() => validateData(data), /格式异常/);
});
test('duplicate collection identities are rejected before updating the UI', () => {
  const data = sampleData();
  data.collections.push({ ...data.collections[0] });
  assert.throws(() => validateData(data), /重复标识/);
});

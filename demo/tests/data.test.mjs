import { test } from 'node:test';
import assert from 'node:assert/strict';
import { inferTag, makeSeedData } from '../src/data.ts';

test('a new name stays untagged instead of receiving an invented default', () => {
  assert.equal(inferTag('从未记录过的收藏', makeSeedData().collections), undefined);
  assert.equal(inferTag('  ', makeSeedData().collections), undefined);
});

test('matching names use the most frequently supplied label', () => {
  const sample = makeSeedData().collections[0];
  const items = [
    { ...sample, id: '1', title: '霸气桃桃', tags: ['果茶'] },
    { ...sample, id: '2', title: '霸气桃桃', tags: ['果茶'] },
    { ...sample, id: '3', title: '霸气桃桃', tags: ['饮品'] },
    { ...sample, id: '4', title: '桂花拿铁', tags: ['咖啡'] },
  ];
  assert.equal(inferTag('霸气桃桃', items), '果茶');
  assert.deepEqual(items[2].tags, ['饮品']);
});

test('received example copies are independent from the original collection', () => {
  const data = makeSeedData();
  const original = data.collections.find(i => i.title === data.shares[0].item.title);
  const originalTags = [...original.tags];
  data.shares[0].item.tags.push('自己的备注');
  assert.deepEqual(original.tags, originalTags);
  data.collections.length = 0;
  assert.ok(data.shares[0].item.title);
});

test('a fresh workspace does not share arrays with another workspace', () => {
  const a = makeSeedData(), b = makeSeedData();
  a.collections[0].tags.push('测试');
  a.profile.name = '测试用户';
  assert.equal(b.profile.name, '小宇');
  assert.ok(!b.collections[0].tags.includes('测试'));
});

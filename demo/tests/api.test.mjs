import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { registerHooks } from 'node:module';
import { makeSeedData, validateData } from '../src/data.ts';

// Node 24 strips TypeScript; resolve the app's extensionless Vite import for Node.
const apiUrl = new URL('../src/api.ts', import.meta.url).href;
const hook = registerHooks({
  resolve(specifier, context, nextResolve) {
    if (context.parentURL === apiUrl && specifier === './data') {
      return { url: new URL('../src/data.ts', import.meta.url).href, shortCircuit: true };
    }
    return nextResolve(specifier, context);
  },
});
const { planInitialization, getState, mutate, uploadMedia, ApiError, API_BASE } = await import(apiUrl);
hook.deregister();
const originalFetch = globalThis.fetch;
after(() => { globalThis.fetch = originalFetch; });
const state = () => ({ initialized: true, serverId: 'local-server', revision: 7, data: makeSeedData() });

test('migration preserves existing snapshots and retries with the same identity', () => {
  const data = makeSeedData();
  data.collections[0].title = '我的旧收藏';
  data.collections[0].customImage = 'data:image/png;base64,aGVsbG8=';
  const source = { migrationId: 'stable-id', completedServerIds: [], data };
  const first = planInitialization(source, { initialized: false, serverId: 'local-server' });
  const retry = planInitialization(source, { initialized: true, serverId: 'local-server' });
  assert.equal(first.migrationId, retry.migrationId);
  assert.equal(first.data, data);
  assert.equal(retry.data.collections[0].customImage, data.collections[0].customImage);
});

test('completed migration never overwrites later server changes or silently reseeds', () => {
  const source = { migrationId: 'stable-id', completedServerIds: ['local-server'], data: makeSeedData() };
  assert.equal(planInitialization(source, { initialized: true, serverId: 'local-server' }), null);
  assert.throws(() => planInitialization(source, { initialized: false, serverId: 'local-server' }), /不会重新写入/);
});

test('only a genuinely empty workspace without legacy data receives seed content', () => {
  const source = { migrationId: 'new-browser', completedServerIds: [] };
  assert.equal(planInitialization(source, { initialized: true, serverId: 'existing-server' }), null);
  assert.equal(planInitialization(source, { initialized: false, serverId: 'empty-server' }).data.collections.length, 14);
});

test('legacy pending state can be validated for migration but is not an API state', () => {
  const data = makeSeedData();
  delete data.collections[0].version;
  data.collections[0].generation = 'pending';
  assert.doesNotThrow(() => validateData(data, true));
  assert.throws(() => validateData(data), /格式异常/);
});

test('API state resolves media URLs without changing record IDs', async () => {
  const response = state();
  response.data.collections[0].customImage = '/api/media/photo';
  response.data.collections[0].modelUrl = '/api/models/model.glb';
  globalThis.fetch = async () => Response.json(response);
  const result = await getState();
  assert.equal(result.data.collections[0].id, response.data.collections[0].id);
  assert.equal(result.data.collections[0].customImage, `${API_BASE}/api/media/photo`);
  assert.equal(result.data.collections[0].modelUrl, `${API_BASE}/api/models/model.glb`);
});

test('resource mutations send explicit JSON and local client header', async () => {
  globalThis.fetch = async (url, options) => {
    assert.equal(url, `${API_BASE}/api/collections/abc`);
    assert.equal(options.method, 'PATCH');
    assert.equal(options.headers.get('X-MemoVerse-Client'), 'local');
    assert.deepEqual(JSON.parse(options.body), { version: 2, title: '保留草稿' });
    return Response.json(state());
  };
  assert.equal((await mutate('/api/collections/abc', 'PATCH', { version: 2, title: '保留草稿' })).revision, 7);
});

test('conflicts and service failures reject instead of returning fake success', async () => {
  globalThis.fetch = async () => Response.json({ detail: '版本已更新' }, { status: 409 });
  await assert.rejects(() => mutate('/api/collections/abc', 'PATCH', {}), error => error instanceof ApiError && error.status === 409 && error.message === '版本已更新');
  globalThis.fetch = async () => { throw new TypeError('connection refused'); };
  await assert.rejects(() => getState(), /无法连接本机服务/);
});

test('uploads carry the original file as multipart, not a Data URL JSON payload', async () => {
  const file = new File([new Uint8Array([137, 80, 78, 71])], 'memory.png', { type: 'image/png' });
  globalThis.fetch = async (url, options) => {
    assert.equal(url, `${API_BASE}/api/media`);
    assert.ok(options.body instanceof FormData);
    assert.equal(options.body.get('file'), file);
    assert.equal(options.headers.get('Content-Type'), null);
    assert.equal(options.headers.get('X-MemoVerse-Client'), 'local');
    return Response.json({ assetId: 'asset-1', url: '/api/media/asset-1', sha256: 'hash', size: 4, width: 1, height: 1, mimeType: 'image/png' });
  };
  const result = await uploadMedia(file);
  assert.equal(result.assetId, 'asset-1');
  assert.equal(result.url, `${API_BASE}/api/media/asset-1`);
});

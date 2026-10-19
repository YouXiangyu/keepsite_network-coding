import { after, before, test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import { mkdir, mkdtemp, readFile, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from '../../demo/node_modules/@playwright/test/index.mjs';

const root = fileURLToPath(new URL('../../', import.meta.url));
const source = fileURLToPath(new URL('./', import.meta.url));
const cache = path.resolve(process.env.RUNNER_TEMP || path.join(root, '.cache'));
let temporary;
let server;
let browser;
let origin;

before(async () => {
  await mkdir(cache, { recursive: true });
  temporary = await mkdtemp(path.join(cache, 'lab-ui-test-'));
  execFileSync(process.execPath, [path.join(root, 'demo/node_modules/rolldown/bin/cli.mjs'),
    path.join(source, 'lab.js'), '--format', 'esm', '--platform', 'browser', '--file', path.join(temporary, 'lab.bundle.js')],
  { cwd: root, windowsHide: true, stdio: 'pipe' });
  const files = new Map([
    ['/', ['text/html', await readFile(path.join(source, 'lab.html'))]],
    ['/styles.css', ['text/css', await readFile(path.join(source, 'lab.css'))]],
    ['/lab.bundle.js', ['text/javascript', await readFile(path.join(temporary, 'lab.bundle.js'))]],
  ]);
  server = createServer((request, response) => {
    const file = files.get(request.url);
    if (!file) { response.writeHead(404).end(); return; }
    response.writeHead(200, { 'Content-Type': file[0] }).end(file[1]);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({ channel: 'chromium' });
});

after(async () => {
  await browser?.close();
  if (server) {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
  if (temporary) {
    assert.equal(path.dirname(temporary), path.resolve(cache));
    assert.ok(path.basename(temporary).startsWith('lab-ui-test-'));
    await rm(temporary, { recursive: true, force: true });
  }
});

const defaults = { input_side: 512, scales: [1], mirror: false, background_flatten: 1, mask_mode: 'auto', use_cache: true };
const config = {
  defaults,
  groups: [{ id: 'depth', label: '参数', description: '测试参数' }],
  fields: [{ key: 'input_side', label: '深度输入最长边', group: 'depth', kind: 'integer', min: 128, max: 1536, step: 64, impact: 'depth', help: '测试参数' }],
  presets: [{ id: 'test', label: '测试预设', parameters: { ...defaults, input_side: 1024 } }],
};
const job = (id, inputSide) => ({
  id, title: `历史 ${id}`, status: 'ready', image_id: id, image_name: `照片 ${id}`, mask_image_id: null,
  parameters: { ...defaults, input_side: inputSide }, boxes: [[0.1, 0.1, 0.8, 0.8]], files: {},
  created_at: '2026-10-19T00:00:00Z', started_at: '2026-10-19T00:00:00Z', finished_at: '2026-10-19T00:00:01Z',
});

async function withPendingHistory(run, prepare = async () => {}) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  let release;
  const held = new Promise(resolve => { release = resolve; });
  let requested;
  const started = new Promise(resolve => { requested = resolve; });
  const jobs = [job('a', 128), job('b', 256)];
  await page.route('**/api/**', async route => {
    const pathname = new URL(route.request().url()).pathname;
    const respond = value => route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) });
    if (pathname === '/api/bootstrap') return respond({
      token: 'test', config, default_image_id: 'current', references: [], runs: jobs, active_run_id: null,
      images: ['current', 'a', 'b'].map(id => ({ id, kind: 'image', name: `照片 ${id}`, width: 400, height: 300, preview_url: `/api/images/${id}/preview` })),
    });
    if (pathname === '/api/runs/a') { requested(); await held; return respond({ ...jobs[0], report: {} }).catch(() => {}); }
    if (pathname === '/api/runs/b') return respond({ ...jobs[1], report: {} });
    if (pathname.endsWith('/preview')) return route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="300"><rect width="400" height="300" fill="#aabbcc"/></svg>' });
    throw new Error(`Unexpected API request: ${pathname}`);
  });
  try {
    await page.goto(origin);
    await started;
    await prepare(page);
    await page.locator('#load-parameters').click();
    const finishOldRead = async () => {
      const response = page.waitForResponse(value => new URL(value.url()).pathname === '/api/runs/a');
      release();
      await (await response).finished();
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    };
    await run(page, finishOldRead);
  } finally {
    release();
    await context.close();
  }
}

test('the actual history loader keeps B when the older A response arrives last', async () => {
  await withPendingHistory(async (page, finishOldRead) => {
    await page.locator('#history-select').selectOption('b');
    await page.locator('#load-parameters').click();
    await page.waitForFunction(() => document.getElementById('parameter-input_side').value === '256');
    await finishOldRead();
    assert.equal(await page.locator('#parameter-input_side').inputValue(), '256');
    assert.equal(await page.locator('#image-select').inputValue(), 'b');
    assert.equal(await page.locator('#history-select').inputValue(), 'b');
  });
});

for (const [name, change, expectedSide, expectedImage, expectedLeft, prepare] of [
  ['edited parameter', page => page.locator('#parameter-input_side').fill('768'), '768', 'current', null],
  ['invalid edited parameter', page => page.locator('#parameter-input_side').fill(''), '', 'current', null],
  ['new photo selection', page => page.locator('#image-select').selectOption('b'), '512', 'b', null],
  ['preset selection', page => page.locator('#preset-buttons button').click(), '1024', 'current', null],
  ['added keep box', page => page.locator('#add-box').click(), '512', 'current', '25'],
  ['edited box coordinate', page => page.getByRole('spinbutton', { name: '保留框 1 左 %', exact: true }).fill('30'),
    '512', 'current', '30', page => page.locator('#add-box').click()],
]) {
  test(`a pending history load cannot replace ${name}`, async () => {
    await withPendingHistory(async (page, finishOldRead) => {
      await change(page);
      await finishOldRead();
      assert.equal(await page.locator('#parameter-input_side').inputValue(), expectedSide);
      assert.equal(await page.locator('#image-select').inputValue(), expectedImage);
      if (expectedLeft) assert.equal(await page.getByRole('spinbutton', { name: '保留框 1 左 %', exact: true }).inputValue(), expectedLeft);
      else assert.equal(await page.locator('#box-count').textContent(), '0 / 8');
    }, prepare);
  });
}

test('starting a drawn keep box invalidates the history load before the drag finishes', async () => {
  await withPendingHistory(async (page, finishOldRead) => {
    await page.waitForFunction(() => !document.getElementById('source-image').hidden);
    await page.locator('#image-stage').evaluate(element => element.scrollIntoView({ block: 'center' }));
    const bounds = await page.locator('#image-stage').boundingBox();
    assert.equal(await page.evaluate(({ x, y }) => Boolean(document.elementFromPoint(x, y)?.closest('#image-stage')),
      { x: bounds.x + bounds.width * 0.2, y: bounds.y + bounds.height * 0.2 }), true);
    await page.mouse.move(bounds.x + bounds.width * 0.2, bounds.y + bounds.height * 0.2);
    await page.mouse.down();
    await finishOldRead();
    await page.mouse.move(bounds.x + bounds.width * 0.7, bounds.y + bounds.height * 0.7);
    await page.mouse.up();
    assert.equal(await page.locator('#image-select').inputValue(), 'current');
    assert.equal(await page.locator('#parameter-input_side').inputValue(), '512');
    assert.equal(await page.locator('#box-count').textContent(), '1 / 8');
  });
});

test('selecting an upload invalidates the old history load before the upload response arrives', async () => {
  await withPendingHistory(async (page, finishOldRead) => {
    let releaseUpload;
    const upload = new Promise(resolve => { releaseUpload = resolve; });
    await page.route('**/api/images?**', async route => {
      await upload;
      await route.fulfill({ contentType: 'application/json', body: JSON.stringify({
        id: 'b', kind: 'image', name: '新照片', width: 400, height: 300, preview_url: '/api/images/b/preview',
      }) });
    });
    try {
      await page.locator('#image-file').setInputFiles({ name: 'new.png', mimeType: 'image/png', buffer: Buffer.from('test upload') });
      await finishOldRead();
      assert.equal(await page.locator('#image-select').inputValue(), 'current');
      assert.equal(await page.locator('#parameter-input_side').inputValue(), '512');
      releaseUpload();
      await page.waitForFunction(() => document.getElementById('image-select').value === 'b');
      assert.equal(await page.locator('#parameter-input_side').inputValue(), '512');
    } finally { releaseUpload(); }
  });
});

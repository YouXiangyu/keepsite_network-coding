import { expect, test, type APIRequestContext, type Page } from '@playwright/test';

const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=', 'base64');
const status = async (request: APIRequestContext) => (await request.get('/__test/status')).json();
const hang = async (request: APIRequestContext, path: string, method = 'GET') => {
  await request.post('/__test/hang', { data: { path, method } });
};
async function stalled(request: APIRequestContext, path: string) {
  await expect.poll(async () => (await status(request)).stalls.some((item: any) => item.pathname === path)).toBeTruthy();
}
async function waitForHeaders(page: Page, path: string, action: () => Promise<void>) {
  const headers = page.waitForResponse(response => new URL(response.url()).pathname === path && response.status() === 200);
  await action();
  return headers;
}

test.beforeEach(async ({ request }) => { await request.post('/__test/reset'); });

test('a failed production lazy module preserves the page and reload clears the rejected module cache', async ({ page }) => {
  let block = true;
  let requests = 0;
  await page.route('**/assets/ReliefViewer-*.js', async route => {
    requests += 1;
    if (block) await route.abort();
    else await route.continue();
  });
  await page.goto('/collection/item-1');
  await expect(page.getByRole('alert')).toContainText('浮雕查看器未能加载');
  await expect(page.getByRole('heading', { name: 'A 的海边', exact: true })).toBeVisible();
  await expect(page.locator('.detail-stage img')).toBeVisible();
  expect(requests).toBeGreaterThan(0);
  const rejectedRequests = requests;
  block = false;
  await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: '我的收藏', exact: true }).click();
  await page.locator('a[href="/collection/item-2"]').first().click();
  await expect(page.getByRole('alert')).toContainText('浮雕查看器未能加载');
  expect(requests).toBe(rejectedRequests);
  await page.getByRole('button', { name: '重新加载页面', exact: true }).click();
  await expect(page.getByRole('button', { name: '浮雕向左', exact: true })).toBeEnabled();
  expect(requests).toBeGreaterThan(rejectedRequests);
  await expect(page.getByRole('heading', { name: '第二件收藏', exact: true })).toBeVisible();
});

test('JSON save body timeout releases the form, retains its photo and draft, and a retry reuses the same id', async ({ page, request }) => {
  await page.clock.install();
  await page.goto('/add');
  await page.getByLabel('选择收藏图片').setInputFiles({ name: 'photo.png', mimeType: 'image/png', buffer: png });
  await page.getByRole('textbox', { name: '名称', exact: true }).fill('慢响应中的收藏');
  await page.getByRole('textbox', { name: /想记住的话/ }).fill('请求超时后仍保留的文字');
  await hang(request, '/api/collections', 'POST');
  await waitForHeaders(page, '/api/collections', () => page.getByRole('button', { name: '保存收藏', exact: true }).click());
  await stalled(request, '/api/collections');
  await expect(page.getByRole('button', { name: '正在保存…', exact: true })).toBeDisabled();
  await page.clock.fastForward(30_001);
  await expect(page.getByRole('alert')).toContainText('请求超时');
  await expect(page.getByRole('button', { name: '保存收藏', exact: true })).toBeEnabled();
  await expect(page.getByRole('textbox', { name: /想记住的话/ })).toHaveValue('请求超时后仍保留的文字');
  await expect(page.getByRole('img', { name: '收藏图片预览' })).toBeVisible();
  await page.getByRole('button', { name: '保存收藏', exact: true }).click();
  await expect(page.getByRole('heading', { name: '慢响应中的收藏', exact: true })).toBeVisible();
  const received = (await status(request)).requests;
  const creates = received.filter((item: any) => item.path === '/api/collections' && item.method === 'POST');
  expect(creates).toHaveLength(2);
  expect(creates[1].body.id).toBe(creates[0].body.id);
  expect(received.filter((item: any) => item.path === '/api/media' && item.method === 'POST')).toHaveLength(1);
  await request.post('/__test/release');
  await expect(page.getByRole('heading', { name: '慢响应中的收藏', exact: true })).toBeVisible();
});

test('backup body timeout releases the dialog lock and can be retried as a real download', async ({ page, request }) => {
  await page.clock.install();
  await page.goto('/profile');
  await page.getByRole('button', { name: '隐私与数据导出', exact: true }).click();
  await hang(request, '/api/backup');
  await waitForHeaders(page, '/api/backup', () => page.getByRole('button', { name: '导出账户备份', exact: true }).click());
  await stalled(request, '/api/backup');
  await expect(page.getByRole('button', { name: '导出账户备份', exact: true })).toBeDisabled();
  await page.clock.fastForward(120_001);
  await expect(page.getByRole('dialog').getByRole('alert')).toContainText('请求超时');
  await expect(page.getByRole('button', { name: '导出账户备份', exact: true })).toBeEnabled();
  const download = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出账户备份', exact: true }).click();
  expect((await download).suggestedFilename()).toBe('memoverse-backup.zip');
  await expect(page.getByRole('button', { name: '导出账户备份', exact: true })).toBeEnabled();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: '我的收藏', exact: true }).click();
  await expect(page.getByRole('heading', { name: '我的收藏', exact: true })).toBeVisible();
});

test('model body timeout retains the original image and retry displays the model', async ({ page, request }) => {
  await page.clock.install();
  await hang(request, '/api/models/item-1/model.glb');
  await waitForHeaders(page, '/api/models/item-1/model.glb', () => page.goto('/collection/item-1').then(() => {}));
  await stalled(request, '/api/models/item-1/model.glb');
  await page.clock.fastForward(30_001);
  await expect(page.getByRole('alert')).toContainText('浮雕暂时无法显示');
  await expect(page.locator('.relief-fallback')).toBeVisible();
  await page.getByRole('button', { name: '重新加载浮雕', exact: true }).click();
  await expect(page.getByRole('button', { name: '浮雕向左', exact: true })).toBeEnabled();
  await expect(page.locator('.relief-viewport canvas')).toBeVisible();
});

test('leaving a detail aborts a stalled model body and a late completion cannot restore its viewer', async ({ page, request }) => {
  await hang(request, '/api/models/item-1/model.glb');
  await waitForHeaders(page, '/api/models/item-1/model.glb', () => page.goto('/collection/item-1').then(() => {}));
  await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: '我的收藏', exact: true }).click();
  await expect.poll(async () => (await status(request)).stalls[0]?.closed).toBeTruthy();
  await request.post('/__test/release');
  await expect(page.getByRole('heading', { name: '我的收藏', exact: true })).toBeVisible();
  await expect(page.locator('.relief-viewer')).toHaveCount(0);
});

test('logout during a stalled save cannot publish the old response into a replacement account', async ({ page, request }) => {
  await page.goto('/collection/item-1/edit');
  await page.getByRole('textbox', { name: /想记住的话/ }).fill('旧账户的草稿');
  await hang(request, '/api/collections/item-1', 'PATCH');
  await waitForHeaders(page, '/api/collections/item-1', () => page.getByRole('button', { name: '保存收藏', exact: true }).click());
  await page.getByRole('button', { name: '退出登录', exact: true }).first().click();
  await expect(page.getByRole('heading', { name: '欢迎回来', exact: true })).toBeVisible();
  await expect.poll(async () => (await status(request)).stalls[0]?.closed).toBeTruthy();
  await page.getByLabel('邮箱地址', { exact: true }).fill('b@example.test');
  await page.getByLabel('密码', { exact: true }).fill('ExamplePassword123!');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.locator('.memoverse-shell')).toHaveAttribute('data-user-id', 'b');
  await request.post('/__test/release');
  await expect(page.locator('.memoverse-shell')).toHaveAttribute('data-user-id', 'b');
  await expect(page.getByRole('heading', { name: 'B 的海边', exact: true }).first()).toBeVisible();
  await expect(page.getByText('旧账户的草稿', { exact: true })).toHaveCount(0);
});

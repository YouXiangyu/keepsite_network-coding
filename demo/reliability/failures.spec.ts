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

test('JSON save body timeout releases the form, retains its photo and draft, and a retry reuses the same id', async ({ page, request }) => {
  await page.clock.install();
  await page.goto('/tickets/new');
  await page.getByLabel('选择原票照片').setInputFiles({ name: 'photo.png', mimeType: 'image/png', buffer: png });
  await page.getByLabel('片名').fill('慢响应中的票根');
  await page.getByLabel('影院').fill('请求超时后仍保留的文字');
  await hang(request, '/api/tickets', 'POST');
  await waitForHeaders(page, '/api/tickets', () => page.getByRole('button', { name: '保存票根', exact: true }).click());
  await stalled(request, '/api/tickets');
  await expect(page.getByRole('button', { name: '正在保存…', exact: true })).toBeDisabled();
  await page.clock.fastForward(29_000);
  await expect(page.getByRole('button', { name: '正在保存…', exact: true })).toBeDisabled();
  await page.clock.fastForward(1_001);
  await expect(page.getByRole('alert')).toContainText('请求超时');
  await expect(page.getByRole('button', { name: '保存票根', exact: true })).toBeEnabled();
  await expect(page.getByLabel('影院')).toHaveValue('请求超时后仍保留的文字');
  await expect(page.getByRole('img', { name: '原票预览' })).toBeVisible();
  await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: '慢响应中的票根', exact: true })).toBeVisible();
  const received = (await status(request)).requests;
  const creates = received.filter((item: any) => item.path === '/api/tickets' && item.method === 'POST');
  expect(creates).toHaveLength(2);
  expect(creates[1].body.id).toBe(creates[0].body.id);
  expect(creates[1].body.operationId).toBe(creates[0].body.operationId);
  expect(received.filter((item: any) => item.path === '/api/media' && item.method === 'POST')).toHaveLength(1);
  await request.post('/__test/release');
  await expect(page.getByRole('heading', { name: '慢响应中的票根', exact: true })).toBeVisible();
});

test('logout during a stalled save cannot publish the old response into a replacement account', async ({ page, request }) => {
  await page.goto('/tickets/item-1/edit');
  await page.getByLabel('影院').fill('旧账户的草稿');
  await hang(request, '/api/tickets/item-1', 'PATCH');
  await waitForHeaders(page, '/api/tickets/item-1', () => page.getByRole('button', { name: '保存票根', exact: true }).click());
  await page.getByRole('button', { name: '退出登录', exact: true }).first().click();
  await expect(page.getByRole('heading', { name: '欢迎回来', exact: true })).toBeVisible();
  await expect.poll(async () => (await status(request)).stalls[0]?.closed).toBeTruthy();
  await page.getByLabel('邮箱地址', { exact: true }).fill('b@example.test');
  await page.getByLabel('密码', { exact: true }).fill('ExamplePassword123!');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.locator('.memoverse-shell')).toHaveAttribute('data-user-id', 'b');
  await request.post('/__test/release');
  await expect(page.locator('.memoverse-shell')).toHaveAttribute('data-user-id', 'b');
  await expect(page.getByRole('heading', { name: 'B 的电影票', exact: true }).first()).toBeVisible();
  await expect(page.getByText('旧账户的草稿', { exact: true })).toHaveCount(0);
});

test('an already-created draft can recover from timeout, changed content and a create conflict without duplication', async ({ page, request }) => {
  await page.clock.install();
  await page.goto('/tickets/new');
  await page.getByLabel('选择原票照片').setInputFiles({ name: 'photo.png', mimeType: 'image/png', buffer: png });
  await page.getByLabel('片名').fill('超时后继续编辑');
  await page.getByLabel('影院').fill('第一次提交');
  await hang(request, '/api/tickets', 'POST');
  await waitForHeaders(page, '/api/tickets', () => page.getByRole('button', { name: '保存票根', exact: true }).click());
  await page.clock.fastForward(30_001);
  await expect(page.getByRole('alert')).toContainText('请求超时');
  await page.getByLabel('影院').fill('响应丢失后继续修改的草稿');
  await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('票根已创建');
  await page.getByRole('button', { name: '核对保存结果，保留草稿', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('再次保存会更新同一条记录');
  await expect(page.getByRole('heading', { name: '编辑票根', exact: true })).toBeVisible();
  await expect(page.getByLabel('影院')).toHaveValue('响应丢失后继续修改的草稿');
  await expect(page.getByRole('img', { name: '原票预览' })).toBeVisible();
  await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: '超时后继续编辑', exact: true })).toBeVisible();
  await expect(page.getByRole('definition').filter({ hasText: '响应丢失后继续修改的草稿' })).toBeVisible();
  const received = (await status(request)).requests;
  const creates = received.filter((item: any) => item.path === '/api/tickets' && item.method === 'POST');
  const updates = received.filter((item: any) => item.method === 'PATCH');
  expect(creates).toHaveLength(2);
  expect(updates).toHaveLength(1);
  expect(updates[0].path).toBe(`/api/tickets/${creates[0].body.id}`);
  expect(creates[1].body.id).toBe(creates[0].body.id);
  expect(updates[0].body.operationId).not.toBe(creates[0].body.operationId);
  expect(creates[1].body.operationId).not.toBe(creates[0].body.operationId);
  expect(received.filter((item: any) => item.path === '/api/media' && item.method === 'POST')).toHaveLength(1);
  const response = await page.request.get('/api/state');
  const saved = (await response.json()).data.tickets.filter((item: any) => item.id === creates[0].body.id);
  expect(saved).toHaveLength(1);
  expect(saved[0].version).toBe(2);
  expect(saved[0].fields.cinema).toBe('响应丢失后继续修改的草稿');
});

test('an uncertain edit can be reverted to its original value after reconciliation', async ({ page, request }) => {
  await page.clock.install(); await page.goto('/tickets/item-1/edit');
  await page.getByLabel('片名').fill('服务端已保存的新片名');
  await hang(request, '/api/tickets/item-1', 'PATCH');
  await waitForHeaders(page, '/api/tickets/item-1', () => page.getByRole('button', { name: '保存票根', exact: true }).click());
  await page.clock.fastForward(30001); await expect(page.getByRole('alert')).toContainText('请求超时');
  await page.getByLabel('片名').fill('A 的电影票');
  await page.getByRole('button', { name: '核对保存结果，保留草稿' }).click();
  await expect(page.getByLabel('片名')).toHaveValue('A 的电影票');
  await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'A 的电影票', exact: true })).toBeVisible();
  const data = await (await request.get('/api/state')).json();
  expect(data.data.tickets[0].fields.title).toBe('A 的电影票'); expect(data.data.tickets[0].version).toBe(3);
});
test('reconciliation merges untouched remote fields and never reattaches a confirmed old photo', async ({ page, request }) => {
  await page.clock.install(); await page.goto('/tickets/new');
  await page.getByLabel('选择原票照片').setInputFiles({ name: 'photo.png', mimeType: 'image/png', buffer: png });
  await page.getByLabel('片名').fill('自己的片名'); await hang(request, '/api/tickets', 'POST');
  await waitForHeaders(page, '/api/tickets', () => page.getByRole('button', { name: '保存票根', exact: true }).click());
  await page.clock.fastForward(30001); await expect(page.getByRole('alert')).toContainText('请求超时');
  const created = (await status(request)).requests.find((entry: any) => entry.path === '/api/tickets' && entry.method === 'POST').body;
  const remote = await request.patch(`/api/tickets/${created.id}`, { headers: { 'X-CSRF-Token': 'csrf-a' }, data: { operationId: 'remote-fields', fields: { cinema: '另一台设备填写的影院' } } }); expect(remote.ok()).toBeTruthy();
  const replacement = 'b'.repeat(64);
  const changedPhoto = await request.patch(`/api/tickets/${created.id}`, { headers: { 'X-CSRF-Token': 'csrf-a' }, data: { operationId: 'remote-photo', originalAssetId: replacement } }); expect(changedPhoto.ok()).toBeTruthy();
  await page.getByRole('button', { name: '核对保存结果，保留草稿' }).click();
  await expect(page.getByLabel('影院')).toHaveValue('另一台设备填写的影院');
  await expect(page.getByRole('img', { name: '原票预览' })).toHaveAttribute('src', '/api/media/' + replacement);
  await page.getByLabel('片名').fill('新的片名'); await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: '新的片名' })).toBeVisible();
  const writes = (await status(request)).requests.filter((entry: any) => entry.method === 'PATCH');
  expect(writes.at(-1).body.fields).toEqual({ title: '新的片名' }); expect(writes.at(-1).body.originalAssetId).toBeUndefined();
  const data = await (await request.get('/api/state')).json(); const ticket = data.data.tickets.find((entry: any) => entry.id === created.id);
  expect(ticket.fields.cinema).toBe('另一台设备填写的影院'); expect(ticket.media.original.assetId).toBe(replacement);
});
test('same-account CSRF renewal exposes reconciliation for an interrupted save', async ({ page, request }) => {
  await page.goto('/tickets/item-1/edit'); await page.getByLabel('片名').fill('服务端已保存的新片名');
  await hang(request, '/api/tickets/item-1', 'PATCH');
  await waitForHeaders(page, '/api/tickets/item-1', () => page.getByRole('button', { name: '保存票根', exact: true }).click());
  await request.post('/__test/renew', { data: { user: 'a' } });
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByRole('alert')).toContainText('保存请求已取消');
  await page.getByLabel('片名').fill('A 的电影票');
  await page.getByRole('button', { name: '核对保存结果，保留草稿' }).click();
  await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'A 的电影票', exact: true })).toBeVisible();
  const data = await (await request.get('/api/state')).json(); expect(data.data.tickets[0].fields.title).toBe('A 的电影票');
});

test('confirmed fields accept a later remote correction when the user made no new edit', async ({ page, request }) => {
  await page.clock.install(); await page.goto('/tickets/item-1/edit');
  await page.getByLabel('片名').fill('本端提交 B'); await hang(request, '/api/tickets/item-1', 'PATCH');
  await waitForHeaders(page, '/api/tickets/item-1', () => page.getByRole('button', { name: '保存票根', exact: true }).click());
  await page.clock.fastForward(30001); await expect(page.getByRole('alert')).toContainText('请求超时');
  const remote = await request.patch('/api/tickets/item-1', { headers: { 'X-CSRF-Token': 'csrf-a' }, data: { operationId: 'remote-correction', fields: { title: '另一端修正 C' } } }); expect(remote.ok()).toBeTruthy();
  await page.getByRole('button', { name: '核对保存结果，保留草稿' }).click();
  await expect(page.getByLabel('片名')).toHaveValue('另一端修正 C');
  await page.getByLabel('座位').fill('7 排 8 座'); await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: '另一端修正 C' })).toBeVisible();
  const updates = (await status(request)).requests.filter((entry: any) => entry.method === 'PATCH');
  expect(updates.at(-1).body.fields).toEqual({ seat: '7 排 8 座' });
});

test('an unapplied receipt preserves the operation identity for an unchanged retry', async ({ page, request }) => {
  await page.goto('/tickets/item-1/edit'); await page.getByLabel('影院').fill('未提交的影院');
  await request.post('/__test/reject', { data: { path: '/api/tickets/item-1', method: 'PATCH' } });
  await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('暂时无法提交');
  await page.getByRole('button', { name: '核对保存结果，保留草稿' }).click();
  await expect(page.getByRole('alert')).toContainText('上次提交尚未确认');
  await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'A 的电影票' })).toBeVisible();
  const writes = (await status(request)).requests.filter((entry: any) => entry.method === 'PATCH');
  expect(writes).toHaveLength(2); expect(writes[1].body.operationId).toBe(writes[0].body.operationId);
});
test('a confirmed create deleted elsewhere cannot be mistaken for an unsaved new draft', async ({ page, request }) => {
  await page.clock.install(); await page.goto('/tickets/new');
  await page.getByLabel('选择原票照片').setInputFiles({ name: 'photo.png', mimeType: 'image/png', buffer: png });
  await hang(request, '/api/tickets', 'POST'); await waitForHeaders(page, '/api/tickets', () => page.getByRole('button', { name: '保存票根', exact: true }).click());
  await page.clock.fastForward(30001); await expect(page.getByRole('alert')).toContainText('请求超时');
  const created = (await status(request)).requests.find((entry: any) => entry.path === '/api/tickets' && entry.method === 'POST').body;
  const removed = await request.delete(`/api/tickets/${created.id}`, { headers: { 'X-CSRF-Token': 'csrf-a' }, data: { operationId: 'remote-delete' } }); expect(removed.ok()).toBeTruthy();
  await page.getByRole('button', { name: '核对保存结果，保留草稿' }).click();
  await expect(page.getByRole('alert')).toContainText('已保存的票根已被删除');
  await expect(page.getByRole('button', { name: '保存票根', exact: true })).toBeDisabled();
});

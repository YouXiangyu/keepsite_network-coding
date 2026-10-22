import { expect, test, type Page, type Route } from '@playwright/test';
import { sampleItem, media, pngHash, otherHash, otherPng, imageFixture, posterSource } from '../tests/web-fixtures.mjs';
const password = 'ExamplePassword123!';
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=', 'base64');
async function mockApi(page: Page, options: { user?: string; configured?: boolean; google?: boolean; sessionGate?: Promise<void>; empty?: boolean; cloud?: boolean; processing?: boolean } = {}) {
  const server = {
    user: options.user ?? '', configured: options.configured ?? true, google: options.google ?? false,
    requests: [] as { path: string; method: string; body: any; headers: Record<string, string> }[],
    data: new Map<string, { tickets: ReturnType<typeof sampleItem>[] }>(), revision: 1,
    rejectVerification: false, unverifiedLogin: false, holdState: undefined as Promise<void> | undefined,
    csrfToken: undefined as string | undefined, logoutUrl: undefined as string | undefined,
    appliedOperations: new Set<string>(),
    uploads: new Map<string, { uploadId: string; status: string; asset: ReturnType<typeof media> }>(),
    processing: options.processing ?? false,
  };
  const currentData = (user = server.user) => {
    if (!server.data.has(user)) { const ticket = sampleItem(); ticket.fields.title = `${user.toUpperCase()} 的电影票`; ticket.media.original.cloudStatus = options.cloud === false ? 'not_configured' : 'saved'; server.data.set(user, { tickets: options.empty ? [] : [ticket] }); }
    return server.data.get(user)!;
  };
  const session = () => ({ authenticated: !!server.user, configured: server.configured, providers: { google: server.google, email: true },
    ...(server.user ? { user: { id: server.user, email: `${server.user}@example.test`, name: server.user }, csrfToken: server.csrfToken ?? `csrf-${server.user}` } : {}) });
  const state = () => ({ initialized: true, serverId: 'web-server', userId: server.user, revision: server.revision, data: structuredClone(currentData()), capabilities: { processing: server.processing, processingUnavailableReason: server.processing ? null : '处理配置未就绪，请配置服务端识别。', deviceSync: false, cloudStorage: options.cloud !== false } });
  const respond = (route: Route, value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
  await page.addInitScript(() => { (window as any).__legacyReads = 0; const open = indexedDB.open.bind(indexedDB); indexedDB.open = ((name: string, version?: number) => { if (name === 'memoverse-media-cache-v1') return open(name, version); (window as any).__legacyReads += 1; throw new Error('Legacy data must not be read'); }) as typeof indexedDB.open; });
  await page.route('**/api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname, method = request.method();
    let body: any; try { body = request.postDataJSON(); } catch { body = request.postData(); }
    server.requests.push({ path, method, body, headers: request.headers() });
    if (path === '/api/auth/session') { await options.sessionGate; return respond(route, session()).catch(() => {}); }
    if (path === '/api/auth/login') { if (server.unverifiedLogin) return respond(route, { detail: '请先验证邮箱。', code: 'email_unverified' }, 403); server.user = body.email.startsWith('b') ? 'b' : 'a'; return respond(route, session()); }
    if (path === '/api/auth/register') return respond(route, { verificationRequired: true, email: body.email });
    if (path === '/api/auth/verify-email') return respond(route, server.rejectVerification ? { detail: '验证码不正确。' } : { verified: true }, server.rejectVerification ? 400 : 200);
    if (['/api/auth/resend-code', '/api/auth/forgot-password', '/api/auth/reset-password'].includes(path)) return respond(route, { ok: true });
    if (path === '/api/auth/logout') { server.user = ''; return respond(route, { ok: true, logoutUrl: server.logoutUrl }); }
    if (!server.user) return respond(route, { detail: '请登录。' }, 401);
    if (path === '/api/state') { const snapshot = state(), held = server.holdState; server.holdState = undefined; if (held) await held; return respond(route, snapshot).catch(() => {}); }
    if (method !== 'GET' && request.headers()['x-csrf-token'] !== (server.csrfToken ?? `csrf-${server.user}`)) return respond(route, { detail: 'CSRF' }, 403);
    if (path.startsWith('/api/operations/')) { const operationId = path.split('/')[3]; return respond(route, { operationId, applied: server.appliedOperations.has(`${server.user}/${operationId}`) }); }
    if (path.startsWith('/api/uploads/')) { const upload = server.uploads.get(`${server.user}/${path.split('/')[3]}`); return respond(route, upload ?? { detail: '未找到上传' }, upload ? 200 : 404); }
    if (path.endsWith('/download')) { const hash = path.split('/')[3]; const assets = currentData().tickets.flatMap(ticket => [ticket.media.original, ticket.media.poster]).filter(Boolean); const asset = assets.find(asset => asset!.assetId === hash) ?? media(); return respond(route, { ...asset, transport: 'server', expiresAt: Date.now() + 60000 }); }
    if (path.startsWith('/api/media/')) return route.fulfill({ contentType: 'image/png', body: path.endsWith(otherHash) ? otherPng : png });
    if (path === '/api/media') { const uploadId = /name="uploadId"\r\n\r\n([^\r]+)/.exec(body)?.[1] ?? ''; const asset = media(); server.uploads.set(`${server.user}/${uploadId}`, { uploadId, status: 'stored', asset }); return respond(route, { ...asset, uploadId }); }
    const data = currentData();
    if (server.appliedOperations.has(`${server.user}/${body?.operationId}`)) return respond(route, state());
    if (path.endsWith('/process') && method === 'POST') {
      if (!server.processing) return respond(route, { detail: '处理配置未就绪' }, 503);
      const ticket = data.tickets.find(item => item.id === decodeURIComponent(path.split('/')[3]));
      if (!ticket || ticket.deletedAt !== null) return respond(route, { detail: '票根不存在' }, 404);
      ticket.processing = { status: 'queued', error: null }; ticket.version++;
    } else if (path === '/api/tickets' && method === 'POST') { const ticket = sampleItem(body.id); ticket.fields = { title: '', cinema: '', hall: '', seat: '', showtime: '', ...body.fields }; ticket.media.original.cloudStatus = options.cloud === false ? 'not_configured' : 'saved'; if (server.processing && ticket.media.original.cloudStatus === 'saved') ticket.processing.status = 'queued'; data.tickets.push(ticket); }
    else if (path.startsWith('/api/tickets/')) { const ticket = data.tickets.find(item => item.id === decodeURIComponent(path.split('/')[3])); if (!ticket) return respond(route, { detail: '不存在' }, 404); if (method === 'PATCH') { Object.assign(ticket.fields, body.fields); for (const key of Object.keys(body.fields ?? {})) ticket.fieldSources[key] = 'user'; if (body.originalAssetId || 'title' in (body.fields ?? {}) || 'showtime' in (body.fields ?? {})) { ticket.inputVersion++; if (server.processing) ticket.processing = { status: 'queued', error: null }; } } else if (method === 'DELETE') ticket.deletedAt = Date.now(); else return respond(route, {}, 404); ticket.version++; }
    else return respond(route, { detail: 'Not found' }, 404);
    if (body.originalAssetId) for (const [key, upload] of server.uploads) if (key.startsWith(server.user + '/') && upload.asset.assetId === body.originalAssetId) upload.status = 'attached';
    server.appliedOperations.add(`${server.user}/${body.operationId}`);
    server.revision++; return respond(route, state());
  });
  return server;
}
async function login(page: Page, email = 'a@example.test') { await page.getByLabel('邮箱地址', { exact: true }).fill(email); await page.getByLabel('密码', { exact: true }).fill(password); await page.getByRole('button', { name: '登录', exact: true }).click(); }

test('session barrier blocks private pages and all business requests', async ({ page }) => {
  let release!: () => void; const gate = new Promise<void>(resolve => { release = resolve; });
  const api = await mockApi(page, { sessionGate: gate }); await page.goto('/tickets/item-1');
  await expect(page.getByText('正在确认登录状态…')).toBeVisible(); await expect(page.locator('.memoverse-shell')).toHaveCount(0);
  expect(api.requests.every(request => request.path.startsWith('/api/auth/'))).toBeTruthy(); release();
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Google 登录暂不可用' })).toBeDisabled();
  expect(await page.evaluate(() => (window as any).__legacyReads)).toBe(0);
});
test('new account starts empty and obsolete business routes are not mounted', async ({ page }) => {
  const api = await mockApi(page, { empty: true }); await page.goto('/login'); await login(page);
  await expect(page.getByRole('heading', { name: '你的第一张电影票' })).toBeVisible();
  await expect(page.locator('.phone-device,.keyboard-dock,.mobile-scroll')).toHaveCount(0);
  await page.goto('/explore'); await expect(page.getByRole('heading', { name: '票根或页面不存在' })).toBeVisible();
  expect(api.requests.some(request => /community|shares|bootstrap|models/.test(request.path))).toBeFalsy();
});
test('registration verifies email and keeps invalid-code input for correction', async ({ page }) => {
  const api = await mockApi(page);
  await page.goto('/register');
  await page.getByLabel('邮箱地址', { exact: true }).fill('new@example.test');
  await page.getByLabel('设置密码', { exact: true }).fill(password);
  await page.getByRole('button', { name: '创建账户', exact: true }).click();
  await expect(page.getByRole('heading', { name: '验证你的邮箱' })).toBeVisible();
  await expect(page.getByLabel('邮箱地址', { exact: true })).toHaveValue('new@example.test');
  api.rejectVerification = true;
  await page.getByLabel('邮件验证码', { exact: true }).fill('long-code-ABC123');
  await page.getByRole('button', { name: '验证邮箱', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('验证码不正确');
  await expect(page.getByLabel('邮件验证码', { exact: true })).toHaveValue('long-code-ABC123');
  api.rejectVerification = false;
  await page.getByRole('button', { name: '验证邮箱', exact: true }).click();
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  expect(api.requests.find(request => request.path === '/api/auth/verify-email')?.body.code).toBe('long-code-ABC123');
  expect(api.requests.every(request => request.path.startsWith('/api/auth/'))).toBeTruthy();
});

test('forgot-password and reset use real forms without trimming the password', async ({ page }) => {
  const api = await mockApi(page);
  await page.goto('/forgot-password');
  await page.getByLabel('邮箱地址', { exact: true }).fill('a@example.test');
  await page.getByRole('button', { name: '发送重置验证码', exact: true }).click();
  await expect(page.getByRole('heading', { name: '设置新密码' })).toBeVisible();
  await page.getByLabel('邮件验证码').fill('RESET-ABC');
  const replacement = '  NewExamplePassword456!  ';
  await page.getByLabel('设置密码').fill(replacement);
  await page.getByRole('button', { name: '更新密码', exact: true }).click();
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  expect(api.requests.find(request => request.path === '/api/auth/reset-password')?.body.password).toBe(replacement);
});

test('unverified login routes to verification without exposing the collection', async ({ page }) => {
  const api = await mockApi(page);
  api.unverifiedLogin = true;
  await page.goto('/login');
  await login(page);
  await expect(page.getByRole('heading', { name: '验证你的邮箱' })).toBeVisible();
  await expect(page.locator('.memoverse-shell')).toHaveCount(0);
});

test('Cognito logout leaves an anonymous document before redirect and returns signed out', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  await page.goto('/tickets');
  await expect(page.getByRole('heading', { name: '我的票夹', exact: true })).toBeVisible();
  const origin = new URL(page.url()).origin;
  const provider = 'https://memoverse.auth.ap-northeast-3.amazoncognito.com';
  api.logoutUrl = provider + '/logout?' + new URLSearchParams({ client_id: 'testclient123', logout_uri: origin + '/' });
  let beforeNavigation: { privateUI: boolean; pathname: string; message: boolean } | undefined;
  await page.exposeFunction('recordLogoutDocument', (snapshot: typeof beforeNavigation) => { beforeNavigation = snapshot; });
  await page.evaluate(() => {
    window.addEventListener('beforeunload', () => {
      void (window as any).recordLogoutDocument({ privateUI: !!document.querySelector('.memoverse-shell'),
        pathname: location.pathname, message: document.body.innerText.includes('已退出登录。') });
    }, { once: true });
  });
  await page.route(provider + '/logout?**', async route => {
    await route.fulfill({ status: 302, headers: { location: origin + '/' } });
  });
  await page.getByRole('button', { name: '退出登录', exact: true }).first().click();
  await expect.poll(() => beforeNavigation).toEqual({ privateUI: false, pathname: '/login', message: true });
  await expect(page).toHaveURL(origin + '/');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(page.locator('.memoverse-shell')).toHaveCount(0);
  expect(api.user).toBe('');
});

test('an unsafe logout URL stays on the signed-out local page', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  api.logoutUrl = 'https://evil.example/logout';
  await page.goto('/tickets');
  await expect(page.getByRole('heading', { name: '我的票夹', exact: true })).toBeVisible();
  const origin = new URL(page.url()).origin;
  const externalNavigations: string[] = [];
  await page.route('https://evil.example/**', async route => { externalNavigations.push(route.request().url()); await route.abort(); });
  await page.getByRole('button', { name: '退出登录', exact: true }).first().click();
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(page.getByText('已退出登录。', { exact: true })).toBeVisible();
  await expect(page.locator('.memoverse-shell')).toHaveCount(0);
  await expect(page).toHaveURL(origin + '/login');
  expect(externalNavigations).toEqual([]);
});

test('unconfigured authentication remains unavailable and cannot expose workspace', async ({ page }) => {
  const api = await mockApi(page, { configured: false });
  await page.goto('/login');
  await expect(page.getByRole('alert')).toContainText('登录服务暂时不可用');
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeDisabled();
  await expect(page.locator('.memoverse-shell')).toHaveCount(0);
  expect(api.requests.every(request => request.path.startsWith('/api/auth/'))).toBeTruthy();
});

test('Google callback failure shows a fixed message and removes the error query', async ({ page }) => {
  await mockApi(page);
  await page.goto('/?auth_error=google_login_failed');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(page.getByText('Google 登录未完成，请重试。', { exact: true })).toBeVisible();
  await expect(page).not.toHaveURL(/auth_error/);
  await expect(page.locator('.memoverse-shell')).toHaveCount(0);
});

test('returning to a browser tab rechecks a session ended elsewhere', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  await page.goto('/tickets');
  await expect(page.getByRole('heading', { name: '我的票夹', exact: true })).toBeVisible();
  api.user = '';
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(page.locator('.memoverse-shell')).toHaveCount(0);
});


for (const width of [360, 390, 768, 1280, 1440]) {
  test(`ticket library and native editor fit ${width}px`, async ({ page }, info) => {
    await mockApi(page, { user: 'a' }); await page.setViewportSize({ width, height: 900 }); await page.goto('/tickets');
    await expect(page.getByRole('heading', { name: '我的票夹', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    await page.screenshot({ path: info.outputPath(`tickets-${width}.png`), fullPage: true });
    await page.getByRole('link', { name: '上传电影票', exact: true }).click();
    await expect(page.getByLabel('片名')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    await expect(page.locator('.keyboard-dock,.phone-device')).toHaveCount(0);
    await page.getByLabel('片名').focus(); await expect(page.getByLabel('片名')).toBeFocused();
    if (width === 390 || width === 1440) { await page.evaluate(() => window.scrollTo(0, 0)); await page.screenshot({ path: info.outputPath(`editor-${width}.png`), fullPage: true }); }
  });
}
test('direct links, refresh, back and cloud status remain accurate', async ({ page }) => {
  await mockApi(page, { user: 'a', cloud: false }); await page.goto('/tickets/item-1');
  await expect(page.getByRole('heading', { name: 'A 的电影票' })).toBeVisible();
  await expect(page.getByText('原票已保存到当前服务器，尚未云存')).toBeVisible();
  await expect(page.getByText('本机可用情况以图片下方的校验与缓存状态为准。')).toBeVisible();
  await page.reload(); await expect(page.getByRole('heading', { name: 'A 的电影票' })).toBeVisible();
  await page.getByRole('link', { name: '编辑票根' }).click(); await page.goBack();
  await expect(page.getByRole('heading', { name: 'A 的电影票' })).toBeVisible();
  await page.getByRole('link', { name: '设备与状态' }).click();
  await expect(page.getByText('当前服务器未配置云存储。当前保存不具备云端恢复保障。')).toBeVisible();
});
test('upload with empty fields, field-only correction and deletion use the new contract', async ({ page }) => {
  const api = await mockApi(page, { user: 'a', empty: true }); await page.goto('/tickets/new');
  await page.getByLabel('选择原票照片').setInputFiles({ name: 'ticket.png', mimeType: 'image/png', buffer: png });
  await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: '待补充片名' })).toBeVisible();
  const create = api.requests.find(request => request.path === '/api/tickets' && request.method === 'POST')!;
  expect(create.body.fields).toEqual({}); expect(create.body.operationId).toBeTruthy();
  await page.getByRole('link', { name: '编辑票根' }).click(); await page.getByLabel('片名').fill('新片名');
  await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: '新片名' })).toBeVisible();
  expect(api.requests.find(request => request.method === 'PATCH')?.body.fields).toEqual({ title: '新片名' });
  await page.getByRole('button', { name: '删除票根', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible(); await page.getByRole('button', { name: '确认删除' }).click();
  await expect(page.getByRole('heading', { name: '你的第一张电影票' })).toBeVisible();
  expect(api.requests.find(request => request.method === 'DELETE')?.body.operationId).toBeTruthy();
});
test('same-account session renewal retains the draft and attaches the new CSRF token', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' }); await page.goto('/tickets/item-1/edit');
  await page.getByLabel('影院').fill('会话刷新后保留的影院');
  api.csrfToken = 'renewed'; await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect.poll(() => api.requests.filter(request => request.path === '/api/auth/session').length).toBeGreaterThan(1);
  await expect(page.getByLabel('影院')).toHaveValue('会话刷新后保留的影院');
  await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'A 的电影票' })).toBeVisible();
  expect(api.requests.find(request => request.method === 'PATCH')?.headers['x-csrf-token']).toBe('renewed');
});
test('logout discards a late old-account state before another login', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' }); await page.goto('/tickets');
  await expect(page.getByRole('heading', { name: 'A 的电影票' })).toBeVisible();
  let release!: () => void; api.holdState = new Promise<void>(resolve => { release = resolve; });
  const before = api.requests.length; await page.getByRole('button', { name: '刷新票夹' }).click();
  await expect.poll(() => api.requests.length).toBeGreaterThan(before);
  await page.getByRole('button', { name: '退出登录' }).click(); await login(page, 'b@example.test'); release();
  await expect(page.locator('.memoverse-shell')).toHaveAttribute('data-user-id', 'b');
  await expect(page.getByRole('heading', { name: 'B 的电影票' })).toBeVisible();
  await expect(page.getByText('A 的电影票', { exact: true })).toHaveCount(0);
});
test('temporary photo URLs are released on replacement, navigation and logout', async ({ page }) => {
  await mockApi(page, { user: 'a' });
  await page.addInitScript(() => { const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL); (window as any).__urls = []; (window as any).__draftUrls = []; (window as any).__revoked = []; URL.createObjectURL = value => { const url = create(value); (window as any).__urls.push(url); if (value instanceof File) (window as any).__draftUrls.push(url); return url; }; URL.revokeObjectURL = url => { (window as any).__revoked.push(url); revoke(url); }; });
  await page.goto('/tickets/new');
  const select = () => page.getByLabel('选择原票照片').setInputFiles({ name: 'ticket.png', mimeType: 'image/png', buffer: png });
  await select(); await select(); await page.getByRole('link', { name: '取消', exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as any).__revoked.filter((url: string) => (window as any).__draftUrls.includes(url)).length)).toBe(2);
  await page.getByRole('link', { name: '上传电影票', exact: true }).click(); await select();
  await page.getByRole('button', { name: '退出登录' }).click();
  await expect.poll(() => page.evaluate(() => (window as any).__revoked.filter((url: string) => (window as any).__draftUrls.includes(url)).length)).toBe(3);
});

test('an authenticated state 401 removes all private UI', async ({ page }) => {
  await mockApi(page, { user: 'a' }); await page.goto('/tickets');
  await expect(page.getByRole('heading', { name: 'A 的电影票' })).toBeVisible();
  await page.route('**/api/state', route => route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ detail: '会话结束' }) }));
  await page.getByRole('button', { name: '刷新票夹' }).click();
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible(); await expect(page.locator('.memoverse-shell')).toHaveCount(0);
});
test('an old detail poll cannot restore a deleted ticket', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' }); await page.clock.install(); await page.goto('/tickets/item-1');
  await expect(page.getByRole('heading', { name: 'A 的电影票' })).toBeVisible();
  let release!: () => void; api.holdState = new Promise<void>(resolve => { release = resolve; });
  const before = api.requests.filter(request => request.path === '/api/state').length;
  await page.clock.fastForward(10001); await expect.poll(() => api.requests.filter(request => request.path === '/api/state').length).toBeGreaterThan(before);
  await page.getByRole('button', { name: '删除票根', exact: true }).click(); await page.getByRole('button', { name: '确认删除' }).click();
  await expect(page.getByRole('heading', { name: '你的第一张电影票' })).toBeVisible(); release();
  await expect(page.getByText('A 的电影票', { exact: true })).toHaveCount(0);
});

async function cacheEntries(page: Page): Promise<{ accountId: string; sha256: string }[]> {
  return page.evaluate(() => new Promise((resolve, reject) => {
    const open = indexedDB.open('memoverse-media-cache-v1', 1);
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
      const database = open.result;
      if (!database.objectStoreNames.contains('media')) { database.close(); resolve([]); return; }
      const request = database.transaction('media').objectStore('media').getAll();
      request.onsuccess = () => { const entries = request.result.map((record: any) => ({ accountId: record.accountId, sha256: record.sha256 })); database.close(); resolve(entries); };
      request.onerror = () => { database.close(); reject(request.error); };
    };
  }));
}
const imageReads = (api: Awaited<ReturnType<typeof mockApi>>) => api.requests.filter(request => /^\/api\/media\/[a-f0-9]{64}$/.test(request.path)).length;

test('verified cache survives reload and clearing recovers from the server only on the next read', async ({ page }, info) => {
  const api = await mockApi(page, { user: 'a' }); await page.goto('/tickets');
  await expect(page.locator('.media-availability').first()).toContainText('已校验');
  const firstReads = imageReads(api); expect(firstReads).toBe(1);
  await page.reload(); await expect(page.locator('.media-availability').first()).toContainText('本机缓存可用');
  expect(imageReads(api)).toBe(firstReads);
  await page.getByRole('link', { name: '设备与状态' }).click();
  await expect(page.getByText(/本账号已存 1 张图片/)).toBeVisible();
  await page.screenshot({ path: info.outputPath('cache-desktop-1440.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 900 });
  await page.screenshot({ path: info.outputPath('cache-mobile-390.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole('button', { name: '清除本账号本机缓存' }).click();
  await expect(page.getByText(/本账号已存 0 张图片/)).toBeVisible();
  expect(await cacheEntries(page)).toEqual([]); expect(imageReads(api)).toBe(firstReads);
  await page.getByRole('link', { name: '票夹', exact: true }).click();
  await expect(page.locator('.media-availability').first()).toContainText('已校验');
  expect(imageReads(api)).toBe(firstReads + 1);
});
test('equal hashes remain isolated across accounts and clearing B keeps A cache', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' }); await page.goto('/tickets');
  await expect(page.locator('.media-availability').first()).toContainText('已校验');
  await page.getByRole('button', { name: '退出登录' }).click(); await login(page, 'b@example.test');
  await expect(page.locator('.media-availability').first()).toContainText('已校验');
  expect(imageReads(api)).toBe(2);
  expect((await cacheEntries(page)).map(entry => entry.accountId).sort()).toEqual(['a', 'b']);
  await page.getByRole('link', { name: '设备与状态' }).click(); await page.getByRole('button', { name: '清除本账号本机缓存' }).click();
  await expect(page.getByText(/本账号已存 0 张图片/)).toBeVisible();
  expect((await cacheEntries(page)).map(entry => entry.accountId)).toEqual(['a']);
});
test('quota exhaustion still displays the verified cloud image without claiming it is cached', async ({ page }) => {
  await mockApi(page, { user: 'a' });
  await page.addInitScript(() => { IDBObjectStore.prototype.put = function() { throw new DOMException('test quota', 'QuotaExceededError'); }; });
  await page.goto('/tickets/item-1');
  await expect(page.locator('.ticket-image img')).toBeVisible();
  await expect(page.locator('.media-availability')).toContainText('本机缓存空间不足');
  expect(await cacheEntries(page)).toEqual([]);
  await page.goto('/tickets/new');
  await page.getByLabel('选择原票照片').setInputFiles({ name: 'quota.png', mimeType: 'image/png', buffer: png });
  await page.getByLabel('片名').fill('缓存不足也能保存');
  await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: '缓存不足也能保存' })).toBeVisible();
  await expect(page.getByText('原票已保存到云端', { exact: true })).toBeVisible();
  await expect(page.locator('.media-availability')).toContainText('本机缓存空间不足');
});
test('unavailable browser storage does not prevent a verified temporary image', async ({ page }) => {
  await mockApi(page, { user: 'a' });
  await page.addInitScript(() => { indexedDB.open = () => { throw new DOMException('test denied', 'SecurityError'); }; });
  await page.goto('/tickets/item-1');
  await expect(page.locator('.ticket-image img')).toBeVisible();
  await expect(page.locator('.media-availability')).toContainText('本机缓存不可用');
});
test('a checksum mismatch is never displayed or marked cached', async ({ page }) => {
  await mockApi(page, { user: 'a' });
  await page.route('**/api/media/' + pngHash, route => route.fulfill({ contentType: 'image/png', body: Buffer.alloc(png.length) }));
  await page.goto('/tickets/item-1');
  await expect(page.locator('.media-availability')).toContainText('图片完整性校验失败');
  await expect(page.locator('.ticket-image img')).toHaveCount(0);
  expect(await cacheEntries(page)).toEqual([]);
});
test('logout during a download cannot write the old account cache', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  let release!: () => void; const held = new Promise<void>(resolve => { release = resolve; }); let started = false;
  await page.route('**/api/media/' + pngHash, async route => { if (api.user === 'a') { started = true; await held; await route.fulfill({ contentType: 'image/png', body: png }).catch(() => {}); } else await route.fallback(); });
  await page.goto('/tickets'); await expect.poll(() => started).toBeTruthy();
  await page.getByRole('button', { name: '退出登录' }).click(); await login(page, 'b@example.test'); release();
  await expect(page.locator('.media-availability').first()).toContainText('已校验');
  expect((await cacheEntries(page)).map(entry => entry.accountId)).toEqual(['b']);
});
test('logout during SHA-256 cannot publish a stale URL or write the old cache', async ({ page }) => {
  await mockApi(page, { user: 'a' });
  await page.addInitScript(() => {
    const digest = crypto.subtle.digest.bind(crypto.subtle); let calls = 0;
    crypto.subtle.digest = async (algorithm, data) => { const result = await digest(algorithm, data); if (calls++ === 0) { (window as any).__digestStarted = true; await new Promise<void>(resolve => { (window as any).__releaseDigest = resolve; }); } return result; };
  });
  await page.goto('/tickets'); await expect.poll(() => page.evaluate(() => (window as any).__digestStarted)).toBeTruthy();
  await page.getByRole('button', { name: '退出登录' }).click(); await login(page, 'b@example.test');
  await page.evaluate(() => (window as any).__releaseDigest());
  await expect(page.locator('.media-availability').first()).toContainText('已校验');
  expect((await cacheEntries(page)).map(entry => entry.accountId)).toEqual(['b']);
});
test('deletion prunes only unreferenced account images and retains shared originals', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' }); const first = sampleItem('first'), second = sampleItem('second');
  api.data.set('a', { tickets: [first, second] }); await page.goto('/tickets');
  await expect(page.locator('.media-availability').first()).toContainText('已校验'); expect(await cacheEntries(page)).toHaveLength(1);
  await page.goto('/tickets/first'); await page.getByRole('button', { name: '删除票根', exact: true }).click(); await page.getByRole('button', { name: '确认删除' }).click();
  await expect(page.locator('.ticket-card')).toHaveCount(1); expect(await cacheEntries(page)).toHaveLength(1);
  await page.goto('/tickets/second'); await page.getByRole('button', { name: '删除票根', exact: true }).click(); await page.getByRole('button', { name: '确认删除' }).click();
  await expect(page.getByRole('heading', { name: '你的第一张电影票' })).toBeVisible();
  await expect.poll(() => cacheEntries(page)).toEqual([]);
});

test('focus and device status recheck storage eviction without redownloading an in-memory image', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' }); await page.goto('/tickets/item-1');
  await expect(page.locator('.media-availability')).toContainText('已校验'); const reads = imageReads(api);
  await page.evaluate(() => new Promise<void>((resolve, reject) => {
    const open = indexedDB.open('memoverse-media-cache-v1', 1);
    open.onsuccess = () => { const database = open.result, tx = database.transaction('media', 'readwrite'); tx.objectStore('media').clear(); tx.oncomplete = () => { database.close(); resolve(); }; tx.onerror = () => reject(tx.error); };
  }));
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.locator('.media-availability')).toContainText('本机缓存已被回收');
  await expect(page.locator('.ticket-image img')).toBeVisible(); expect(imageReads(api)).toBe(reads);
  await page.getByRole('link', { name: '设备与状态' }).click();
  await expect(page.getByText(/本账号已存 0 张图片/)).toBeVisible(); expect(imageReads(api)).toBe(reads);
});

test('a large library fetches only near-viewport images with at most four concurrent transfers', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  const images = Array.from({ length: 100 }, (_, index) => imageFixture(index + 1));
  api.data.set('a', { tickets: images.map((image, index) => ({ ...sampleItem(`many-${index}`), createdAt: 1000 - index, media: { original: image.asset, poster: null } })) });
  const byHash = new Map(images.map(image => [image.asset.sha256, image]));
  let release!: () => void; const held = new Promise<void>(resolve => { release = resolve; }); let started = 0, active = 0, maximum = 0;
  const reads = new Map<string, number>();
  await page.addInitScript(() => { const revoke = URL.revokeObjectURL.bind(URL); (window as any).__releasedMedia = []; URL.revokeObjectURL = url => { (window as any).__releasedMedia.push(url); revoke(url); }; });
  await page.route('**/api/media/**', async route => {
    const path = new URL(route.request().url()).pathname, image = byHash.get(path.split('/')[3]);
    if (!image) return route.fallback();
    if (path.endsWith('/download')) return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ...image.asset, transport: 'server', expiresAt: Date.now() + 60000 }) });
    started++; active++; reads.set(image.asset.sha256, (reads.get(image.asset.sha256) ?? 0) + 1); maximum = Math.max(maximum, active); await held;
    try { await route.fulfill({ contentType: 'image/png', body: image.bytes }); } catch { /* Closed test page. */ } finally { active--; }
  });
  await page.goto('/tickets'); await expect.poll(() => started).toBe(4);
  expect(maximum).toBe(4); release();
  const eligible = await page.locator('.ticket-image').evaluateAll(nodes => nodes.filter(node => { const bounds = node.getBoundingClientRect(); return bounds.top < innerHeight + 200 && bounds.bottom > -200; }).length);
  await expect.poll(() => started).toBe(eligible); expect(started).toBeLessThan(100); expect(maximum).toBeLessThanOrEqual(4);
  await expect(page.locator('.media-availability').first()).toContainText('已校验');
  const firstUrl = await page.locator('.ticket-image img').first().getAttribute('src');
  await page.locator('.ticket-card').last().scrollIntoViewIfNeeded();
  await expect(page.locator('.media-availability').last()).toContainText('已校验');
  await expect.poll(() => page.evaluate(url => (window as any).__releasedMedia.includes(url), firstUrl)).toBeTruthy();
  await page.locator('.ticket-card').first().scrollIntoViewIfNeeded();
  await expect(page.locator('.media-availability').first()).toContainText('本机缓存可用');
  expect(reads.get(images[0].asset.sha256)).toBe(1);
});
test('a collapsed original photo is not downloaded until it is opened', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' }); const poster = imageFixture(123); const ticket = sampleItem(); ticket.media.poster = { ...poster.asset, source: posterSource() };
  api.data.set('a', { tickets: [ticket] });
  await page.route('**/api/media/' + poster.asset.sha256 + '**', route => new URL(route.request().url()).pathname.endsWith('/download')
    ? route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ...poster.asset, transport: 'server', expiresAt: Date.now() + 60000 }) })
    : route.fulfill({ contentType: 'image/png', body: poster.bytes }));
  await page.goto('/tickets/item-1'); await expect(page.locator('.media-availability').first()).toContainText('已校验');
  expect(api.requests.filter(request => request.path === media().url)).toHaveLength(0);
  await page.getByText('查看原票照片', { exact: true }).click();
  await expect(page.locator('.original-photo .media-availability')).toContainText('已校验');
  expect(api.requests.filter(request => request.path === media().url)).toHaveLength(1);
});
test('a corrupt cached blob is removed even when cloud retrieval also fails', async ({ page }) => {
  await mockApi(page, { user: 'a' }); await page.goto('/tickets'); await expect(page.locator('.media-availability').first()).toContainText('已校验');
  await page.getByRole('link', { name: '设备与状态' }).click();
  await page.evaluate(() => new Promise<void>((resolve, reject) => {
    const open = indexedDB.open('memoverse-media-cache-v1', 1);
    open.onsuccess = () => { const database = open.result, tx = database.transaction('media', 'readwrite'), store = tx.objectStore('media'), request = store.openCursor();
      request.onsuccess = () => { const cursor = request.result; if (cursor) { const record = cursor.value; record.blob = new Blob([new Uint8Array(record.size)], { type: record.mimeType }); cursor.update(record); } };
      tx.oncomplete = () => { database.close(); resolve(); }; tx.onerror = () => reject(tx.error); };
  }));
  await page.route('**/api/media/' + pngHash, route => route.abort('failed'));
  await page.getByRole('link', { name: '票夹', exact: true }).click();
  await expect(page.locator('.media-availability').first()).toContainText('暂时无法连接服务');
  expect(await cacheEntries(page)).toEqual([]);
});

test('disabled processing reports configuration readiness while retaining manual editing', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' }); await page.goto('/tickets/item-1');
  await expect(page.getByRole('region', { name: '自动处理' })).toContainText('处理配置未就绪');
  await expect(page.getByRole('button', { name: '开始识别' })).toBeDisabled();
  await page.getByRole('link', { name: '编辑票根' }).click();
  await expect(page.getByText('处理配置未就绪，请配置服务端识别。', { exact: true })).toBeVisible();
  await expect(page.getByLabel('片名')).toBeEditable();
  expect(api.requests.some(request => request.path.endsWith('/process'))).toBeFalsy();
});
test('active processing advances in the library and catches up after a failed state request', async ({ page }) => {
  const api = await mockApi(page, { user: 'a', processing: true }); const ticket = sampleItem(); ticket.processing.status = 'queued';
  api.data.set('a', { tickets: [ticket] }); await page.clock.install(); await page.goto('/tickets');
  await expect(page.getByText('等待处理', { exact: true })).toBeVisible();
  await expect(page.locator('.media-availability')).toContainText('已校验');
  await expect(page.getByText('处理完成', { exact: true })).toHaveCount(0);
  ticket.processing.status = 'ocr'; ticket.version++; api.revision++;
  await page.clock.fastForward(4001); await expect(page.getByText('正在识别票面', { exact: true })).toBeVisible();
  const unavailable = (route: Route) => route.abort('failed'); await page.route('**/api/state', unavailable);
  ticket.processing.status = 'poster'; ticket.version++; api.revision++;
  await page.clock.fastForward(4001); await expect(page.getByRole('alert')).toContainText('暂时无法连接服务');
  ticket.processing.status = 'no_match'; ticket.version++; api.revision++;
  await page.unroute('**/api/state', unavailable); await page.clock.fastForward(4001);
  await expect(page.getByText('未找到可靠海报', { exact: true })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveCount(0);
});
test('OCR updates untouched fields while preserving a local correction and an explicit clearing', async ({ page }) => {
  const api = await mockApi(page, { user: 'a', processing: true }); const ticket = sampleItem();
  ticket.fields = { title: '', cinema: '', hall: '', seat: '', showtime: '' };
  ticket.fieldSources = { title: 'empty', cinema: 'empty', hall: 'empty', seat: 'empty', showtime: 'empty' };
  ticket.processing.status = 'ocr'; api.data.set('a', { tickets: [ticket] });
  await page.clock.install(); await page.goto('/tickets/item-1/edit'); await page.getByLabel('片名').fill('用户修正的片名');
  ticket.fields = { title: '识别的片名', cinema: '识别的影院', hall: '3 号厅', seat: '5 排 6 座', showtime: '2026-10-22T19:30' };
  ticket.fieldSources = { title: 'ocr', cinema: 'ocr', hall: 'ocr', seat: 'ocr', showtime: 'ocr' };
  ticket.processing.status = 'poster'; ticket.version++; api.revision++;
  await page.clock.fastForward(4001);
  await expect(page.getByLabel('片名')).toHaveValue('用户修正的片名'); await expect(page.getByLabel('影院')).toHaveValue('识别的影院');
  await page.getByLabel('座位').fill(''); await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: '用户修正的片名' })).toBeVisible();
  const update = api.requests.find(request => request.method === 'PATCH')!;
  expect(update.body.fields).toEqual({ title: '用户修正的片名', seat: '' });
  expect(ticket.fieldSources.title).toBe('user'); expect(ticket.fieldSources.seat).toBe('user');
  ticket.processing.status = 'no_match'; ticket.fields.cinema = '后续识别的影院'; ticket.version++; api.revision++;
  await page.clock.fastForward(4001);
  await expect(page.getByRole('heading', { name: '用户修正的片名' })).toBeVisible();
  await expect(page.getByRole('definition').filter({ hasText: '未填写' })).toContainText('手动填写');
  await expect(page.getByRole('definition').filter({ hasText: '后续识别的影院' })).toContainText('识别结果');
});
test('a failed task can be retried and a later failure starts a fresh operation', async ({ page }) => {
  const api = await mockApi(page, { user: 'a', processing: true }); const ticket = sampleItem(); ticket.processing = { status: 'failed', error: '识别暂时失败，请重试。' }; api.data.set('a', { tickets: [ticket] });
  await page.clock.install(); await page.goto('/tickets/item-1');
  await expect(page.getByRole('alert')).toContainText('识别暂时失败');
  await page.getByRole('button', { name: '重试处理', exact: true }).click(); await expect(page.getByRole('button', { name: '处理中…' })).toBeDisabled();
  const first = api.requests.find(request => request.path.endsWith('/process'))!; expect(first.body.operationId).toBeTruthy(); expect(first.headers['x-csrf-token']).toBe('csrf-a');
  ticket.processing = { status: 'failed', error: '海报保存失败，原票保留。' }; ticket.version++; api.revision++;
  await page.clock.fastForward(4001); await page.getByRole('button', { name: '重试处理', exact: true }).click();
  const requests = api.requests.filter(request => request.path.endsWith('/process')); expect(requests).toHaveLength(2); expect(requests[1].body.operationId).not.toBe(requests[0].body.operationId);
});
async function servePoster(page: Page, image: ReturnType<typeof imageFixture>) {
  await page.route('**/api/media/' + image.asset.sha256 + '**', route => new URL(route.request().url()).pathname.endsWith('/download')
    ? route.fulfill({ contentType: 'application/json', body: JSON.stringify({ ...image.asset, transport: 'server', expiresAt: Date.now() + 60000 }) })
    : route.fulfill({ contentType: 'image/png', body: image.bytes }));
}
test('old poster stays through processing and no-match, then changes only with the published reference', async ({ page }) => {
  const api = await mockApi(page, { user: 'a', processing: true }); const oldPoster = imageFixture(201), nextPoster = imageFixture(202);
  const ticket = sampleItem(); ticket.media.poster = { ...oldPoster.asset, source: posterSource('Old_poster.png') }; ticket.processing.status = 'queued'; api.data.set('a', { tickets: [ticket] });
  await servePoster(page, oldPoster); await servePoster(page, nextPoster); await page.clock.install(); await page.goto('/tickets/item-1');
  await expect(page.locator('.ticket-image img').first()).toBeVisible(); const oldUrl = await page.locator('.ticket-image img').first().getAttribute('src');
  ticket.processing.status = 'poster'; ticket.version++; api.revision++; await page.clock.fastForward(4001);
  await expect(page.getByText('正在查找海报', { exact: true })).toBeVisible(); await expect(page.locator('.ticket-image img').first()).toHaveAttribute('src', oldUrl!);
  ticket.processing.status = 'no_match'; ticket.version++; api.revision++; await page.clock.fastForward(4001);
  await expect(page.getByText('本次没有找到可靠海报，继续保留已有封面。')).toBeVisible();
  await page.getByRole('button', { name: '重试处理', exact: true }).click();
  ticket.media.poster = { ...nextPoster.asset, source: { ...posterSource('New_poster.png'), author: '合成作者 <b>纯文本</b>' } };
  ticket.processing.status = 'ready'; ticket.version++; api.revision++; await page.clock.fastForward(4001);
  await expect(page.getByText('处理完成', { exact: true })).toBeVisible();
  await expect(page.locator('.ticket-image img').first()).not.toHaveAttribute('src', oldUrl!);
  await expect(page.locator('.ticket-image img').first()).toHaveAttribute('src', /^blob:/);
  await expect(page.getByRole('link', { name: 'Wikimedia Commons 来源页' })).toHaveAttribute('href', ticket.media.poster.source.sourcePage);
  await expect(page.getByRole('link', { name: 'CC BY-SA 4.0' })).toHaveAttribute('href', 'https://creativecommons.org/licenses/by-sa/4.0/');
  await expect(page.getByText('作者：合成作者 <b>纯文本</b>', { exact: true })).toBeVisible();
  await expect(page.locator('.poster-attribution b')).toHaveCount(0);
  await expect.poll(async () => (await cacheEntries(page)).map(entry => entry.sha256)).toEqual([nextPoster.asset.sha256]);
});
for (const width of [360, 390, 768, 1280, 1440]) {
  test(`processing and poster attribution remain readable at ${width}px`, async ({ page }, info) => {
    const api = await mockApi(page, { user: 'a', processing: true }); const poster = imageFixture(204), ticket = sampleItem();
    ticket.media.poster = { ...poster.asset, source: posterSource() }; ticket.processing.status = 'poster'; ticket.fieldSources.title = 'ocr';
    api.data.set('a', { tickets: [ticket] }); await servePoster(page, poster); await page.setViewportSize({ width, height: 1000 }); await page.goto('/tickets/item-1');
    await expect(page.getByRole('region', { name: '自动处理' })).toContainText('正在查找海报');
    await expect(page.getByRole('link', { name: 'CC BY-SA 4.0' })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
    await page.evaluate(() => window.scrollTo(0, 0));
    if (width === 390 || width === 1440) await page.screenshot({ path: info.outputPath(`processing-${width}.png`), fullPage: true });
  });
}

test('a manual value matching a concurrent OCR result still claims the user field source', async ({ page }) => {
  const api = await mockApi(page, { user: 'a', processing: true }); const ticket = sampleItem();
  ticket.fields.title = ''; ticket.fieldSources.title = 'empty'; ticket.processing.status = 'ocr'; api.data.set('a', { tickets: [ticket] });
  await page.clock.install(); await page.goto('/tickets/item-1/edit'); await page.getByLabel('片名').fill('用户手填 A');
  ticket.fields.title = '用户手填 A'; ticket.fieldSources.title = 'ocr'; ticket.processing.status = 'poster'; ticket.version++; api.revision++;
  await page.clock.fastForward(4001); await page.getByLabel('影院').fill('用户填写的影院');
  await expect(page.getByLabel('片名')).toHaveValue('用户手填 A');
  await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: '用户手填 A' })).toBeVisible();
  const update = api.requests.find(request => request.method === 'PATCH')!;
  expect(update.body.fields).toEqual({ title: '用户手填 A', cinema: '用户填写的影院' });
  expect(ticket.fieldSources.title).toBe('user');
  await expect(page.getByRole('definition').filter({ hasText: '用户手填 A' })).toContainText('手动填写');
});

test('explicitly clearing a new manual field preserves its user intent while untouched fields stay available to OCR', async ({ page }) => {
  const api = await mockApi(page, { user: 'a', empty: true, processing: true }); await page.goto('/tickets/new');
  await page.getByLabel('选择原票照片').setInputFiles({ name: 'manual-empty.png', mimeType: 'image/png', buffer: png });
  await page.getByLabel('片名').fill('临时手填'); await page.getByLabel('片名').fill('');
  await page.getByRole('button', { name: '保存票根', exact: true }).click();
  await expect(page.getByRole('heading', { name: '待补充片名' })).toBeVisible();
  const created = api.requests.find(request => request.path === '/api/tickets' && request.method === 'POST')!;
  expect(created.body.fields).toEqual({ title: '' });
});

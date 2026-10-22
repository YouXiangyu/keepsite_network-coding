import { expect, test, type Page, type Route } from '@playwright/test';
import { sampleItem, media } from '../tests/web-fixtures.mjs';
const password = 'ExamplePassword123!';
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=', 'base64');
async function mockApi(page: Page, options: { user?: string; configured?: boolean; google?: boolean; sessionGate?: Promise<void>; empty?: boolean; cloud?: boolean } = {}) {
  const server = {
    user: options.user ?? '', configured: options.configured ?? true, google: options.google ?? false,
    requests: [] as { path: string; method: string; body: any; headers: Record<string, string> }[],
    data: new Map<string, { tickets: ReturnType<typeof sampleItem>[] }>(), revision: 1,
    rejectVerification: false, unverifiedLogin: false, holdState: undefined as Promise<void> | undefined,
    csrfToken: undefined as string | undefined, logoutUrl: undefined as string | undefined,
    appliedOperations: new Set<string>(),
  };
  const currentData = (user = server.user) => {
    if (!server.data.has(user)) { const ticket = sampleItem(); ticket.fields.title = `${user.toUpperCase()} 的电影票`; ticket.media.original.cloudStatus = options.cloud === false ? 'not_configured' : 'saved'; server.data.set(user, { tickets: options.empty ? [] : [ticket] }); }
    return server.data.get(user)!;
  };
  const session = () => ({ authenticated: !!server.user, configured: server.configured, providers: { google: server.google, email: true },
    ...(server.user ? { user: { id: server.user, email: `${server.user}@example.test`, name: server.user }, csrfToken: server.csrfToken ?? `csrf-${server.user}` } : {}) });
  const state = () => ({ initialized: true, serverId: 'web-server', userId: server.user, revision: server.revision, data: structuredClone(currentData()), capabilities: { processing: false, deviceSync: false, cloudStorage: options.cloud !== false } });
  const respond = (route: Route, value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
  await page.addInitScript(() => { (window as any).__legacyReads = 0; indexedDB.open = (() => { (window as any).__legacyReads += 1; throw new Error('Legacy data must not be read'); }) as typeof indexedDB.open; });
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
    if (path.startsWith('/api/media/')) return route.fulfill({ contentType: 'image/svg+xml', body: `<svg xmlns="http://www.w3.org/2000/svg" width="600" height="750"><rect width="600" height="750" fill="#e6e5dd"/><rect x="45" y="140" width="510" height="470" rx="12" fill="#fffdf5"/><path d="M65 420H535" stroke="#aaa798" stroke-width="2" stroke-dasharray="9 7"/><text x="85" y="210" font-family="sans-serif" font-size="20" fill="#696958">MEMOVERSE CINEMA · SAMPLE</text><text x="85" y="295" font-family="sans-serif" font-size="48" fill="#222820">INTERSTELLAR</text><text x="85" y="355" font-family="sans-serif" font-size="22" fill="#535b4e">2026.10.22  /  19:30</text><text x="85" y="480" font-family="sans-serif" font-size="26" fill="#222820">HALL 03    ROW 05    SEAT 06</text><text x="85" y="555" font-family="sans-serif" font-size="16" fill="#696958">SYNTHETIC TEST TICKET · NOT VALID</text></svg>` });
    if (path === '/api/media') return respond(route, media());
    const data = currentData();
    if (path === '/api/tickets' && method === 'POST') { const ticket = sampleItem(body.id); ticket.fields = { title: '', cinema: '', hall: '', seat: '', showtime: '', ...body.fields }; ticket.media.original.cloudStatus = options.cloud === false ? 'not_configured' : 'saved'; data.tickets.push(ticket); }
    else if (path.startsWith('/api/tickets/')) { const ticket = data.tickets.find(item => item.id === decodeURIComponent(path.split('/')[3])); if (!ticket) return respond(route, { detail: '不存在' }, 404); if (method === 'PATCH') Object.assign(ticket.fields, body.fields); else if (method === 'DELETE') ticket.deletedAt = Date.now(); else return respond(route, {}, 404); ticket.version++; }
    else return respond(route, { detail: 'Not found' }, 404);
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
  await expect(page.getByText('本机持久缓存未启用，图片显示不代表已缓存')).toBeVisible();
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
  await page.addInitScript(() => { const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL); (window as any).__urls = []; (window as any).__revoked = []; URL.createObjectURL = value => { const url = create(value); (window as any).__urls.push(url); return url; }; URL.revokeObjectURL = url => { (window as any).__revoked.push(url); revoke(url); }; });
  await page.goto('/tickets/new');
  const select = () => page.getByLabel('选择原票照片').setInputFiles({ name: 'ticket.png', mimeType: 'image/png', buffer: png });
  await select(); await select(); await page.getByRole('link', { name: '取消', exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as any).__revoked.length)).toBe(2);
  await page.getByRole('link', { name: '上传电影票', exact: true }).click(); await select();
  await page.getByRole('button', { name: '退出登录' }).click();
  await expect.poll(() => page.evaluate(() => (window as any).__revoked.length)).toBe(3);
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

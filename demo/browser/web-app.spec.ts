import { expect, test, type Page, type Route } from '@playwright/test';

const password = 'ExamplePassword123!';
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=', 'base64');
const categories = ['travel', 'movie', 'daily', 'exhibition'];
const art = ['sea', 'lighthouse', 'cafe', 'exhibition'];
function dataFor(user: string) {
  const collections = Array.from({ length: 17 }, (_, index) => ({
    id: `item-${index + 1}`, title: index === 0 ? `${user.toUpperCase()} 的海边` : `珍藏片刻 ${index + 1}`,
    category: categories[index % 4], tags: ['纪念'], note: '把值得留念的日子，好好收藏。', date: '2026-10-14',
    addedAt: 100 - index, source: 'mine', art: art[index % 4], version: 1, generation: 'ready',
    assetId: `asset-${index}`, customImage: `/api/media/${index}`, modelUrl: `/api/models/${index}/model.glb`,
  }));
  return {
    collections,
    shares: Array.from({ length: 4 }, (_, index) => ({ id: `share-${index}`, sender: '小林', receivedAt: '2026-10-13', unread: index < 2,
      item: { ...collections[index], id: `shared-${index}`, source: 'friend', sender: '小林' } })),
    community: Array.from({ length: 6 }, (_, index) => ({ id: `community-${index}`, title: `社区灵感 ${index + 1}`, category: categories[index % 4], art: art[index % 4],
      author: '小雨', description: '一起收藏生活里的片刻。', exposures: 100, saves: 10, likes: 20, generation: 'ready',
      customImage: `/api/media/${index}`, modelUrl: `/api/models/${index}/model.glb` })),
    profile: { name: `用户 ${user.toUpperCase()}`, bio: '把喜欢的日子留下来' }, likedCommunityIds: [] as string[],
  };
}
function glb() {
  const vertices = new Float32Array([-1, -.7, 0, 1, -.7, 0, 1, .7, .1, -1, .7, 0]);
  const indices = new Uint16Array([0, 1, 2, 0, 2, 3]);
  const binary = Buffer.concat([Buffer.from(vertices.buffer), Buffer.from(indices.buffer)]);
  const document = { asset: { version: '2.0' }, scene: 0, scenes: [{ nodes: [0] }], nodes: [{ mesh: 0 }],
    meshes: [{ primitives: [{ attributes: { POSITION: 0 }, indices: 1, material: 0 }] }],
    materials: [{ doubleSided: true, pbrMetallicRoughness: { baseColorFactor: [.75, .8, .68, 1], metallicFactor: 0, roughnessFactor: 1 } }],
    buffers: [{ byteLength: binary.length }], bufferViews: [{ buffer: 0, byteOffset: 0, byteLength: vertices.byteLength, target: 34962 },
      { buffer: 0, byteOffset: vertices.byteLength, byteLength: indices.byteLength, target: 34963 }],
    accessors: [{ bufferView: 0, componentType: 5126, count: 4, type: 'VEC3', min: [-1, -.7, 0], max: [1, .7, .1] },
      { bufferView: 1, componentType: 5123, count: 6, type: 'SCALAR', min: [0], max: [3] }] };
  const json = Buffer.from(JSON.stringify(document));
  const padded = Buffer.concat([json, Buffer.alloc((4 - json.length % 4) % 4, 32)]);
  const output = Buffer.alloc(12 + 8 + padded.length + 8 + binary.length);
  output.writeUInt32LE(0x46546c67, 0); output.writeUInt32LE(2, 4); output.writeUInt32LE(output.length, 8);
  output.writeUInt32LE(padded.length, 12); output.writeUInt32LE(0x4e4f534a, 16); padded.copy(output, 20);
  output.writeUInt32LE(binary.length, 20 + padded.length); output.writeUInt32LE(0x004e4942, 24 + padded.length); binary.copy(output, 28 + padded.length);
  return output;
}
const model = glb();

async function mockApi(page: Page, options: { user?: string; configured?: boolean; google?: boolean; sessionGate?: Promise<void> } = {}) {
  const server = {
    user: options.user ?? '', configured: options.configured ?? true, google: options.google ?? false,
    requests: [] as { path: string; method: string; body: any; headers: Record<string, string> }[],
    data: new Map<string, ReturnType<typeof dataFor>>(), revision: 1, denyModel: false, conflictOnce: false,
    rejectVerification: false, unverifiedLogin: false, holdState: undefined as Promise<void> | undefined,
    csrfToken: undefined as string | undefined,
    logoutUrl: undefined as string | undefined,
  };
  const currentData = (user = server.user) => {
    if (!server.data.has(user)) server.data.set(user, dataFor(user));
    return server.data.get(user)!;
  };
  const session = () => ({ authenticated: !!server.user, configured: server.configured,
    providers: { google: server.google, email: true }, ...(server.user ? { user: { id: server.user, email: `${server.user}@example.test`, name: `用户 ${server.user.toUpperCase()}` }, csrfToken: server.csrfToken ?? `csrf-${server.user}` } : {}) });
  const state = (user = server.user) => ({ initialized: true, serverId: 'web-server', userId: user, revision: server.revision,
    data: structuredClone(currentData(user)), generationEngine: { available: true, reason: 'test engine' } });
  const respond = (route: Route, value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
  await page.addInitScript(() => {
    (window as any).__legacyReads = 0;
    indexedDB.open = (() => { (window as any).__legacyReads += 1; throw new Error('Legacy IndexedDB must not be read'); }) as typeof indexedDB.open;
  });
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    let body: any;
    try { body = request.postDataJSON(); } catch { body = request.postData(); }
    server.requests.push({ path, method, body, headers: request.headers() });
    if (path === '/api/auth/session') {
      await options.sessionGate;
      return respond(route, session()).catch(() => {});
    }
    if (path === '/api/auth/login') {
      if (server.unverifiedLogin) return respond(route, { detail: '请先验证邮箱。', code: 'email_unverified' }, 403);
      if (body.password !== password && body.password !== 'NewExamplePassword456!') return respond(route, { detail: '邮箱或密码不正确。' }, 401);
      server.user = body.email.startsWith('b') ? 'b' : 'a';
      return respond(route, session());
    }
    if (path === '/api/auth/register') return respond(route, { verificationRequired: true, email: body.email });
    if (path === '/api/auth/verify-email') return respond(route, server.rejectVerification ? { detail: '验证码不正确。' } : { verified: true }, server.rejectVerification ? 400 : 200);
    if (['/api/auth/resend-code', '/api/auth/forgot-password', '/api/auth/reset-password'].includes(path)) return respond(route, { ok: true });
    if (path === '/api/auth/logout') { server.user = ''; return respond(route, { ok: true, logoutUrl: server.logoutUrl }); }
    if (!server.user) return respond(route, { detail: '请登录。' }, 401);
    if (path === '/api/state') {
      const snapshot = state();
      const held = server.holdState;
      server.holdState = undefined;
      if (held) await held;
      return respond(route, snapshot).catch(() => {});
    }
    if (path.startsWith('/api/models/')) {
      if (server.denyModel) return respond(route, { detail: '登录已过期。' }, 401);
      return route.fulfill({ status: 200, contentType: 'model/gltf-binary', body: model });
    }
    if (path.startsWith('/api/media/')) {
      const index = Number(path.split('/').pop()) || 0;
      const colors = [['#c4d3d6', '#e7e6d8', '#82949a'], ['#c9bfab', '#eee9df', '#8c8575'], ['#ccd6c6', '#e9ece2', '#81917b']][index % 3];
      return route.fulfill({ contentType: 'image/svg+xml', body: `<svg xmlns="http://www.w3.org/2000/svg" width="600" height="450"><rect width="600" height="450" fill="${colors[0]}"/><circle cx="450" cy="90" r="48" fill="${colors[1]}"/><path d="M0 300L160 160L360 340L500 260L600 330V450H0Z" fill="${colors[2]}"/><path d="M0 370Q200 270 600 390V450H0Z" fill="${colors[1]}"/></svg>` });
    }
    if (path === '/api/media' && method === 'POST') return respond(route, { assetId: 'uploaded', url: '/api/media/uploaded', sha256: 'hash', width: 1, height: 1, size: 68, mimeType: 'image/png' });
    const data = currentData();
    if (path === '/api/collections' && method === 'POST') {
      data.collections.push({ ...body, tags: body.tags ?? [], addedAt: 1000, source: 'mine', version: 1, generation: 'queued',
        customImage: '/api/media/uploaded', generationJobId: 'new-job' } as any);
    } else if (path.startsWith('/api/collections/') && method === 'PATCH') {
      const item = data.collections.find(item => item.id === path.split('/')[3])!;
      if (server.conflictOnce) { server.conflictOnce = false; item.version = 3; server.revision += 1; return respond(route, { detail: '版本已更新，草稿仍保留。' }, 409); }
      if (body.version !== item.version) return respond(route, { detail: '版本已更新。' }, 409);
      Object.assign(item, body, { version: item.version + 1 });
    } else if (path.startsWith('/api/collections/') && method === 'DELETE') {
      data.collections = data.collections.filter(item => item.id !== path.split('/')[3]);
    } else if (path === '/api/profile') data.profile = body;
    else if (path === '/api/shares/read-all') data.shares.forEach(share => { share.unread = false; });
    else if (path.match(/^\/api\/shares\/[^/]+\/read$/)) data.shares.find(share => share.id === path.split('/')[3])!.unread = false;
    else if (path.match(/^\/api\/community\/[^/]+\/view$/)) data.community.find(item => item.id === path.split('/')[3])!.exposures += 1;
    else if (path.match(/^\/api\/community\/[^/]+\/like$/) && method === 'PUT') {
      const item = data.community.find(item => item.id === path.split('/')[3])!;
      const wasLiked = data.likedCommunityIds.includes(item.id);
      data.likedCommunityIds = data.likedCommunityIds.filter(id => id !== item.id);
      if (body.liked) data.likedCommunityIds.push(item.id);
      item.likes += Number(body.liked) - Number(wasLiked);
    } else if (path.match(/^\/api\/(community|shares)\/[^/]+\/collect$/) && method === 'POST') {
      const [, , source, id] = path.split('/');
      const shared = source === 'shares' ? data.shares.find(share => share.id === id)!.item : undefined;
      const community = source === 'community' ? data.community.find(item => item.id === id)! : undefined;
      data.collections.push(shared ? { ...shared, id: `collected-${id}` } : {
        ...data.collections[0], ...community, id: `collected-${id}`, source: 'friend', sender: community!.author,
        note: community!.description,
      });
    }
    server.revision += 1;
    return respond(route, state());
  });
  return server;
}

async function login(page: Page, email = 'a@example.test') {
  await page.getByLabel('邮箱地址', { exact: true }).fill(email);
  await page.getByLabel('密码', { exact: true }).fill(password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
}

test('session barrier shows no business, does not initialize browser data, and disables unavailable Google', async ({ page }, info) => {
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const api = await mockApi(page, { sessionGate: gate });
  await page.goto('/collection/item-1');
  await expect(page.getByText('正在确认登录状态…')).toBeVisible();
  await expect(page.locator('.memoverse-shell')).toHaveCount(0);
  expect(api.requests.every(request => request.path.startsWith('/api/auth/'))).toBeTruthy();
  release();
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Google 登录暂不可用' })).toBeDisabled();
  expect(await page.evaluate(() => (window as any).__legacyReads)).toBe(0);
  await expect(page.getByLabel('密码', { exact: true })).toHaveAttribute('autocomplete', 'current-password');
  await page.screenshot({ path: info.outputPath('auth-desktop.png'), fullPage: true });
});

test('native login opens the existing template with no device chrome', async ({ page }) => {
  const api = await mockApi(page);
  await page.goto('/login');
  await login(page);
  await expect(page.getByRole('heading', { name: '我的收藏', exact: true })).toBeVisible();
  await expect(page.locator('.collection-overview')).toContainText('17');
  await expect(page.locator('.sidebar')).toBeVisible();
  await expect(page.locator('.phone-device, .device-menu-bar, .keyboard-dock, .mobile-scroll')).toHaveCount(0);
  expect(await page.evaluate(() => (window as any).__legacyReads)).toBe(0);
  const request = api.requests.find(request => request.path === '/api/auth/login')!;
  expect(request.headers['x-memoverse-client']).toBe('web');
  expect(api.requests.some(request => request.path.includes('bootstrap'))).toBeFalsy();
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

for (const width of [360, 390, 768, 1440]) {
  test(`native browser layout at ${width}px has no page overflow or fake keyboard`, async ({ page }, info) => {
    await mockApi(page, { user: 'a' });
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/collection');
    await expect(page.getByRole('heading', { name: '我的收藏', exact: true })).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
    if (width >= 1024) await expect(page.locator('.sidebar')).toBeVisible();
    else await expect(page.getByRole('navigation', { name: '手机主导航' })).toBeVisible();
    await page.mouse.wheel(0, 650);
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThan(100);
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
    await page.screenshot({ path: info.outputPath(`collection-${width}.png`), fullPage: true });
    await page.goto('/add');
    await page.getByRole('textbox', { name: '名称', exact: true }).fill('浏览器原生输入');
    await expect(page.locator('.keyboard-dock, .phone-frame, .phone-device')).toHaveCount(0);
    await expect(page.getByLabel('日期', { exact: true })).toHaveAttribute('type', 'date');
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  });
}

test('detail deep links survive refresh and browser navigation; real WebGL controls stay bounded', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  await page.goto('/collection/item-1');
  await expect(page.getByRole('heading', { name: 'A 的海边', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: '浮雕向右', exact: true })).toBeEnabled();
  for (let index = 0; index < 9; index += 1) await page.getByRole('button', { name: '浮雕向右', exact: true }).click();
  expect(Number(await page.locator('.relief-viewport').getAttribute('data-tilt-degrees'))).toBeLessThanOrEqual(40);
  await page.reload();
  await expect(page.getByRole('heading', { name: 'A 的海边', exact: true })).toBeVisible();
  await page.getByRole('button', { name: /返回我的收藏/ }).click();
  await expect(page).toHaveURL(/\/collection$/);
  await page.getByRole('link', { name: /A 的海边/ }).first().click();
  await expect(page).toHaveURL(/\/collection\/item-1$/);
  await page.goBack();
  await expect(page.getByRole('heading', { name: '我的收藏', exact: true })).toBeVisible();
  expect(api.requests.some(request => request.path === '/api/models/0/model.glb')).toBeTruthy();
});

test('upload, edit, conflict recovery and document-portal delete preserve business semantics', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  await page.goto('/add');
  await page.getByLabel('选择收藏图片').setInputFiles({ name: 'photo.png', mimeType: 'image/png', buffer: png });
  await page.getByRole('textbox', { name: '名称', exact: true }).fill('新的一杯咖啡');
  await page.getByRole('textbox', { name: /标签/ }).fill('咖啡、周末');
  await page.getByRole('textbox', { name: /想记住的话/ }).fill('留下这一天');
  await page.getByRole('button', { name: '保存收藏', exact: true }).click();
  await expect(page.getByRole('heading', { name: '新的一杯咖啡' })).toBeVisible();
  const create = api.requests.find(request => request.path === '/api/collections' && request.method === 'POST')!;
  expect(create.body.tags).toEqual(['咖啡', '周末']);
  expect(create.headers['x-csrf-token']).toBe('csrf-a');
  expect(create.headers['x-memoverse-client']).toBe('web');
  await page.getByRole('link', { name: '编辑收藏' }).click();
  await page.getByRole('textbox', { name: /想记住的话/ }).fill('版本冲突后保留的草稿');
  api.conflictOnce = true;
  await page.getByRole('button', { name: '保存收藏', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('版本已更新');
  await expect(page.getByRole('textbox', { name: /想记住的话/ })).toHaveValue('版本冲突后保留的草稿');
  await page.getByRole('button', { name: '读取最新版本，保留草稿' }).click();
  await page.getByRole('button', { name: '保存收藏', exact: true }).click();
  await expect(page.getByRole('heading', { name: '新的一杯咖啡' })).toBeVisible();
  expect(api.requests.filter(request => request.method === 'PATCH').at(-1)?.body.version).toBe(3);
  await page.getByRole('button', { name: '删除收藏', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  expect(await page.getByRole('dialog').evaluate(element => element.closest('.memoverse-shell') === null)).toBeTruthy();
  await page.getByRole('dialog').getByRole('button', { name: '删除收藏', exact: true }).click();
  await expect(page.getByRole('heading', { name: '我的收藏', exact: true })).toBeVisible();
});

test('model authorization failure returns to login and removes all private business UI', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  api.denyModel = true;
  await page.goto('/collection/item-1');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(page.locator('.memoverse-shell')).toHaveCount(0);
});

test('logout discards a late account response before another account signs in', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  let release!: () => void;
  api.holdState = new Promise(resolve => { release = resolve; });
  await page.goto('/collection');
  await expect(page.getByRole('button', { name: '退出登录', exact: true }).first()).toBeVisible();
  await page.getByRole('button', { name: '退出登录', exact: true }).first().click();
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await login(page, 'b@example.test');
  await expect(page.getByRole('heading', { name: '我的收藏', exact: true })).toBeVisible();
  release();
  await expect(page.locator('.memoverse-shell')).toHaveAttribute('data-user-id', 'b');
  await expect(page.getByRole('link', { name: /B 的海边/ }).first()).toBeVisible();
  await expect(page.getByText('A 的海边', { exact: true })).toHaveCount(0);
});

test('Cognito logout leaves an anonymous document before redirect and returns signed out', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  await page.goto('/collection');
  await expect(page.getByRole('heading', { name: '我的收藏', exact: true })).toBeVisible();
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
  await page.goto('/collection');
  await expect(page.getByRole('heading', { name: '我的收藏', exact: true })).toBeVisible();
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
  await page.goto('/collection');
  await expect(page.getByRole('heading', { name: '我的收藏', exact: true })).toBeVisible();
  api.user = '';
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(page.locator('.memoverse-shell')).toHaveCount(0);
});

test('same-account session refresh retries a pending initial state request', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  let release!: () => void;
  const initialState = new Promise<void>(resolve => { release = resolve; });
  let delayedRequests = 0;
  await page.route('**/api/state', async route => {
    if (!api.csrfToken) { delayedRequests += 1; await initialState; }
    await route.fallback().catch(() => {});
  });
  try {
    await page.goto('/collection');
    await expect.poll(() => delayedRequests).toBeGreaterThan(0);
    await expect(page.getByRole('heading', { name: '正在打开你的收藏', exact: true })).toBeVisible();
    api.csrfToken = 'csrf-a-renewed';
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await expect(page.getByRole('heading', { name: '我的收藏', exact: true })).toBeVisible();
    await expect(page.locator('.memoverse-shell')).toHaveAttribute('data-user-id', 'a');
    expect(api.requests.some(request => request.path === '/api/state')).toBeTruthy();
  } finally { release(); }
});

test('same-account session refresh preserves an in-progress edit', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  await page.goto('/collection/item-1');
  await page.getByRole('link', { name: '编辑收藏' }).click();
  const note = page.getByRole('textbox', { name: /想记住的话/ });
  await note.fill('同账号会话刷新时保留这份草稿');
  const stateRequests = api.requests.filter(request => request.path === '/api/state').length;
  api.csrfToken = 'csrf-a-renewed';
  await page.evaluate(() => window.dispatchEvent(new Event('focus')));
  await expect.poll(() => api.requests.filter(request => request.path === '/api/state').length).toBeGreaterThan(stateRequests);
  await expect(note).toHaveValue('同账号会话刷新时保留这份草稿');
  await page.getByRole('button', { name: '保存收藏', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'A 的海边', exact: true })).toBeVisible();
  const update = api.requests.find(request => request.method === 'PATCH')!;
  expect(update.body.note).toBe('同账号会话刷新时保留这份草稿');
  expect(update.headers['x-csrf-token']).toBe('csrf-a-renewed');
});

test('gallery supporting text retains readable contrast on its light surfaces', async ({ page }) => {
  await mockApi(page, { user: 'a' });
  await page.goto('/collection');
  await expect(page.getByRole('heading', { name: '我的收藏', exact: true })).toBeVisible();
  const ratios = await page.locator('.card-caption p, .page-heading p, .sidebar-caption').evaluateAll(elements => {
    const luminance = (channels: number[]) => channels.slice(0, 3).map(value => value / 255)
      .map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
      .reduce((sum, value, index) => sum + value * [.2126, .7152, .0722][index], 0);
    return elements.map(element => {
      const color = getComputedStyle(element).color.match(/[\d.]+/g)!.map(Number);
      const foreground = luminance(color);
      return 1.05 / (foreground + .05);
    });
  });
  expect(ratios.length).toBeGreaterThan(0);
  expect(Math.min(...ratios)).toBeGreaterThanOrEqual(4.5);
});


test('explore filtering survives detail navigation and community actions update the account', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  await page.goto('/explore');
  await page.getByRole('group', { name: '探索分类' }).getByRole('button', { name: '旅行', exact: true }).click();
  await expect(page.locator('.explore-grid .photo-card')).toHaveCount(2);
  await page.locator('.explore-grid .photo-card').first().click();
  await expect(page.getByRole('heading', { name: '社区灵感 1', exact: true })).toBeVisible();
  await expect.poll(() => api.requests.some(request => request.path === '/api/community/community-0/view')).toBeTruthy();
  await page.getByRole('button', { name: '喜欢这件纪念品', exact: true }).click();
  await expect(page.getByRole('button', { name: '取消喜欢', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '加入我的收藏', exact: true }).click();
  await expect(page.getByText('已加入我的收藏', { exact: true })).toBeVisible();
  const liked = api.requests.find(request => request.path === '/api/community/community-0/like')!;
  expect(liked.method).toBe('PUT');
  expect(liked.body).toEqual({ liked: true });
  expect(liked.headers['x-csrf-token']).toBe('csrf-a');
  await page.goBack();
  await expect(page.getByRole('group', { name: '探索分类' }).getByRole('button', { name: '旅行', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.explore-grid .photo-card')).toHaveCount(2);
  await expect(page.locator('.community-byline').first()).toContainText('21 人喜欢');
  await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: '我的收藏', exact: true }).click();
  await expect(page.locator('.recent-grid').getByRole('heading', { name: '社区灵感 1', exact: true })).toBeVisible();
});

test('shares preserve unread filtering across detail navigation and add the received item to collections', async ({ page }) => {
  const api = await mockApi(page, { user: 'a' });
  await page.goto('/friends');
  await page.getByRole('group', { name: '分享筛选' }).getByRole('button', { name: /未查看/ }).click();
  await expect(page.locator('.friend-card')).toHaveCount(2);
  await page.locator('.friend-card .photo-card').first().click();
  await expect(page.getByRole('heading', { name: 'A 的海边', exact: true })).toBeVisible();
  await expect.poll(() => api.requests.some(request => request.path === '/api/shares/share-0/read')).toBeTruthy();
  await expect(page.locator('#main-content')).toHaveAttribute('aria-busy', 'false');
  await page.goBack();
  await expect(page.getByRole('group', { name: '分享筛选' }).getByRole('button', { name: /未查看/ })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.locator('.friend-card')).toHaveCount(1);
  await page.getByRole('button', { name: '收藏这份分享', exact: true }).click();
  await expect(page.getByText('已加入我的收藏', { exact: true })).toBeVisible();
  const collect = api.requests.find(request => request.path === '/api/shares/share-1/collect')!;
  expect(collect.method).toBe('POST');
  expect(collect.headers['x-csrf-token']).toBe('csrf-a');
  await page.getByRole('navigation', { name: '主导航', exact: true }).getByRole('link', { name: '我的收藏', exact: true }).click();
  await expect(page.locator('a[href="/collection/collected-share-1"]').first()).toBeVisible();
});

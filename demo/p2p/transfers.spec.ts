import { expect, test, chromium, type Browser, type BrowserContext, type Page } from '@playwright/test';

const origin = 'http://127.0.0.1:4177';
const fixtureHeaders = { 'X-MemoVerse-Fixture': 'p2p-browser-fixture', 'X-MemoVerse-Fixture-Run': process.env.MEMOVERSE_P2P_RUN_TOKEN! };
type Fault = 'none' | 'corrupt' | 'interrupt' | 'stall' | 'relay-only';
type Fixture = { userId: string; media: { assetId: string; sha256: string; size: number; mimeType: string } };
type Endpoint = { browser: Browser; context: BrowserContext; page: Page; fixture: Fixture; label: string };

// Observe real browser channels; fault cases alter only native channel calls in
// the isolated test browser. No production debug hooks or mocked RTC statistics.
async function observe(context: BrowserContext, fault: Fault) {
  await context.addInitScript(({ fault }) => {
    const state = { receivedBytes: 0, sentBytes: 0, binaryMessages: 0, channels: 0, interrupted: false, closed: 0, pcCreated: 0, pcClosed: 0, getOffsets: [] as number[], channelFirstOffsets: [] as number[], events: [] as unknown[] };
    (window as any).__peerObservation = state;
    const NativeSocket = window.WebSocket;
    window.WebSocket = class extends NativeSocket {
      constructor(url: string | URL, protocols?: string | string[]) {
        super(url, protocols);
        this.addEventListener('message', event => {
          try {
            const value = JSON.parse(event.data);
            state.events.push({ type: value.type, code: value.code, kind: value.kind, reason: value.reason, leaseMs: value.leaseMs });
            state.events = state.events.slice(-100);
          } catch { /* Only safe protocol metadata, never SDP, addresses or cookies. */ }
        });
      }
    };
    const observed = new WeakSet<RTCDataChannel>(), broken = new WeakSet<RTCDataChannel>(), binarySent = new WeakSet<RTCDataChannel>();
    const nativeSend = RTCDataChannel.prototype.send;
    RTCDataChannel.prototype.send = function(data: any) {
      if (!observed.has(this)) {
        observed.add(this); state.channels += 1;
        this.addEventListener('message', event => {
          if (event.data instanceof ArrayBuffer) { state.receivedBytes += event.data.byteLength; state.binaryMessages += 1; }
        });
        this.addEventListener('close', () => { state.closed += 1; });
      }
      if (broken.has(this)) return;
      if (typeof data === 'string') {
        try { const control = JSON.parse(data); if (control.type === 'get') state.getOffsets.push(control.offset); }
        catch { /* Ignore non-control text. */ }
      }
      if (data instanceof ArrayBuffer) {
        const offset = new DataView(data).getUint32(0);
        if (!binarySent.has(this)) { binarySent.add(this); state.channelFirstOffsets.push(offset); }
        if (fault === 'corrupt' && offset === 0) {
          data = data.slice(0); new Uint8Array(data)[8] ^= 1;
        }
        if ((fault === 'interrupt' && !state.interrupted || fault === 'stall') && offset >= 256 * 1024) {
          state.interrupted = true; broken.add(this);
          if (fault === 'interrupt') setTimeout(() => this.close(), 75);
          return;
        }
        state.sentBytes += data.byteLength;
      }
      return nativeSend.call(this, data);
    };
    const NativeConnection = window.RTCPeerConnection;
    window.RTCPeerConnection = class extends NativeConnection {
      constructor(config?: RTCConfiguration) { super(fault === 'relay-only' ? { ...config, iceTransportPolicy: 'relay' } : config); state.pcCreated += 1; }
      close() { if (this.signalingState !== 'closed') state.pcClosed += 1; super.close(); }
    };
  }, { fault });
}

async function endpoint(group: string, label: string, fault: Fault = 'none', who = 'alice'): Promise<Endpoint> {
  // Launch separate processes, not merely two tabs sharing a browser profile.
  const browser = await chromium.launch({ channel: 'chromium' });
  try {
    const context = await browser.newContext({ baseURL: origin, viewport: { width: 1440, height: 1000 } });
    await observe(context, fault);
    const response = await context.request.post('/__test/session', { headers: fixtureHeaders, data: { group, who, label } });
    expect(response.ok()).toBeTruthy();
    const fixture = await response.json();
    const page = await context.newPage();
    return { browser, context, page, fixture, label };
  } catch (error) { await browser.close(); throw error; }
}

async function ready(device: Endpoint, inventory = 0) {
  await device.page.goto('/devices');
  await expect(device.page.getByTestId('peer-status')).toHaveAttribute('data-enabled', 'true');
  await expect(device.page.getByTestId('peer-inventory')).toHaveAttribute('data-count', String(inventory));
}

async function load(device: Endpoint) {
  await device.page.getByRole('link', { name: '票夹', exact: true }).click();
  await expect.poll(() => cached(device), { timeout: 20_000 }).toEqual({ hash: device.fixture.media.sha256, size: device.fixture.media.size });
}

async function cached(device: Endpoint) {
  return device.page.evaluate(async ({ userId, media }) => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open('memoverse-media-cache-v1', 1);
      request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
    });
    try {
      if (!database.objectStoreNames.contains('media')) return null;
      const record = await new Promise<any>((resolve, reject) => {
        const tx = database.transaction('media', 'readonly'), request = tx.objectStore('media').get([userId, media.sha256]);
        request.onsuccess = () => resolve(request.result); request.onerror = () => reject(request.error);
      });
      if (!record) return null;
      const bytes = await record.blob.arrayBuffer(), digest = await crypto.subtle.digest('SHA-256', bytes);
      return { hash: [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join(''), size: bytes.byteLength };
    } finally { database.close(); }
  }, device.fixture);
}

async function mediaRequests(device: Endpoint) {
  const response = await device.context.request.get('/__test/status', { headers: fixtureHeaders });
  return (await response.json()).requests.filter((item: any) => item.label === device.label && item.path.startsWith('/api/media/'));
}

async function observation(device: Endpoint) {
  return device.page.evaluate(() => (window as any).__peerObservation);
}

async function transferRecords(device: Endpoint) {
  return device.page.getByTestId('peer-transfer').evaluateAll(items => items.map(item => ({
    direction: item.getAttribute('data-direction'), path: item.getAttribute('data-path'),
    route: item.getAttribute('data-route'), status: item.getAttribute('data-status'),
    bytes: Number(item.getAttribute('data-bytes')), controlBytes: Number(item.getAttribute('data-control-bytes')),
    discardedBytes: Number(item.getAttribute('data-discarded-bytes')), discardedPrefixBytes: Number(item.getAttribute('data-discarded-prefix')),
    timing: JSON.parse(item.getAttribute('data-timing') || '{}'),
  })));
}

async function pair(group: string, sourceFault: Fault = 'none', targetFault: Fault = 'none') {
  const source = await endpoint(group, group + '-source', sourceFault);
  let target: Endpoint | undefined;
  try {
    await ready(source); await load(source);
    await source.page.getByRole('link', { name: '设备与状态', exact: true }).click();
    await expect(source.page.getByTestId('peer-inventory')).toHaveAttribute('data-count', '1');
    target = await endpoint(group, group + '-target', targetFault);
    await ready(target);
    return { source, target, close: async () => { await Promise.all([source.browser.close(), target!.browser.close()]); } };
  } catch (error) { await Promise.all([source.browser.close(), target?.browser.close()]); throw error; }
}

test('two independent Chromium processes transfer verified bytes directly without a server media read', async ({}, testInfo) => {
  const devices = await pair('direct');
  try {
    const foreignToken = fixtureHeaders['X-MemoVerse-Fixture-Run'] === '0'.repeat(32) ? '1'.repeat(32) : '0'.repeat(32);
    expect((await devices.source.context.request.post('/__test/shutdown', {
      headers: { ...fixtureHeaders, 'X-MemoVerse-Fixture-Run': foreignToken },
    })).status()).toBe(403);
    expect((await devices.source.context.request.get('/api/health')).ok()).toBe(true);
    await load(devices.target);
    await testInfo.attach('protocol-observations.json', { body: JSON.stringify({ source: await observation(devices.source), target: await observation(devices.target) }), contentType: 'application/json' });
    expect(await mediaRequests(devices.target)).toEqual([]);
    const observed = await devices.target.page.evaluate(() => (window as any).__peerObservation);
    expect(observed.receivedBytes).toBeGreaterThan(devices.target.fixture.media.size);
    expect(observed.binaryMessages).toBeGreaterThan(32);
    await devices.target.page.getByRole('link', { name: '设备与状态', exact: true }).click();
    await expect(devices.target.page.locator('[data-testid="peer-transfer"][data-path="peer"][data-route="direct"][data-status="complete"]')).toHaveCount(1);
    await testInfo.attach('direct-transfer.json', { body: JSON.stringify({ fixture: devices.target.fixture, browserVersion: devices.target.browser.version(), verifiedCache: await cached(devices.target), observed, records: await transferRecords(devices.target), serverMediaRequests: 0, scope: 'same-host independent Chromium; fake Redis; local object store; no TURN or real S3' }), contentType: 'application/json' });
    await devices.target.page.reload();
    await expect(devices.target.page.getByTestId('peer-inventory')).toHaveAttribute('data-count', '1');
    await load(devices.target);
    expect(await mediaRequests(devices.target)).toEqual([]);
    expect((await observation(devices.target)).receivedBytes).toBe(0);
  } finally { await devices.close(); }
});

for (const [group, sourceFault, targetFault] of [
  ['checksum', 'corrupt', 'none'], ['no-route', 'none', 'relay-only'], ['stalled', 'stall', 'none'],
] as const) {
  test(`${group}: bounded peer failure falls back and caches only the verified local-server object`, async ({}, testInfo) => {
    const devices = await pair(group, sourceFault, targetFault);
    try {
      await load(devices.target);
      const reads = await mediaRequests(devices.target);
      expect(reads.filter((item: any) => item.path === '/api/media/' + devices.target.fixture.media.assetId)).toHaveLength(1);
      expect(reads.every((item: any) => item.status === 200)).toBeTruthy();
      await devices.target.page.getByRole('link', { name: '设备与状态', exact: true }).click();
      await expect(devices.target.page.locator('[data-testid="peer-transfer"][data-path="server"][data-status="complete"]')).toHaveCount(1);
      await expect(devices.target.page.locator('[data-testid="peer-transfer"][data-path="peer"][data-status="complete"]')).toHaveCount(0);
      const source = await observation(devices.source), target = await observation(devices.target);
      expect(target.pcCreated).toBeGreaterThan(0); expect(target.pcClosed).toBe(target.pcCreated);
      if (sourceFault === 'corrupt') expect(target.receivedBytes).toBeGreaterThan(devices.target.fixture.media.size);
      if (sourceFault === 'stall') { expect(source.interrupted).toBe(true); expect(target.receivedBytes).toBeGreaterThan(128 * 1024); }
      await testInfo.attach(`${group}-transfer.json`, { body: JSON.stringify({ source, target, records: await transferRecords(devices.target), mediaRequests: reads }), contentType: 'application/json' });
    } finally { await devices.close(); }
  });
}

test('one interrupted connection resumes its contiguous prefix within the original attempt budget', async ({}, testInfo) => {
  const devices = await pair('resume', 'interrupt');
  try {
    await load(devices.target);
    expect(await mediaRequests(devices.target)).toEqual([]);
    const source = await devices.source.page.evaluate(() => (window as any).__peerObservation);
    expect(source.interrupted).toBeTruthy(); expect(source.channels).toBe(2);
    const target = await observation(devices.target);
    expect(target.getOffsets).toHaveLength(2);
    expect(target.getOffsets[0]).toBe(0); expect(target.getOffsets[1]).toBeGreaterThan(0);
    expect(source.channelFirstOffsets).toEqual(target.getOffsets);
    // A successful resume does not send the complete file a second time.
    expect(source.sentBytes).toBeLessThan(devices.target.fixture.media.size * 1.3);
    await devices.target.page.getByRole('link', { name: '设备与状态', exact: true }).click();
    await expect(devices.target.page.locator('[data-testid="peer-transfer"][data-path="peer"][data-status="complete"]')).toHaveCount(1);
    await testInfo.attach('resume-transfer.json', { body: JSON.stringify({ source, target: await observation(devices.target), records: await transferRecords(devices.target), media: devices.target.fixture.media, serverMediaRequests: 0 }), contentType: 'application/json' });
  } finally { await devices.close(); }
});

test('logout cancels an active transfer before caching bytes or starting a server download', async () => {
  const devices = await pair('logout', 'stall');
  try {
    await devices.target.page.getByRole('link', { name: '票夹', exact: true }).click();
    await expect.poll(async () => (await observation(devices.target)).receivedBytes, { intervals: [20, 50, 100] }).toBeGreaterThan(128 * 1024);
    await devices.target.page.getByRole('button', { name: '退出登录', exact: true }).click();
    await expect(devices.target.page.getByRole('heading', { name: '欢迎回来', exact: true })).toBeVisible();
    await expect.poll(async () => (await observation(devices.target)).closed).toBeGreaterThan(0);
    expect(await cached(devices.target)).toBeNull();
    expect(await mediaRequests(devices.target)).toEqual([]);
    const current = await devices.source.context.request.get('/api/auth/session');
    expect(current.ok()).toBeTruthy(); expect((await current.json()).authenticated).toBe(true);
  } finally { await devices.close(); }
});

test('account isolation and a withdrawn local cache prevent peer discovery', async () => {
  const devices = await pair('isolation');
  let other: Endpoint | undefined;
  try {
    other = await endpoint('isolation', 'isolation-bob', 'none', 'bob');
    await ready(other);
    const forbidden = await other.context.request.get('/api/media/' + devices.source.fixture.media.assetId);
    expect(forbidden.status()).toBe(404);
    await expect(other.page.getByText('当前没有其他可用页面，缺少的图片从云端获取。', { exact: true })).toBeVisible();
    await devices.source.page.getByRole('button', { name: '清除本账号本机缓存', exact: true }).click();
    await expect(devices.source.page.getByTestId('peer-inventory')).toHaveAttribute('data-count', '0');
    await load(devices.target);
    expect((await mediaRequests(devices.target)).filter((item: any) => !item.path.endsWith('/download'))).toHaveLength(1);
  } finally { await Promise.all([devices.close(), other?.browser.close()]); }
});

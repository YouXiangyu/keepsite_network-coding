#!/usr/bin/env node
// Real Android JNI/WebRTC and production Web UI against the owned loopback fixture.
import { createRequire } from 'node:module';
import { readFile, writeFile, mkdir, readdir } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import assert from 'node:assert/strict';
import { checkedNewOutput } from './browser-benchmark-lib.mjs';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const prepare = args.includes('--prepare');
const execute = args.includes('--execute');
if (!prepare && !execute) {
  console.log('Plan: use the owned 4180 fixture, warm a real Chromium source, run the installed Android instrumentation, then verify the reverse transfer in a fresh Chromium process. No cloud calls. Pass --prepare to seed two synthetic tickets, or --execute --serial emulator-5560 --output <new directory> after installing the loopback-only test APKs.');
  process.exit(0);
}
assert(!(prepare && execute), 'Choose exactly one mode');
const option = name => { const index = args.indexOf(name); return index >= 0 ? args[index + 1] : undefined; };
const control = JSON.parse((await readFile(resolve(root, '.cache/m7-native-control.json'), 'utf8')).replace(/^\uFEFF/, ''));
assert.equal(control.origin, 'http://127.0.0.1:4180');
assert.match(control.runToken, /^[a-f0-9]{32}$/);
const owned = JSON.parse(await readFile(resolve(root, `.cache/native-run-${control.runToken}.json`), 'utf8'));
assert.equal(owned.port, 4180); assert.match(owned.runId, /^native-browser-[a-z0-9_]{8}$/);
const origin = control.origin;
const headers = { 'X-MemoVerse-Fixture': 'native-browser-fixture', 'X-MemoVerse-Fixture-Run': control.runToken };
const health = await fetch(origin + '/api/health', { signal: AbortSignal.timeout(10000) }).then(r => r.json());
assert.equal(health.fixture, 'native-loopback'); assert.equal(health.cloud, false);

async function fixture(path, body, { diagnostic = false } = {}) {
  const response = await fetch(origin + path, { method: body ? 'POST' : 'GET', signal: AbortSignal.timeout(10000), headers: { ...headers, ...(body ? { 'Content-Type': 'application/json' } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
  assert.equal(response.status, 200, 'Owned test control failed');
  const result = await response.json();
  if (path === '/__test/progress' && !body && !diagnostic) {
    assert.equal(result.mediaRequestsOverflow, false, 'HTTP observation ledger overflowed');
    assert.equal(result.mediaRequestCount, result.mediaRequests.length, 'HTTP observation ledger is incomplete');
  }
  return result;
}
async function seed() {
  const response = await fetch(origin + '/api/auth/login', { method: 'POST', signal: AbortSignal.timeout(10000), headers: { Origin: origin, 'X-MemoVerse-Client': 'web', 'Content-Type': 'application/json' },
    body: JSON.stringify({ email: 'alice@example.test', password: 'TestTicket123!' }) });
  assert.equal(response.status, 200);
  const session = await response.json();
  const cookie = response.headers.getSetCookie().map(value => value.split(';')[0]).join('; ');
  assert(session.authenticated && cookie);
  try {
    const seeded = await fetch(origin + '/__test/bootstrap', { method: 'POST', signal: AbortSignal.timeout(30000), headers: { ...headers, Origin: origin, 'X-MemoVerse-Client': 'web', 'X-CSRF-Token': session.csrfToken, Cookie: cookie, 'Content-Type': 'application/json' }, body: JSON.stringify({ count: 2 }) });
    assert.equal(seeded.status, 200);
    return seeded.json();
  } finally {
    const logout = await fetch(origin + '/api/auth/logout', { method: 'POST', signal: AbortSignal.timeout(10000), headers: { Origin: origin, 'X-MemoVerse-Client': 'web', 'X-CSRF-Token': session.csrfToken, Cookie: cookie, 'Content-Type': 'application/json' }, body: '{}' });
    assert.equal(logout.status, 200, 'Seed session cleanup failed');
  }
}
// A deliberate test-run boundary, never an automatic retry for rejected logins.
await fixture('/__test/reset-auth-limits', {});
const manifest = await seed();
if (prepare) {
  await writeFile(resolve(root, '.cache/m7-native-manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
  console.log(JSON.stringify({ prepared: true, tickets: manifest.count, media: new Set(manifest.items.map(item => item.media.assetId)).size }));
  process.exit(0);
}
const serial = option('--serial');
assert.match(serial || '', /^emulator-\d{4}$/);
const output = await checkedNewOutput(option('--output') || '');
assert(output.startsWith(resolve(root, '.cache') + '\\') || output.startsWith(resolve(root, '.cache') + '/'), 'Output must be a new directory inside this workspace cache');
await mkdir(output);
assert.deepEqual(Object.keys((await fixture('/__test/progress')).phases), [], 'Start a fresh owned fixture for each interop attempt');
const adb = process.env.MEMOVERSE_ADB || 'D:/300-tools/Android/Sdk/platform-tools/adb.exe';
function command(arguments_, { timeout = 180000 } = {}) {
  const child = spawn(adb, ['-s', serial, ...arguments_], { windowsHide: true, env: { ...process.env, ANDROID_USER_HOME: resolve(root, '.cache/m7-android-user') }, stdio: ['ignore', 'pipe', 'pipe'] });
  let stdout = '', stderr = '', failure;
  const abort = message => { failure ??= new Error(message); child.kill(); };
  child.stdout.on('data', chunk => { stdout += chunk; if (stdout.length > 1024 * 1024) abort('Owned Android stdout exceeded its bound'); });
  child.stderr.on('data', chunk => { stderr += chunk; if (stderr.length > 256 * 1024) abort('Owned Android stderr exceeded its bound'); });
  const result = new Promise((resolve_, reject) => {
    const timer = setTimeout(() => abort('Owned Android command deadline exceeded'), timeout);
    child.once('error', error => { clearTimeout(timer); failure ??= error; });
    // Settle only after the exact owned child has exited and both pipes have closed.
    child.once('close', code => {
      clearTimeout(timer);
      const redact = value => value.replaceAll(control.runToken, '[redacted-run-token]');
      if (failure || code !== 0) reject(failure || new Error(`Owned Android command failed (${code}): ${redact(stderr.slice(-1000))}`));
      else resolve_({ stdout: redact(stdout), stderr: redact(stderr) });
    });
  });
  return { child, result };
}
assert.match((await command(['emu', 'avd', 'name']).result).stdout, /^MemoVerseM7\r?\n/);
const { chromium } = createRequire(resolve(root, 'demo/package.json'))('playwright');
const selected = { userId: manifest.userId, media: manifest.items[0].media };
let source, target, instrumentation, instrumentFinished = false, instrumentSucceeded = false;
const report = { scope: 'One owned API 36.1 emulator and separate Chromium processes; real JNI/DataChannel; real BFF SID/JWT validation with synthetic provider, fake Redis and local objects. Not physical Android, Cognito, S3, TURN, WAN or performance evidence.',
  media: selected.media, startedAt: new Date().toISOString(), passed: false, browserMdnsDisabledForEmulatorRouting: true };
const apkFiles = {
  'io.github.youxiangyu.memoverse': 'android/app/build/outputs/apk/debug/app-debug.apk',
  'io.github.youxiangyu.memoverse.test': 'android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk',
};
async function hashes() {
  const files = new Map();
  async function tree(relative, predicate, binary = false) {
    for (const item of await readdir(resolve(root, relative), { withFileTypes: true })) {
      assert(!item.isSymbolicLink(), 'Verification inputs must not traverse links');
      const next = `${relative}/${item.name}`;
      if (item.isDirectory()) {
        if (!item.name.startsWith('.') && !['__pycache__', 'node_modules', 'data', 'data-v2', 'data-v3'].includes(item.name)) await tree(next, predicate, binary);
      } else if (predicate(next)) files.set(next, binary || /\.(jar|png|webp|jpg|jpeg|ico|woff2?)$/.test(next));
    }
  }
  await tree('backend', name => name.endsWith('.py') && !name.startsWith('backend/tests/') || /requirements[^/]*\.txt$/.test(name));
  await tree('android/app/src', () => true);
  await tree('android/gradle', () => true);
  await tree('demo/src', () => true);
  await tree('demo/dist/client', () => true, true);
  for (const name of ['scripts/verify-android.mjs', 'scripts/browser-benchmark-lib.mjs', 'backend/tests/native-server.py',
    'backend/tests/test_native_fixture.py', 'android/gradlew', 'android/gradlew.bat', 'android/build.gradle.kts',
    'android/settings.gradle.kts', 'android/gradle.properties', 'android/app/build.gradle.kts',
    'demo/package.json', 'demo/package-lock.json', 'demo/vite.config.ts']) files.set(name, false);
  for (const name of Object.values(apkFiles)) files.set(name, true);
  const result = {};
  for (const [name, binary] of [...files].sort(([left], [right]) => left.localeCompare(right))) {
    const raw = await readFile(resolve(root, name));
    result[name] = { sha256: createHash('sha256').update(binary ? raw : raw.toString('utf8').replaceAll('\r\n', '\n')).digest('hex'),
      representation: binary ? 'raw-bytes' : 'utf8-lf' };
  }
  return result;
}
async function installedApks(inputs) {
  const result = {};
  for (const [packageName, file] of Object.entries(apkFiles)) {
    const paths = (await command(['shell', 'pm', 'path', packageName], { timeout: 10000 }).result).stdout.trim().split(/\r?\n/);
    assert.equal(paths.length, 1, 'Expected one installed monolithic verification APK');
    assert.match(paths[0], /^package:\/data\/app\/[A-Za-z0-9_+=~./-]+\/base\.apk$/);
    const hash = (await command(['shell', 'sha256sum', paths[0].slice('package:'.length)], { timeout: 10000 }).result).stdout.match(/^([a-f0-9]{64})\s/);
    assert(hash, 'Device APK hash unavailable');
    assert.equal(hash[1], inputs[file].sha256, 'Installed APK differs from the compiled verification artifact');
    result[packageName] = hash[1];
  }
  return result;
}
async function completedTransfer(device, direction) {
  const completed = device.page.locator(`[data-testid="peer-transfer"][data-direction="${direction}"][data-path="peer"][data-status="complete"]`);
  return until(async () => {
    const values = await completed.evaluateAll(items => items.map(item => ({ direction: item.getAttribute('data-direction'),
      path: item.getAttribute('data-path'), status: item.getAttribute('data-status'), bytes: Number(item.getAttribute('data-bytes')) })));
    return values.find(value => value.bytes === selected.media.size);
  }, `Web ${direction} side did not confirm transfer completion`, 15000);
}
async function until(predicate, message, timeout = 60000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const value = await predicate(); if (value) return value;
    await new Promise(resolve_ => setTimeout(resolve_, 100));
  }
  throw new Error(message);
}
async function browser() {
  const instance = await chromium.launch({ channel: 'chromium', args: ['--disable-features=WebRtcHideLocalIpsWithMdns'], env: { ...process.env, TEMP: resolve(root, '.cache/android-temp'), TMP: resolve(root, '.cache/android-temp') } });
  try {
    const context = await instance.newContext({ baseURL: origin, viewport: { width: 1440, height: 1000 } });
    await context.addInitScript(() => {
      window.__nativeInterop = { sentBytes: 0, receivedBytes: 0, sentFrames: 0, receivedFrames: 0, pcCreated: 0, pcClosed: 0, events: [] };
      const observed = new WeakSet(), nativeSend = RTCDataChannel.prototype.send, NativePC = RTCPeerConnection, NativeWS = WebSocket;
      let peerSocket;
      window.__nativeFindSources = assetId => new Promise((resolve_, reject) => {
        if (!/^[a-f0-9]{64}$/.test(assetId) || !peerSocket || peerSocket.readyState !== WebSocket.OPEN) { reject(new Error('Registered peer socket unavailable')); return; }
        const socket = peerSocket, requestId = crypto.randomUUID();
        const timer = setTimeout(() => { socket.removeEventListener('message', received); reject(new Error('Peer source probe timed out')); }, 3000);
        function received(event) {
          try {
            const value = JSON.parse(event.data);
            if (value.requestId !== requestId) return;
            clearTimeout(timer); socket.removeEventListener('message', received);
            if (value.type !== 'peer.sources' || value.assetId !== assetId || !Array.isArray(value.peers)) { reject(new Error('Peer source probe rejected')); return; }
            resolve_(value.peers.map(peer => ({ peerId: peer.peerId, name: peer.name })));
          } catch { /* Unrelated production frames remain owned by the application. */ }
        }
        socket.addEventListener('message', received);
        socket.send(JSON.stringify({ v: 1, type: 'peer.find', requestId, assetId }));
      });
      RTCDataChannel.prototype.send = function(data) {
        if (!observed.has(this)) {
          observed.add(this);
          this.addEventListener('message', event => { if (event.data instanceof ArrayBuffer) { window.__nativeInterop.receivedBytes += event.data.byteLength; window.__nativeInterop.receivedFrames++; } });
        }
        if (data instanceof ArrayBuffer) { window.__nativeInterop.sentBytes += data.byteLength; window.__nativeInterop.sentFrames++; }
        return nativeSend.call(this, data);
      };
      window.RTCPeerConnection = class extends NativePC {
        constructor(config) { super(config); window.__nativeInterop.pcCreated++; }
        close() { if (this.signalingState !== 'closed') window.__nativeInterop.pcClosed++; super.close(); }
      };
      window.WebSocket = class extends NativeWS {
        constructor(url, protocols) { super(url, protocols); peerSocket = this; this.addEventListener('message', event => {
          try { const value = JSON.parse(event.data); const list = window.__nativeInterop.events; if (list.length < 200) list.push({ type: value.type, code: value.code, kind: value.kind, reason: value.reason }); } catch { }
        }); }
      };
    });
    const login = await context.request.post('/api/auth/login', { headers: { Origin: origin, 'X-MemoVerse-Client': 'web' }, data: { email: 'alice@example.test', password: 'TestTicket123!' } });
    assert(login.ok());
    const page = await context.newPage(); await page.goto('/devices');
    await until(async () => (await page.getByTestId('peer-status').getAttribute('data-enabled')) === 'true', 'Web peer not registered');
    return { instance, context, page };
  } catch (error) { await instance.close(); throw error; }
}
async function cached(device, selection = selected) {
  return device.page.evaluate(async ({ userId, media }) => {
    const database = await new Promise((resolve_, reject) => { const request = indexedDB.open('memoverse-media-cache-v1', 1); request.onsuccess = () => resolve_(request.result); request.onerror = () => reject(request.error); });
    try {
      if (!database.objectStoreNames.contains('media')) return null;
      const record = await new Promise((resolve_, reject) => { const query = database.transaction('media', 'readonly').objectStore('media').get([userId, media.sha256]); query.onsuccess = () => resolve_(query.result); query.onerror = () => reject(query.error); });
      if (!record) return null;
      const bytes = await record.blob.arrayBuffer(); const digest = await crypto.subtle.digest('SHA-256', bytes);
      return { sha256: [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join(''), size: bytes.byteLength, mimeType: record.blob.type };
    } finally { database.close(); }
  }, selection);
}
try {
  report.inputSha256Before = await hashes();
  report.installedApkSha256Before = await installedApks(report.inputSha256Before);
  const startup = (await fixture('/__test/progress')).sourceSha256AtStartup;
  assert(startup && Object.keys(startup).length > 1, 'Fixture startup source fingerprint unavailable');
  for (const [name, sha256] of Object.entries(startup)) assert.equal(report.inputSha256Before[name]?.sha256, sha256, 'Running fixture does not match the current backend source');
  report.fixtureSourceSha256AtStartup = startup;
  source = await browser(); report.chromium = source.instance.version();
  await source.page.getByRole('link', { name: '票夹', exact: true }).click();
  const sourceMedia = [...new Map(manifest.items.map(item => [item.media.assetId, item.media])).values()];
  assert.equal(sourceMedia.length, 2, 'The fixture must expose exactly two unique synthetic originals');
  report.webSourceCache = [];
  for (const media of sourceMedia) {
    const verified = await until(() => cached(source, { userId: manifest.userId, media }), 'Web source did not cache both synthetic originals');
    assert.equal(verified.sha256, media.sha256); assert.equal(verified.size, media.size); assert.equal(verified.mimeType, media.mimeType);
    report.webSourceCache.push({ assetId: media.assetId, ...verified });
  }
  await source.page.getByRole('link', { name: '设备与状态', exact: true }).click();
  await until(async () => Number(await source.page.getByTestId('peer-inventory').getAttribute('data-count')) === sourceMedia.length, 'Both verified source images must be advertised');
  report.webSource = await cached(source); assert.equal(report.webSource.sha256, selected.media.sha256);
  await fixture('/__test/progress', { phase: 'web_ready', data: { assetId: selected.media.assetId, sha256: selected.media.sha256 } });
  console.log('Web source ready; starting real Android interop instrumentation');
  instrumentation = command(['shell', 'am', 'instrument', '-w', '-r', '-e', 'class', 'io.github.youxiangyu.memoverse.core.NativeInteropInstrumentedTest#nativeWebRoundTripAndAccountIsolation', '-e', 'runToken', control.runToken, 'io.github.youxiangyu.memoverse.test/androidx.test.runner.AndroidJUnitRunner']);
  instrumentation.result.then(() => { instrumentFinished = true; }, () => { instrumentFinished = true; });
  const native = await until(async () => {
    const status = await fixture('/__test/progress');
    if (instrumentFinished && !status.phases.native_received) throw new Error('Android ended before receiving a peer file');
    return status.phases.native_received;
  }, 'Android did not receive a peer file', 90000);
  assert.equal(native.sha256, selected.media.sha256); assert.equal(native.source, 'peer'); report.nativeReceived = native;
  report.webSendCompletion = await completedTransfer(source, 'send');
  report.sourceObservation = await source.page.evaluate(() => window.__nativeInterop);
  assert(report.sourceObservation.sentBytes > selected.media.size);
  await source.instance.close(); source = null;
  target = await browser();
  await until(async () => (await target.page.getByText('Android', { exact: true }).count()) > 0, 'Android source not listed');
  report.advertisedNativeSource = await until(async () => {
    const peers = await target.page.evaluate(assetId => window.__nativeFindSources(assetId), selected.media.assetId);
    return peers.find(peer => peer.name === 'Android');
  }, 'Android has not advertised the selected verified cache file', 30000);
  assert.equal(await cached(target), null, 'The isolated reverse browser must begin without the selected cache file');
  const before = await fixture('/__test/progress');
  await target.page.getByRole('link', { name: '票夹', exact: true }).click();
  report.webReceived = await until(() => cached(target), 'Web did not receive the native cached file');
  assert.equal(report.webReceived.sha256, selected.media.sha256); assert.equal(report.webReceived.size, selected.media.size);
  assert.equal(report.webReceived.mimeType, selected.media.mimeType);
  await target.page.getByRole('link', { name: '设备与状态', exact: true }).click();
  report.webReceiveCompletion = await completedTransfer(target, 'receive');
  report.targetObservation = await target.page.evaluate(() => window.__nativeInterop);
  assert(report.targetObservation.receivedBytes > selected.media.size, 'Reverse path must carry real DataChannel frames');
  const after = await fixture('/__test/progress');
  const selectedWebReads = after.mediaRequests.slice(before.mediaRequests.length).filter(item => item.client === 'web' && item.assetId === selected.media.assetId);
  assert.equal(selectedWebReads.length, 0, 'Reverse selected file must not use HTTP media fallback');
  report.selectedReverseHttpReads = selectedWebReads.length;
  await fixture('/__test/progress', { phase: 'web_received', data: { ...report.webReceived, assetId: selected.media.assetId } });
  const instrument = await instrumentation.result; instrumentFinished = true;
  await writeFile(resolve(output, 'instrumentation.txt'), instrument.stdout + instrument.stderr);
  assert.match(instrument.stdout, /OK \(1 test\)/); assert(!instrument.stdout.includes('FAILURES!!!'));
  report.fixture = await fixture('/__test/progress');
  assert(report.fixture.phases.native_sent && report.fixture.phases.native_complete);
  instrumentSucceeded = true; report.passed = true;
} catch (error) {
  report.error = String(error.message || error);
  report.fixture = await fixture('/__test/progress', null, { diagnostic: true }).catch(() => null);
  if (source) report.sourceObservation = await source.page.evaluate(() => window.__nativeInterop).catch(() => null);
  if (target) report.targetObservation = await target.page.evaluate(() => window.__nativeInterop).catch(() => null);
  if (instrumentation && instrumentFinished) {
    const result = await instrumentation.result.catch(() => null);
    if (result) await writeFile(resolve(output, 'instrumentation.txt'), result.stdout + result.stderr);
  }
  throw error;
} finally {
  const cleanupErrors = [];
  for (const result of await Promise.allSettled([source?.instance.close(), target?.instance.close()])) {
    if (result.status === 'rejected') cleanupErrors.push('Owned Chromium process did not close');
  }
  if (instrumentation && !instrumentSucceeded) {
    try { await command(['shell', 'am', 'force-stop', 'io.github.youxiangyu.memoverse'], { timeout: 10000 }).result; }
    catch { cleanupErrors.push('Could not stop the owned failed Android instrumentation'); }
    if (!instrumentFinished) instrumentation.child.kill();
    await instrumentation.result.catch(() => {}); // This settles only after exact child close.
  }
  try {
    report.inputSha256After = await hashes();
    report.inputsUnchanged = JSON.stringify(report.inputSha256Before) === JSON.stringify(report.inputSha256After);
    assert(report.inputsUnchanged, 'Source, Web distribution or APK changed during verification');
    report.installedApkSha256After = await installedApks(report.inputSha256After);
    assert.deepEqual(report.installedApkSha256Before, report.installedApkSha256After, 'Installed APK changed during verification');
  } catch (error) {
    cleanupErrors.push(String(error.message || error));
  }
  report.cleanupErrors = cleanupErrors;
  report.cleanupComplete = cleanupErrors.length === 0;
  if (!report.cleanupComplete) report.passed = false;
  report.finishedAt = new Date().toISOString();
  await writeFile(resolve(output, 'report.json'), JSON.stringify(report, null, 2) + '\n');
  if (!report.cleanupComplete) throw new Error('Verification cleanup or artifact identity check failed; inspect report.json');
}
console.log('Native/Web round trip, account isolation, background HTTP cancellation and artifact identity checks passed');

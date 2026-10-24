#!/usr/bin/env node
/** Local, synthetic two-process browser benchmark. Plan-only without --execute. */
import { spawn, execFileSync } from 'node:child_process';
import { createHash, randomBytes } from 'node:crypto';
import { access, mkdir, readFile, readdir, writeFile, lstat, rmdir } from 'node:fs/promises';
import { realpathSync } from 'node:fs';
import { createRequire } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { checkedNewOutput, installObserver, sampleIndices, summarizeRun, validateManifest } from './browser-benchmark-lib.mjs';

const ROOT = fileURLToPath(new URL('../', import.meta.url));
const ORIGIN = 'http://127.0.0.1:4178';
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const sha256 = data => createHash('sha256').update(data).digest('hex');
const exists = async name => { try { await access(name); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; } };

export function parseArguments(argv) {
  const args = { execute: false, count: 300, sample: 60, output: null, python: process.platform === 'win32' ? path.join(ROOT, 'backend/.venv/Scripts/python.exe') : 'python3' };
  const fields = { '--count': 'count', '--sample': 'sample', '--output': 'output', '--python': 'python' };
  for (let index = 0; index < argv.length; index++) {
    if (argv[index] === '--execute') { args.execute = true; continue; }
    const key = fields[argv[index]], value = argv[++index];
    if (!key || !value || value.startsWith('--')) throw new Error('Usage: node scripts/benchmark-browser.mjs --output ABSOLUTE_NEW_DIRECTORY [--count 300 --sample 60 --python EXECUTABLE] [--execute]');
    args[key] = ['count', 'sample'].includes(key) ? Number(value) : value;
  }
  if (!args.output) throw new Error('--output is required');
  sampleIndices(args.count, args.sample);
  return args;
}

async function hashes() {
  const files = new Set();
  async function tree(relative, predicate) {
    const directory = path.join(ROOT, relative);
    if (!await exists(directory)) throw new Error(`Required source directory missing: ${relative}`);
    for (const entry of await readdir(directory, { withFileTypes: true })) {
      const next = relative + '/' + entry.name;
      if (entry.isSymbolicLink()) throw new Error('Hash input must not be a link');
      if (entry.isDirectory()) { if (!['__pycache__', '.venv', 'node_modules', 'data', 'data-v2', 'data-v3'].includes(entry.name)) await tree(next, predicate); }
      else if (predicate(next)) files.add(next);
    }
  }
  await tree('backend', name => /\.py$|requirements[^/]*\.txt$/.test(name) && !name.startsWith('backend/tests/') || name === 'backend/tests/account_fixtures.py');
  await tree('demo/src', name => /\.(ts|tsx|css)$/.test(name));
  await tree('demo/benchmark', () => true);
  await tree('.cache/m6-bench-client', () => true);
  for (const name of ['scripts/benchmark-browser.mjs', 'scripts/browser-benchmark-lib.mjs', 'demo/tests/browser_fixture.py', 'demo/tests/benchmark-server.py', 'demo/vite.benchmark.config.ts', 'demo/package.json', 'demo/package-lock.json']) files.add(name);
  const result = {};
  for (const name of [...files].sort()) {
    const raw = await readFile(path.join(ROOT, name));
    result[name] = sha256(name.startsWith('.cache/m6-bench-client/') ? raw : raw.toString('utf8').replaceAll('\r\n', '\n'));
  }
  return result;
}

async function waitFor(action, description, milliseconds = 20000) {
  const deadline = Date.now() + milliseconds;
  while (Date.now() < deadline) { if (await action()) return; await sleep(50); }
  throw new Error(`Timed out: ${description}`);
}

async function boundedExit(promise, milliseconds) {
  let timer;
  try { return await Promise.race([promise, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Owned fixture exit deadline exceeded')), milliseconds); })]); }
  finally { clearTimeout(timer); }
}

async function execute(args, output) {
  const token = randomBytes(16).toString('hex');
  const headers = { 'X-Keepsite-Fixture': 'p2p-browser-fixture', 'X-Keepsite-Fixture-Run': token };
  const ownership = path.join(ROOT, '.cache', `m6-run-${token}.json`);
  const report = {
    schemaVersion: 1, createdAt: new Date().toISOString(), passed: false,
    sourceHashMethod: { sources: 'SHA-256 of UTF-8 source after CRLF-to-LF normalization', '.cache/m6-bench-client': 'SHA-256 of actual compiled bundle bytes without normalization' },
    environment: { node: process.version, platform: process.platform, architecture: process.arch, osRelease: os.release(), cpuModel: os.cpus()[0]?.model, logicalCpus: os.cpus().length, totalMemoryBytes: os.totalmem(), gitHead: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: ROOT, encoding: 'utf8' }).trim() },
    method: { profile: args.count === 300 && args.sample === 60 ? 'full-300-by-60' : 'smoke-custom', datasetCount: args.count, sampleCount: args.sample, indices: sampleIndices(args.count, args.sample), taskConcurrencies: [1, 2, 4], browserProcessesPerCell: 2, cases: ['http-cold', 'peer-cold', 'cache-warm'], taskDeadlineMs: 120000, p50: 'median', p95: 'nearest-rank ceil(n * 0.95)', sourceWarmup: 'same selected assets; HTTP only; separately recorded for every cell', warmTarget: 'same context after successful read, independent verification and reload', limits: 'production 2 peer transfers and 4 media downloads unchanged; no added pacing' },
    limitations: ['One local descriptive run; not a capacity guarantee or an EC2/S3 cost estimate.', 'Independent Chromium processes on one host; synthetic PNGs, fake Redis and local object storage.', 'HTTP response-body bytes and WebSocket application-payload bytes exclude headers, TLS, TCP, websocket framing and ping/pong.', 'Server response duration runs from the measurement middleware to the final ASGI body send; it is not browser network latency or TTFB.', 'Bootstrap phases measure explicit metadata boot after the HTML/JS entry has loaded; initial static page resources are excluded.', 'DataChannel file payload and 8-byte frame headers are separate; sender and receiver are not added as unique transferred bytes.', 'No TURN, public STUN, real Cognito, real S3, physical Android, OCR or poster lookup in this browser matrix.', 'Media latency ends at verified cache availability, not image paint. P50/P95 describe successful tasks; failure counts remain explicit.', 'Production diagnostic records are supplementary; native transport and all task samples are independently bounded and retained. Missing peer diagnostics make derived diagnostic totals null, not zero.'],
    cells: [], cleanup: {},
  };
  let child, childResult, exitPromise, serverRunId, failure, ownsBrowserTemp = false;
  let childLog = '';
  const temporary = path.join(output, 'browser-temp');
  const previousTemp = Object.fromEntries(['TEMP', 'TMP', 'TMPDIR'].map(key => [key, process.env[key]]));
  const browsers = new Set();
  const control = async (route, body) => {
    const response = await fetch(ORIGIN + '/__test/' + route, { method: 'POST', headers: { ...headers, 'Content-Type': 'application/json' }, body: JSON.stringify(body ?? {}), signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error(`Fixture control ${route}: HTTP ${response.status}`);
    return response.json();
  };
  const harness = (device, method, argument) => device.page.evaluate(async ({ method, argument }) => {
    const client = window.__keepsiteBenchmark;
    if (!client || typeof client[method] !== 'function') throw new Error('Missing public benchmark harness');
    return client[method](argument);
  }, { method, argument });
  const quiet = async devices => {
    await waitFor(async () => (await Promise.all(devices.map(async device => {
      const state = await harness(device, 'status');
      const observed = await device.page.evaluate(() => window.__keepsiteObserver.status());
      return !state.running && !state.inflight && !state.peer.activeTransfers && !observed.activePCs && !observed.activeChannels;
    }))).every(Boolean), 'all media tasks and native connections closed', 10000);
  };
  const phase = async (id, devices, action) => {
    for (const device of devices) await device.page.evaluate(id => window.__keepsiteObserver.start(id), id);
    await control('phase', { phaseId: id, action: 'start' });
    let result, actionError;
    try { result = await action(); await quiet(devices); }
    catch (error) { actionError = error; }
    const http = await control('phase', { phaseId: id, action: 'stop' });
    const observations = {};
    for (const device of devices) observations[device.label] = await device.page.evaluate(() => window.__keepsiteObserver.stop());
    const measured = { id, result, http, observations, ...(actionError ? { error: String(actionError.message) } : {}) };
    if (http.overflow || http.incomplete) measured.error = 'HTTP phase did not complete';
    return measured;
  };
  const assertPhase = measured => { if (measured.error) throw new Error(`${measured.id}: ${measured.error}`); };
  try {
    report.sourceSha256Before = await hashes();
    const require = createRequire(path.join(ROOT, 'demo/package.json'));
    const { chromium } = require('@playwright/test');
    report.environment.playwright = require('@playwright/test/package.json').version;
    // Never adopt an already-running fixture on the well-known local port.
    try { await fetch(ORIGIN + '/api/health', { signal: AbortSignal.timeout(500) }); throw new Error('Fixture port 4178 already in use'); }
    catch (error) { if (error.message === 'Fixture port 4178 already in use' || !['ECONNREFUSED', 'UND_ERR_CONNECT_TIMEOUT'].includes(error.cause?.code) && error.name !== 'TimeoutError') throw error; }
    await mkdir(temporary);
    ownsBrowserTemp = true;
    // Playwright creates profiles in the runner's os.tmpdir(), before spawn.
    for (const key of Object.keys(previousTemp)) process.env[key] = temporary;
    child = spawn(args.python, ['-B', path.join(ROOT, 'demo/tests/benchmark-server.py')], {
      cwd: ROOT, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', KEEPSITE_P2P_RUN_TOKEN: token, KEEPSITE_M6_TICKET_COUNT: String(args.count), TEMP: temporary, TMP: temporary, TMPDIR: temporary }, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
    });
    exitPromise = new Promise(resolve => { child.once('exit', (code, signal) => { childResult = { code, signal }; resolve(childResult); }); child.once('error', error => { childResult = { error: error.message }; resolve(childResult); }); });
    for (const stream of [child.stdout, child.stderr]) stream.on('data', chunk => { if (childLog.length < 128000) childLog += chunk.toString().slice(0, 128000 - childLog.length); });
    await waitFor(async () => {
      if (childResult) throw new Error(`Owned fixture exited before ready (${childResult.error ?? childResult.code})`);
      if (!await exists(ownership)) return false;
      const owned = JSON.parse(await readFile(ownership, 'utf8'));
      if (!/^m6-browser-[a-z0-9_]{8}$/.test(owned.runId)) throw new Error('Invalid fixture ownership manifest');
      serverRunId = owned.runId;
      try { return (await fetch(ORIGIN + '/api/health', { signal: AbortSignal.timeout(1000) })).ok; } catch { return false; }
    }, 'owned fixture startup', 60000);

    async function endpoint(label) {
      const browser = await chromium.launch({ channel: 'chromium', env: { ...process.env, TEMP: temporary, TMP: temporary, TMPDIR: temporary } });
      browsers.add(browser); report.environment.chromium = browser.version();
      const context = await browser.newContext({ baseURL: ORIGIN, viewport: { width: 1440, height: 1000 } });
      await context.addInitScript(installObserver);
      const response = await context.request.post('/__test/session', { headers, data: { group: 'benchmark', who: 'alice', label } });
      if (!response.ok()) throw new Error(`Fixture session: HTTP ${response.status()}`);
      const session = await response.json(), manifestResponse = await context.request.get('/__test/manifest', { headers });
      if (!manifestResponse.ok()) throw new Error('Fixture manifest unavailable');
      const manifest = await manifestResponse.json();
      validateManifest(manifest, args.count, session.userId);
      const page = await context.newPage(); await page.goto('/');
      await page.waitForFunction(() => Boolean(window.__keepsiteBenchmark));
      return { browser, context, page, label, manifest };
    }
    for (const mode of report.method.cases) for (const concurrency of report.method.taskConcurrencies) {
      const id = `${mode}-${concurrency}`, cell = { id, mode, concurrency, phases: [], passed: false };
      report.cells.push(cell); console.log(`Running ${id}`);
      const source = await endpoint('source'), target = await endpoint('target'), devices = [source, target];
      try {
        if (JSON.stringify(source.manifest) !== JSON.stringify(target.manifest)) throw new Error('Endpoints do not share the same dataset');
        report.dataset ??= source.manifest;
        const selected = report.method.indices.map(index => source.manifest.items[index].media), assetIds = selected.map(item => item.assetId);
        report.workset ??= selected;
        const bootstrap = await phase(id + '-bootstrap', devices, async () => {
          const result = await Promise.all(devices.map(device => harness(device, 'boot', { peerWanted: false })));
          if (result.some(item => item.ticketCount !== args.count)) throw new Error('Incomplete metadata bootstrap');
          return result;
        });
        cell.phases.push(bootstrap); assertPhase(bootstrap);
        const prewarm = await phase(id + '-source-warm', devices, () => harness(source, 'run', { assetIds, concurrency }));
        cell.phases.push(prewarm); assertPhase(prewarm);
        cell.sourceVerification = await harness(source, 'verify', { assetIds });
        if (!cell.sourceVerification.ok || prewarm.result.tasks.some(task => task.status !== 'cached' || !['server', 's3'].includes(task.source))) throw new Error('Source HTTP prewarm failed');
        const setup = await phase(id + '-peer-setup', devices, async () => {
          if (mode !== 'http-cold') {
            await Promise.all(devices.map(device => harness(device, 'setPeerWanted', true)));
            await waitFor(async () => {
              const a = await harness(source, 'status'), b = await harness(target, 'status');
              return a.peer.enabled && b.peer.enabled && a.peer.inventoryCount === args.sample;
            }, 'source inventory and both peer registrations');
          }
        });
        cell.phases.push(setup); assertPhase(setup);
        if (mode === 'cache-warm') {
          const prime = await phase(id + '-target-prime', devices, () => harness(target, 'run', { assetIds, concurrency }));
          cell.phases.push(prime); assertPhase(prime);
          cell.primeVerification = await harness(target, 'verify', { assetIds });
          if (!cell.primeVerification.ok || prime.result.tasks.some(task => task.status !== 'cached')) throw new Error('Target warmup failed');
          await harness(target, 'dispose'); await quiet(devices);
          await target.page.reload(); await target.page.waitForFunction(() => Boolean(window.__keepsiteBenchmark));
          const reboot = await phase(id + '-reload', devices, async () => {
            const boot = await harness(target, 'boot', { peerWanted: true });
            await waitFor(async () => { const state = await harness(target, 'status'); return state.peer.enabled && state.peer.inventoryCount === args.sample; }, 'reloaded peer cache inventory');
            return boot;
          });
          cell.phases.push(reboot); assertPhase(reboot);
          cell.reloadedVerification = await harness(target, 'verify', { assetIds });
          if (!cell.reloadedVerification.ok) throw new Error('Persistent cache did not survive reload');
        }
        const measured = await phase(id + '-measured', devices, () => harness(target, 'run', { assetIds, concurrency }));
        cell.phases.push(measured); assertPhase(measured);
        try { cell.verification = await harness(target, 'verify', { assetIds }); }
        catch { cell.verification = { ok: false, items: selected.map(item => ({ assetId: item.assetId, ok: false, sha256: null, size: null, mimeType: null, errorCode: 'verification_unavailable' })) }; }
        cell.summary = summarizeRun(measured.result, selected, cell.verification, measured.http, measured.observations, { warm: mode === 'cache-warm', httpOnly: mode === 'http-cold' });
        if (mode === 'peer-cold' && cell.summary.p2pCompletionShare === 0) throw new Error('P2P cell completed no peer transfer');
        cell.passed = cell.summary.failedTasks === 0 && cell.summary.httpFailures === 0 && cell.summary.cacheVerificationPassed && cell.summary.validationErrors.length === 0;
        if (!cell.passed) throw new Error(`${id}: failed task or HTTP sample`);
      } finally {
        await Promise.allSettled(devices.map(device => harness(device, 'dispose')));
        const results = await Promise.allSettled(devices.map(device => device.browser.close()));
        for (let index = 0; index < devices.length; index++) if (results[index].status === 'fulfilled') browsers.delete(devices[index].browser);
      }
    }
    report.passed = report.cells.length === 9 && report.cells.every(cell => cell.passed);
  } catch (error) { failure = error; report.error = String(error.message); }
  finally {
    for (const browser of browsers) { try { await browser.close(); } catch (error) { report.cleanup.browserError = String(error.message); } }
    if (child) {
      let shutdownError;
      try {
        if (serverRunId && child.exitCode === null) {
          const stopped = await control('shutdown');
          if (stopped.runId !== serverRunId) throw new Error('Shutdown ownership mismatch');
        }
      } catch (error) { shutdownError = error; report.cleanup.shutdownError = String(error.message); failure ??= error; }
      try {
        let exit;
        try { exit = await boundedExit(exitPromise, 12000); }
        catch (error) {
          // Only this exact ChildProcess handle may be terminated on a failed
          // graceful shutdown. Preserve any uncleaned fixture data as evidence.
          report.cleanup.forcedOwnedChildTermination = true; failure ??= error;
          if (child.pid && child.exitCode === null) child.kill('SIGTERM');
          exit = await boundedExit(exitPromise, 5000);
        }
        report.cleanup.serverExit = exit;
        if (exit.error || exit.code !== 0 || shutdownError) throw new Error('Fixture process did not complete a verified graceful shutdown');
        if (await exists(ownership) || serverRunId && await exists(path.join(ROOT, '.cache', serverRunId))) throw new Error('Owned fixture temporary files remain');
        report.cleanup.fixtureRemoved = true;
      } catch (error) { report.cleanup.serverError = String(error.message); failure ??= error; }
    }
    try {
      if (ownsBrowserTemp) { report.cleanup.browserTemporaryEntries = await readdir(temporary); await rmdir(temporary); report.cleanup.browserTemporaryRemoved = true; }
    } catch (error) { report.cleanup.temporaryError = String(error.message); failure ??= error; }
    for (const [key, value] of Object.entries(previousTemp)) { if (value === undefined) delete process.env[key]; else process.env[key] = value; }
    try { report.sourceSha256After = await hashes(); report.sourceUnchanged = JSON.stringify(report.sourceSha256Before) === JSON.stringify(report.sourceSha256After); if (!report.sourceUnchanged) throw new Error('Source or benchmark bundle changed during run'); }
    catch (error) { failure ??= error; report.hashError = String(error.message); }
    report.passed &&= !failure && !report.cleanup.browserError;
    report.finishedAt = new Date().toISOString();
    await writeFile(path.join(output, 'report.json'), JSON.stringify(report, null, 2) + '\n', { flag: 'wx' });
    await writeFile(path.join(output, 'fixture.log'), childLog, { flag: 'wx' });
  }
  console.log(JSON.stringify({ passed: report.passed, cells: report.cells.map(({ id, summary, passed }) => ({ id, passed, summary })), report: path.join(output, 'report.json') }, null, 2));
  if (!report.passed) throw failure ?? new Error('Browser benchmark did not pass');
}

export async function main(argv = process.argv.slice(2)) {
  const args = parseArguments(argv), output = await checkedNewOutput(args.output);
  const plan = { execute: args.execute, output, fixtureOrigin: ORIGIN, datasetCount: args.count, selectedIndices: sampleIndices(args.count, args.sample), matrix: ['http-cold', 'peer-cold', 'cache-warm'].flatMap(mode => [1, 2, 4].map(concurrency => ({ mode, concurrency, tasks: args.sample }))), prerequisites: ['npm --prefix demo run build:benchmark', 'existing Playwright Chromium and Python development dependencies'], scope: 'Synthetic local data only; owns new output, temporary profiles, loopback fixture and two independent browsers per cell; normal closure; no cloud access.' };
  console.log(JSON.stringify(plan, null, 2));
  if (!args.execute) return;
  await mkdir(path.dirname(output), { recursive: true });
  await mkdir(output);
  if ((await lstat(output)).isSymbolicLink() || path.resolve(realpathSync(output)).toLowerCase() !== output.toLowerCase()) throw new Error('Output ownership changed');
  await execute(args, output);
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) main().catch(error => { console.error(error.message); process.exitCode = 1; });

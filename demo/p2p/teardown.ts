import { access, readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Windows may force-terminate Playwright webServer processes. Ask this explicit
// fixture to close itself first so its owned data directory is removed normally.
export default async function teardown() {
  const token = process.env.MEMOVERSE_P2P_RUN_TOKEN;
  if (!token || !/^[a-f0-9]{32}$/.test(token)) throw new Error('Missing P2P fixture ownership key');
  const cache = fileURLToPath(new URL('../../.cache/', import.meta.url));
  const manifest = path.join(cache, `p2p-run-${token}.json`);
  let owned: { runId: string } | undefined;
  try { owned = JSON.parse(await readFile(manifest, 'utf8')); }
  catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  if (owned && (typeof owned.runId !== 'string' || !/^p2p-browser-[a-z0-9_]{8}$/.test(owned.runId))) throw new Error('Invalid owned fixture directory');
  const directory = owned && path.join(cache, owned.runId);
  const removed = async (target: string) => {
    try { await access(target); return false; }
    catch (error) { if ((error as NodeJS.ErrnoException).code === 'ENOENT') return true; throw error; }
  };
  let response: Response;
  try {
    response = await fetch('http://127.0.0.1:4177/__test/shutdown', {
      method: 'POST', headers: { 'X-MemoVerse-Fixture': 'p2p-browser-fixture', 'X-MemoVerse-Fixture-Run': token }, signal: AbortSignal.timeout(3000),
    });
  } catch (error) {
    if ((error as { cause?: { code?: string } }).cause?.code === 'ECONNREFUSED'
      && (!directory || await removed(directory)) && await removed(manifest)) return;
    throw error; // A timed-out or broken shutdown is not evidence of cleanup.
  }
  if (!response.ok) throw new Error('Owned P2P fixture rejected graceful shutdown');
  const { runId } = await response.json();
  if (!directory || runId !== owned?.runId) throw new Error('Fixture shutdown ownership mismatch');
  const deadline = Date.now() + 9000;
  while (Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 100));
    if (await removed(directory) && await removed(manifest)) return;
  }
  throw new Error('Owned P2P fixture failed to stop within its shutdown deadline');
}

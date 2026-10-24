import { defineConfig } from '@playwright/test';
import { randomUUID } from 'node:crypto';

const python = process.env.KEEPSITE_TEST_PYTHON ?? (process.platform === 'win32' ? '../backend/.venv/Scripts/python.exe' : 'python');
// Workers inherit the same opaque ownership key; another run cannot shut down
// this fixture merely because it happens to use the same loopback port.
process.env.KEEPSITE_P2P_RUN_TOKEN ??= randomUUID().replaceAll('-', '');

export default defineConfig({
  testDir: './p2p',
  outputDir: './test-results/p2p',
  timeout: 45_000,
  workers: 1,
  globalTeardown: './p2p/teardown.ts',
  use: { baseURL: 'http://127.0.0.1:4177', viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure' },
  webServer: {
    command: `"${python}" -B tests/p2p-server.py`,
    url: 'http://127.0.0.1:4177/api/health',
    reuseExistingServer: false,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 10_000 },
  },
});

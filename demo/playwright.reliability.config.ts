import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './reliability',
  timeout: 30_000,
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:4176', channel: 'chromium', viewport: { width: 1440, height: 1000 }, trace: 'retain-on-failure' },
  webServer: {
    command: 'node tests/reliability-server.mjs',
    url: 'http://127.0.0.1:4176',
    reuseExistingServer: false,
  },
});

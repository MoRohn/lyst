import { defineConfig, devices } from '@playwright/test';

// Run with `npm test` (builds the variants first). See scripts/build-variants.mjs.
export default defineConfig({
  testDir: 'tests',
  timeout: 30_000,
  fullyParallel: true,
  reporter: [['list']],
  use: { baseURL: 'http://localhost:4329', trace: 'retain-on-failure' },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } }, testIgnore: /mobile\.spec\.ts/ },
    { name: 'mobile', use: { ...devices['Pixel 7'] }, testMatch: /mobile\.spec\.ts/ },
  ],
  webServer: [
    {
      command:
        'rm -f ../.waitlist-test/waitlist.duckdb* && uv run lyst-waitlist serve --port 8799',
      cwd: 'waitlist',
      url: 'http://localhost:8799/healthz',
      reuseExistingServer: false,
      env: {
        LYST_WAITLIST_ENV: 'development',
        LYST_WAITLIST_DB: '../.waitlist-test/waitlist.duckdb',
        LYST_WAITLIST_ALLOWED_ORIGINS: 'http://localhost:4329',
        LYST_WAITLIST_RATE_LIMIT: '200/600',
      },
    },
    { command: 'node scripts/serve.mjs dist-test 4329', url: 'http://localhost:4329/', reuseExistingServer: false },
    { command: 'node scripts/serve.mjs dist-closed 4330', url: 'http://localhost:4330/', reuseExistingServer: false },
    { command: 'node scripts/serve.mjs dist-sub 4331 /lyst', url: 'http://localhost:4331/lyst/', reuseExistingServer: false },
  ],
});

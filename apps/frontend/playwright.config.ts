import { defineConfig, devices } from '@playwright/test';

const port = Number(process.env.CLOUD_AGENT_E2E_PORT);
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error(
    'Run browser regressions with npm run test:browser (allocates an isolated port).',
  );
}
const baseURL = `http://127.0.0.1:${port}`;

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.spec.ts',
  fullyParallel: false,
  workers: 1,
  // One retry in CI, for the host rather than the app: the trusted runners
  // share a host whose other jobs create and remove Docker networks, and
  // Chromium aborts in-flight requests when the host's interfaces change
  // (`net::ERR_NETWORK_CHANGED`). On 2026-11-04 that failed a page chunk and a
  // test that passes in 1.3 s timed out on the error page. Every spec takes
  // `test` from e2e/network-change.ts (lint enforces it), whose retry fails at
  // once unless the failed attempt saw that abort, so an ordinary failure is
  // never retried. Playwright reports a test that needed the retry as flaky.
  retries: process.env.CI ? 1 : 0,
  forbidOnly: Boolean(process.env.CI),
  timeout: 30_000,
  expect: { timeout: 10_000 },
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    serviceWorkers: 'block',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: 'node e2e/server.mjs',
    url: `${baseURL}/__fixture/health`,
    reuseExistingServer: false,
    timeout: 240_000,
    gracefulShutdown: { signal: 'SIGTERM', timeout: 5_000 },
  },
});

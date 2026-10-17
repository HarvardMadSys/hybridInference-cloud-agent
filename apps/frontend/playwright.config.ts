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
  retries: 0,
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

import { expect, test } from './network-change';

import { chatOnly, connectionView } from './modelConnectionFixtures';

test.beforeEach(async ({ request }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
});

test.afterEach(async ({ request }) => {
  expect((await (await request.get('/__fixture/state')).json()).unknown).toEqual([]);
});

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
]) {
  test(`personal connection setup and write-only edit fit ${viewport.width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport);
    await page.addInitScript(() => {
      localStorage.setItem('agents.locale', 'en');
      localStorage.setItem('agents.theme', 'light');
    });
    let saved: ReturnType<typeof connectionView> | null = null;
    let submitted: Record<string, unknown> | null = null;
    await page.route('**/api/v1/agent/model-connections', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({
          json: { enabled: true, configured: true, connections: saved ? [saved] : [] },
        });
        return;
      }
      submitted = route.request().postDataJSON();
      saved = connectionView({
        id: 'mconn_fixture',
        name: String(submitted!.name),
        base_url: String(submitted!.base_url),
        model_status: (submitted!.models as string[]).map(chatOnly),
      });
      await route.fulfill({ status: 201, json: saved });
    });
    await page.route('**/api/v1/agent/model-connections/mconn_fixture', async (route) => {
      submitted = route.request().postDataJSON();
      saved = { ...saved!, ...submitted, updated_at: '2026-10-28' };
      await route.fulfill({ json: saved });
    });
    await page.goto('/settings/models');
    await page.getByRole('button', { name: 'Add connection', exact: true }).click();
    await page.getByLabel('Connection name', { exact: true }).fill('Personal inference');
    await page.getByLabel('Base URL', { exact: true }).fill('https://api.example.com/custom/v1');
    await page.getByLabel('API key', { exact: true }).fill('browser-test-not-a-real-key');
    await page.getByLabel('Model IDs', { exact: true }).fill('fixture-model');
    await page.getByLabel('Model IDs', { exact: true }).press('Enter');
    await expect(page.getByLabel('API key', { exact: true })).toHaveAttribute('type', 'password');
    const section = page.getByRole('region', { name: 'Personal model connections', exact: true });
    await section.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      viewport.width,
    );
    await section.screenshot({
      path: testInfo.outputPath('personal-connection-form.png'),
      animations: 'disabled',
    });
    await section.getByRole('button', { name: 'Verify and save', exact: true }).click();
    await expect(page.getByRole('article', { name: 'Personal inference' })).toBeVisible();
    expect(submitted).toEqual({
      name: 'Personal inference',
      base_url: 'https://api.example.com/custom/v1',
      api_key: 'browser-test-not-a-real-key',
      models: ['fixture-model'],
    });
    await page.getByRole('button', { name: 'Edit', exact: true }).click();
    await expect(page.getByLabel('API key', { exact: true })).toHaveValue('');
    await page.getByLabel('Connection name', { exact: true }).fill('Renamed connection');
    await section.getByRole('button', { name: 'Save', exact: true }).click();
    await expect(page.getByRole('article', { name: 'Renamed connection' })).toBeVisible();
    // A rename sends the name alone: the key, processes and verification stay.
    expect(submitted).toEqual({ name: 'Renamed connection' });
    await section.screenshot({
      path: testInfo.outputPath('personal-connection-saved.png'),
      animations: 'disabled',
    });
  });
}

test('personal connections fold beneath available models and keep a half-filled form', async ({
  page,
}) => {
  await page.addInitScript(() => {
    localStorage.setItem('agents.locale', 'en');
    localStorage.setItem('agents.theme', 'light');
  });
  await page.route('**/api/v1/agent/config', async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      json: {
        ...(await response.json()),
        model_connections_enabled: true,
        provided_sources: [
          {
            id: 'oconn_fixture',
            name: 'Fixture models',
            is_default: true,
            default_model: 'fixture-provided',
            models: ['fixture-provided'],
            model_runtimes: { 'fixture-provided': ['codex'] },
            allowances: { 'fixture-provided': { daily_requests: 100, used_today: 1 } },
            resets_at: '2026-10-29T00:00:00Z',
          },
        ],
      },
    });
  });
  let reads = 0;
  await page.route('**/api/v1/agent/model-connections', async (route) => {
    reads += 1;
    await route.fulfill({ json: { enabled: true, configured: true, connections: [] } });
  });
  await page.goto('/settings/models');

  await expect(page.getByRole('region', { name: 'Available models', exact: true })).toContainText(
    'fixture-provided',
  );
  const section = page.getByRole('region', { name: 'Personal model connections', exact: true });
  const heading = section.getByRole('button', { name: 'Personal model connections', exact: true });
  const key = page.getByLabel('API key', { exact: true });
  await expect(heading).toHaveAttribute('aria-expanded', 'false');
  // Never opened, the section has asked the server for nothing.
  expect(reads).toBe(0);

  await heading.click();
  await section.getByRole('button', { name: 'Add connection', exact: true }).click();
  await key.fill('browser-test-not-a-real-key');

  await heading.click();
  await expect(heading).toHaveAttribute('aria-expanded', 'false');
  await expect(key).toBeHidden();
  await heading.click();
  await expect(key).toBeVisible();
  await expect(key).toHaveValue('browser-test-not-a-real-key');
  expect(reads).toBe(1);

  // Folded again, an address that comes to name the section opens it.
  await heading.click();
  await expect(key).toBeHidden();
  await page.evaluate(() => {
    window.location.hash = '#settings-connections';
  });
  await expect(heading).toHaveAttribute('aria-expanded', 'true');
  await expect(key).toHaveValue('browser-test-not-a-real-key');
});

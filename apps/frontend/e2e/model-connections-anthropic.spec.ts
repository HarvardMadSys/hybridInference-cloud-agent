import { expect, test } from '@playwright/test';

// An Anthropic Messages connection in the real Next app: the third API in
// Settings, and Claude Code offered its models -- and only Claude Code.

const connectionId = `mconn_${'b'.repeat(32)}`;
const claudeLabel = 'claude-fixture · Claude gateway';

test.beforeEach(async ({ page, request }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
  await page.addInitScript(() => {
    localStorage.setItem('agents.locale', 'en');
    localStorage.setItem('agents.theme', 'light');
  });
  await page.route('**/api/v1/agent/model-preference', (route) =>
    route.fulfill({
      json: {
        default_model: null,
        last_model: null,
        effective_model: null,
        deployment_default: null,
        models: ['fixture-model'],
      },
    }),
  );
});

test.afterEach(async ({ request }) => {
  expect((await (await request.get('/__fixture/state')).json()).unknown).toEqual([]);
});

for (const viewport of [
  { width: 1440, height: 1000 },
  { width: 390, height: 844 },
]) {
  test(`an Anthropic Messages connection is saved and runs Claude Code at ${viewport.width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport);
    let submitted: Record<string, unknown> | null = null;
    await page.route('**/api/v1/agent/model-connections', async (route) => {
      if (route.request().method() === 'GET') {
        await route.fulfill({ json: { enabled: true, configured: true, connections: [] } });
        return;
      }
      submitted = route.request().postDataJSON();
      const { api_key: _key, ...fields } = submitted!;
      await route.fulfill({
        status: 201,
        json: {
          ...fields,
          id: connectionId,
          enabled: true,
          has_api_key: true,
          runtimes: ['claude-code'],
          last_check: null,
          created_at: '2026-10-28',
          updated_at: '2026-10-28',
        },
      });
    });
    await page.goto('/settings/my-settings');
    await page.getByRole('button', { name: 'Add connection', exact: true }).click();
    const anthropic = page.getByRole('checkbox', { name: 'Anthropic Messages · Claude Code' });
    await expect(anthropic).toBeVisible();
    await expect(page.getByText(/cannot use these connections/)).toHaveCount(0);
    await page.getByLabel('Connection name', { exact: true }).fill('Claude gateway');
    await page.getByLabel('Base URL', { exact: true }).fill('https://gateway.example/anthropic/v1');
    await page.getByLabel('API key', { exact: true }).fill('browser-test-not-a-real-key');
    await page.getByLabel('Model IDs', { exact: true }).fill('claude-fixture');
    await anthropic.check();
    await page.getByRole('checkbox', { name: 'Chat Completions · OpenCode, Kilo, pi' }).uncheck();
    await anthropic.scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      viewport.width,
    );
    await page.screenshot({
      path: testInfo.outputPath('anthropic-protocols.png'),
      animations: 'disabled',
    });
    await page.getByRole('button', { name: 'Save connection', exact: true }).click();
    await expect(page.getByRole('article', { name: 'Claude gateway' })).toBeVisible();
    expect(submitted).toMatchObject({
      protocols: ['anthropic_messages'],
      base_url: 'https://gateway.example/anthropic/v1',
      models: ['claude-fixture'],
    });

    const created: Record<string, unknown>[] = [];
    await page.route('**/api/v1/agent/config*', (route) =>
      route.fulfill({
        json: {
          repos: ['fixture/repository'],
          runtimes: ['claude-code', 'codex'],
          models: ['fixture-model'],
          default_model: null,
          github_connected: true,
          github_install_url: null,
          model_connections_enabled: true,
          model_connections: [
            {
              id: connectionId,
              name: 'Claude gateway',
              models: ['claude-fixture'],
              runtimes: ['claude-code'],
            },
          ],
        },
      }),
    );
    await page.route('**/api/v1/agent/jobs', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      created.push(route.request().postDataJSON());
      // Captured, not run: no upstream provider is fabricated here.
      await route.fulfill({
        status: 422,
        json: { detail: 'Browser fixture captured the choice.' },
      });
    });
    await page.goto('/?repo=fixture%2Frepository');
    const agent = page.getByRole('button', { name: 'Agent', exact: true });
    const model = page.getByRole('button', { name: 'Model', exact: true });
    await expect(agent).toHaveAttribute('value', 'claude-code');
    await model.click();
    await expect(page.getByRole('option', { name: claudeLabel, exact: true })).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath('claude-model-choice.png'),
      animations: 'disabled',
    });
    await page.getByRole('option', { name: claudeLabel, exact: true }).click();
    await page.locator('#new-task-prompt').fill('Run Claude Code on my key');
    await page.locator('#new-task-prompt').press('Enter');
    await expect.poll(() => created.length).toBe(1);
    expect(created[0]).toMatchObject({
      runtime: 'claude-code',
      model: 'claude-fixture',
      model_connection_id: connectionId,
    });
    expect(created[0]).not.toHaveProperty('reasoning_effort');

    // Codex does not speak Messages: the pick is kept, marked, and holds Run.
    await agent.click();
    await page.getByRole('option', { name: 'codex', exact: true }).click();
    await expect(model).toContainText('unavailable');
    await expect(page.getByRole('button', { name: 'Run', exact: true })).toBeDisabled();
  });
}

import { expect, test } from '@playwright/test';

// A Chat Completions-only connection in the real Next app: Settings names the
// agents that reach it through translation and offers translated tests, the
// composer offers it to Codex and Claude Code marked as translated, and a
// connection that speaks an agent's own API stays native for that agent.

const chatId = `mconn_${'c'.repeat(32)}`;
const bothId = `mconn_${'d'.repeat(32)}`;
const chatOnly = {
  id: chatId,
  name: 'Chat provider',
  base_url: 'https://chat.example/v1',
  protocols: ['chat_completions'],
  models: ['chat-model'],
  enabled: true,
  has_api_key: true,
  runtimes: ['claude-code', 'codex', 'kilo', 'opencode', 'pi'],
  translated_runtimes: ['claude-code', 'codex'],
  last_check: null,
  created_at: '2026-10-28',
  updated_at: '2026-10-28',
};

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
  test(`a Chat-only connection runs Codex and Claude Code through translation at ${viewport.width}px`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport);
    const checked: Record<string, unknown>[] = [];
    await page.route('**/api/v1/agent/model-connections', (route) =>
      route.fulfill({ json: { enabled: true, configured: true, connections: [chatOnly] } }),
    );
    await page.route(`**/api/v1/agent/model-connections/${chatId}/check`, (route) => {
      checked.push(route.request().postDataJSON());
      return route.fulfill({
        json: {
          status: 'supported',
          category: 'supported',
          message: 'Streaming and a tool call round trip both worked.',
          protocol: 'anthropic_messages',
          upstream_protocol: 'chat_completions',
          mode: 'translated',
          model: 'chat-model',
          streaming: true,
          tool_call: true,
          final_answer: true,
          http_status: 200,
          checked_at: '2026-10-28T08:00:00Z',
          recorded: true,
        },
      });
    });
    await page.goto('/settings/my-settings');
    const card = page.getByRole('article', { name: 'Chat provider' });
    await expect(card).toContainText('codex (via Chat Completions)');
    await expect(card).toContainText('claude-code (via Chat Completions)');
    // The stored declaration is the upstream's: only Chat Completions.
    await expect(card).not.toContainText('Responses ·');
    await card.locator('summary').filter({ hasText: 'Test model' }).click();
    const api = card.getByLabel('API to test', { exact: true });
    await expect(api.locator('option')).toHaveText([
      'Chat Completions',
      'Responses via Chat Completions',
      'Anthropic Messages via Chat Completions',
    ]);
    expect(checked).toEqual([]);
    await api.selectOption('anthropic_messages');
    await card.getByRole('button', { name: 'Test model', exact: true }).click();
    await expect(card.getByRole('status')).toContainText(
      'chat-model · Anthropic Messages via Chat Completions.',
    );
    expect(checked).toEqual([{ model: 'chat-model', protocol: 'anthropic_messages' }]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      viewport.width,
    );
    await page.screenshot({ path: testInfo.outputPath('translated-check.png') });

    const created: Record<string, unknown>[] = [];
    await page.route('**/api/v1/agent/config*', (route) =>
      route.fulfill({
        json: {
          repos: ['fixture/repository'],
          runtimes: ['claude-code', 'codex', 'pi'],
          models: ['fixture-model'],
          default_model: null,
          github_connected: true,
          github_install_url: null,
          model_connections_enabled: true,
          model_connections: [
            {
              id: chatId,
              name: 'Chat provider',
              models: ['chat-model'],
              runtimes: chatOnly.runtimes,
              translated_runtimes: ['claude-code', 'codex'],
            },
            {
              id: bothId,
              name: 'Both APIs',
              models: ['both-model'],
              runtimes: ['claude-code', 'codex', 'kilo', 'opencode', 'pi'],
              translated_runtimes: ['claude-code'],
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
    await agent.click();
    await page.getByRole('option', { name: 'codex', exact: true }).click();
    await model.click();
    await expect(
      page.getByRole('option', { name: 'chat-model · Chat provider · via Chat Completions' }),
    ).toBeVisible();
    // Native first: this connection offers Responses, so Codex is not translated.
    await expect(
      page.getByRole('option', { name: 'both-model · Both APIs', exact: true }),
    ).toBeVisible();
    await page.screenshot({ path: testInfo.outputPath('codex-sources.png') });
    await page
      .getByRole('option', { name: 'chat-model · Chat provider · via Chat Completions' })
      .click();
    await expect(model).toContainText('via Chat Completions');
    await page.locator('#new-task-prompt').fill('Run Codex on my Chat provider');
    await page.locator('#new-task-prompt').press('Enter');
    await expect.poll(() => created.length).toBe(1);
    expect(created[0]).toMatchObject({
      runtime: 'codex',
      model: 'chat-model',
      model_connection_id: chatId,
    });

    await agent.click();
    await page.getByRole('option', { name: 'claude-code', exact: true }).click();
    await model.click();
    await expect(
      page.getByRole('option', { name: 'both-model · Both APIs · via Chat Completions' }),
    ).toBeVisible();
    await page
      .getByRole('option', { name: 'chat-model · Chat provider · via Chat Completions' })
      .click();
    await page.locator('#new-task-prompt').fill('Run Claude Code on my Chat provider');
    await page.locator('#new-task-prompt').press('Enter');
    await expect.poll(() => created.length).toBe(2);
    expect(created[1]).toMatchObject({
      runtime: 'claude-code',
      model: 'chat-model',
      model_connection_id: chatId,
    });
  });
}

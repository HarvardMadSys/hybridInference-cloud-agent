import { expect, test } from '@playwright/test';

const connectionId = `mconn_${'b'.repeat(32)}`;
const connectionName = 'My Claude provider';
const model = 'fixture-claude';

test.beforeEach(async ({ request }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
});

test.afterEach(async ({ request }) => {
  expect((await (await request.get('/__fixture/state')).json()).unknown).toEqual([]);
});

for (const locale of ['en', 'zh-CN'] as const) {
  test(`Anthropic connection setup, test and Claude task preserve source (${locale})`, async ({
    page,
  }, testInfo) => {
    const zh = locale === 'zh-CN';
    const width = zh ? 390 : 1440;
    await page.setViewportSize({ width, height: zh ? 844 : 1200 });
    await page.addInitScript((value) => {
      localStorage.setItem('agents.locale', value);
      localStorage.setItem('agents.theme', 'light');
    }, locale);
    await page.route('**/api/v1/agent/model-preference', (route) =>
      route.fulfill({
        json: { default_model: null, last_model: null, effective_model: null, models: [] },
      }),
    );
    let saved: Record<string, unknown> | null = null;
    let checked: Record<string, unknown> | null = null;
    const jobs: Record<string, unknown>[] = [];
    await page.route('**/api/v1/agent/model-connections', async (route) => {
      if (route.request().method() === 'GET') {
        return route.fulfill({
          json: { enabled: true, configured: true, connections: saved ? [saved] : [] },
        });
      }
      const { api_key: key, ...input } = route.request().postDataJSON();
      expect(key).toBe('fixture-key-not-a-credential');
      expect(input.protocols).toEqual(['anthropic_messages']);
      saved = {
        ...input,
        id: connectionId,
        enabled: true,
        has_api_key: true,
        runtimes: ['claude-code'],
        last_check: null,
        created_at: '2026-10-28',
        updated_at: '2026-10-28',
      };
      return route.fulfill({ status: 201, json: saved });
    });
    await page.route(`**/api/v1/agent/model-connections/${connectionId}/check`, (route) => {
      checked = route.request().postDataJSON();
      return route.fulfill({
        json: {
          ...checked,
          status: 'supported',
          category: 'supported',
          message: 'Streaming and a tool call round trip both worked.',
          streaming: true,
          tool_call: true,
          final_answer: true,
          http_status: 200,
          recorded: true,
          checked_at: '2026-10-28T03:00:00Z',
        },
      });
    });
    await page.goto('/settings/my-settings');
    await page
      .getByRole('button', { name: zh ? '添加连接' : 'Add connection', exact: true })
      .click();
    await page
      .getByLabel(zh ? '连接名称' : 'Connection name', { exact: true })
      .fill(connectionName);
    await page
      .getByLabel('Base URL', { exact: true })
      .fill('https://provider.example/anthropic/v1');
    await page.getByLabel('API key', { exact: true }).fill('fixture-key-not-a-credential');
    await page.getByLabel(zh ? '模型 ID' : 'Model IDs', { exact: true }).fill(model);
    await page.getByRole('checkbox', { name: /^Chat Completions/ }).uncheck();
    await page.getByRole('checkbox', { name: /^Anthropic Messages/ }).check();
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      width,
    );
    const region = page.getByRole('region', {
      name: zh ? '个人模型连接' : 'Personal model connections',
      exact: true,
    });
    await region.screenshot({ path: testInfo.outputPath('anthropic-form.png') });
    await page
      .getByRole('button', { name: zh ? '保存连接' : 'Save connection', exact: true })
      .click();
    const card = page.getByRole('article', { name: connectionName });
    await expect(card).toContainText('claude-code');
    await region.screenshot({ path: testInfo.outputPath('anthropic-saved.png') });
    await card
      .locator('summary')
      .filter({ hasText: zh ? '测试模型' : 'Test model' })
      .click();
    await expect(
      card.getByRole('combobox', { name: zh ? '测试 API' : 'API to test', exact: true }),
    ).toHaveText('Anthropic Messages');
    await card.getByRole('button', { name: zh ? '测试模型' : 'Test model', exact: true }).click();
    await expect(card.getByRole('status')).toContainText('Anthropic Messages');
    expect(checked).toEqual({ model, protocol: 'anthropic_messages' });

    await page.route('**/api/v1/agent/config*', (route) =>
      route.fulfill({
        json: {
          repos: ['fixture/repository'],
          runtimes: ['claude-code', 'codex'],
          models: ['fixture-model'],
          default_model: model,
          default_model_connection_id: connectionId,
          github_connected: true,
          github_install_url: null,
          model_connections_enabled: true,
          model_connections: [
            { id: connectionId, name: connectionName, models: [model], runtimes: ['claude-code'] },
          ],
        },
      }),
    );
    await page.route('**/api/v1/agent/jobs', async (route) => {
      if (route.request().method() !== 'POST') return route.continue();
      jobs.push(route.request().postDataJSON());
      return route.fulfill({
        status: 422,
        json: { detail: 'Fixture captured source; no execution.' },
      });
    });
    await page.goto('/?repo=fixture%2Frepository');
    await expect(page.getByRole('button', { name: 'Agent', exact: true })).toContainText(
      'claude-code',
    );
    await page.getByRole('button', { name: zh ? '模型' : 'Model', exact: true }).click();
    await page.screenshot({ path: testInfo.outputPath('claude-model-source.png') });
    await page.keyboard.press('Escape');
    await page.locator('#new-task-prompt').fill('Run using my Anthropic connection');
    await page.locator('#new-task-prompt').press('Enter');
    await expect.poll(() => jobs.length).toBe(1);
    expect(jobs[0]).toMatchObject({
      runtime: 'claude-code',
      model,
      model_connection_id: connectionId,
    });
    await page.getByRole('button', { name: 'Agent', exact: true }).click();
    await page.getByRole('option', { name: 'codex', exact: true }).click();
    await expect(
      page.getByRole('button', { name: zh ? '运行' : 'Run', exact: true }),
    ).toBeDisabled();
    await page.locator('#new-task-prompt').press('Enter');
    expect(jobs).toHaveLength(1);
  });
}

import { expect, test } from '@playwright/test';
import { chatOnly, connectionView } from './modelConnectionFixtures';

const id = `mconn_${'b'.repeat(32)}`;

function composerConnections(connection: ReturnType<typeof connectionView> | null) {
  if (!connection?.enabled) return [];
  return [
    {
      id,
      name: connection.name,
      models: connection.models,
      model_runtimes: { 'fixture-model': ['codex'] },
    },
  ];
}
const variants = [
  {
    locale: 'en',
    width: 1440,
    setup: 'Set up a model connection',
    add: 'Add connection',
    name: 'Connection name',
    models: 'Model IDs',
    save: 'Verify and save',
    disable: 'Disable',
    enable: 'Enable',
    remove: 'Delete',
    confirm: 'Confirm',
    run: 'Run',
    model: 'Model',
    disabled: 'Model connections are unavailable',
    unavailable: 'unavailable',
  },
  {
    locale: 'zh-CN',
    width: 390,
    setup: '设置模型连接',
    add: '添加连接',
    name: '连接名称',
    models: '模型 ID',
    save: '验证并保存',
    disable: '停用',
    enable: '启用',
    remove: '删除',
    confirm: '确认',
    run: '运行',
    model: '模型',
    disabled: '模型连接暂不可用',
    unavailable: '不可用',
  },
] as const;

test.beforeEach(async ({ context, request }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
  await context.route('**/api/v1/session/me', (route) =>
    route.fulfill({
      json: {
        id: 'browser-owner',
        email: 'owner@example.test',
        agent_access: { allowed: true, permissions: ['agent.use'] },
      },
    }),
  );
  await context.route('**/api/v1/auth/config', (route) =>
    route.fulfill({ json: { signup_enabled: true } }),
  );
  await context.route('**/api/v1/agent/environment-globals', (route) => {
    expect(route.request().method()).toBe('GET');
    return route.fulfill({
      json: {
        id: null,
        name: 'Global',
        repo: null,
        revision: 0,
        variables: [],
        setup_script: '',
        start_script: '',
        verify_script: '',
        actions: [],
      },
    });
  });
});

test.afterEach(async ({ request }) => {
  expect((await (await request.get('/__fixture/state')).json()).unknown).toEqual([]);
});

for (const text of variants) {
  test(`approved owner sets up a model and open composer follows save, disable, enable and delete (${text.locale})`, async ({
    page,
    context,
  }, info) => {
    await page.setViewportSize({ width: text.width, height: 900 });
    await context.addInitScript(
      (locale) => localStorage.setItem('agents.locale', locale),
      text.locale,
    );
    let saved: ReturnType<typeof connectionView> | null = null;
    const gatewayCalls: string[] = [];
    context.on('request', (request) => {
      if (/model-preference|prompt-rewrite/.test(request.url())) gatewayCalls.push(request.url());
    });
    await context.route('**/api/v1/agent/config*', (route) =>
      route.fulfill({
        json: {
          repos: ['fixture/repository'],
          runtimes: ['codex'],
          models: [],
          default_model: null,
          github_connected: true,
          github_install_url: null,
          gateway_inference: false,
          model_connection_required: !saved?.enabled,
          model_connections_enabled: true,
          model_connections: composerConnections(saved),
        },
      }),
    );
    await context.route('**/api/v1/agent/model-connections', async (route) => {
      if (route.request().method() === 'GET')
        return route.fulfill({
          json: { enabled: true, configured: true, connections: saved ? [saved] : [] },
        });
      expect(route.request().postDataJSON()).toEqual({
        name: 'My provider',
        base_url: 'https://provider.example/v1',
        api_key: 'fixture-key-not-a-credential',
        models: ['fixture-model'],
      });
      saved = connectionView({
        id,
        name: 'My provider',
        base_url: 'https://provider.example/v1',
        model_status: [chatOnly('fixture-model')],
      });
      return route.fulfill({ status: 201, json: saved });
    });
    await context.route(`**/api/v1/agent/model-connections/${id}`, (route) => {
      if (route.request().method() === 'DELETE') {
        saved = null;
        return route.fulfill({ status: 204 });
      }
      saved = {
        ...saved!,
        ...route.request().postDataJSON(),
        updated_at: new Date().toISOString(),
      };
      return route.fulfill({ json: saved });
    });
    await page.goto('/?repo=fixture%2Frepository');
    const setup = page.getByRole('link', { name: text.setup, exact: true });
    await expect(setup).toBeVisible();
    await expect(page.getByRole('button', { name: text.run, exact: true })).toBeDisabled();
    const prompt = page.locator('#new-task-prompt');
    await prompt.fill('Keep the draft while I connect my model');
    const settings = await context.newPage();
    await settings.setViewportSize({ width: text.width, height: 900 });
    await settings.goto((await setup.getAttribute('href'))!);
    await expect(settings.locator('#settings-model')).toHaveCount(0);
    await settings.getByRole('button', { name: text.add, exact: true }).click();
    await settings.getByLabel(text.name, { exact: true }).fill('My provider');
    await settings.getByLabel('Base URL', { exact: true }).fill('https://provider.example/v1');
    await settings.getByLabel('API key', { exact: true }).fill('fixture-key-not-a-credential');
    await settings.getByLabel(text.models, { exact: true }).fill('fixture-model');
    await settings.getByRole('button', { name: text.save, exact: true }).click();
    const card = settings.getByRole('article', { name: 'My provider', exact: true });
    await expect(card).toBeVisible();
    await expect(setup).toHaveCount(0);
    await expect(prompt).toHaveValue('Keep the draft while I connect my model');
    await page.getByRole('button', { name: text.model, exact: true }).click();
    await page.getByRole('option', { name: 'fixture-model · My provider', exact: true }).click();
    const run = page.getByRole('button', { name: text.run, exact: true });
    await expect(run).toBeEnabled();
    await card.getByRole('button', { name: text.disable, exact: true }).click();
    await card.getByRole('button', { name: text.confirm, exact: true }).click();
    await expect(run).toBeDisabled();
    await expect(page.getByRole('button', { name: text.model, exact: true })).toContainText(
      text.unavailable,
    );
    await card.getByRole('button', { name: text.enable, exact: true }).click();
    await expect(run).toBeEnabled();
    await card.getByRole('button', { name: text.remove, exact: true }).click();
    await card.getByRole('button', { name: text.confirm, exact: true }).click();
    await expect(run).toBeDisabled();
    await expect(setup).toBeVisible();
    expect(gatewayCalls).toEqual([]);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      text.width,
    );
    await page.screenshot({ path: info.outputPath('local-model-setup.png'), fullPage: true });
    await settings.close();
  });

  test(`explicitly disabled personal connections have no broken setup action (${text.locale})`, async ({
    page,
    context,
  }) => {
    await page.setViewportSize({ width: text.width, height: 900 });
    await context.addInitScript(
      (locale) => localStorage.setItem('agents.locale', locale),
      text.locale,
    );
    await context.route('**/api/v1/agent/config*', (route) =>
      route.fulfill({
        json: {
          repos: [],
          runtimes: ['codex'],
          models: [],
          default_model: null,
          github_connected: false,
          github_install_url: null,
          gateway_inference: false,
          model_connection_required: true,
          model_connections_enabled: false,
          model_connections: [],
        },
      }),
    );
    await page.goto('/');
    await expect(page.getByText(text.disabled, { exact: true })).toBeVisible();
    await expect(page.getByRole('link', { name: text.setup, exact: true })).toHaveCount(0);
    await expect(page.getByRole('button', { name: text.run, exact: true })).toBeDisabled();
    await expect(page.getByRole('button', { name: /Rewrite prompt|改写提示词/ })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
      text.width,
    );
  });
}

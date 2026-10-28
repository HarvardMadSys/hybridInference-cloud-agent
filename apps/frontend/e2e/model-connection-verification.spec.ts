import { expect, test } from '@playwright/test';

import { chatOnly, connectionView, messagesOnly, unverified } from './modelConnectionFixtures';

// Automatic API detection in the real Next app. The owner gives a name, a Base
// URL, a key and model IDs; "Save and verify" checks each model on request,
// and every model's own result decides which agents may run it. No protocol
// is asked for or named on the normal path; the diagnostics stay one click
// away. One model is Chat-only (Codex and Claude Code translated), the other
// Messages-only (Claude Code only), and a model added later is unverified.

const connectionId = `mconn_${'e'.repeat(32)}`;
const name = 'Mixed provider';
const baseUrl = 'https://provider.example/v1';
const PROTOCOL_NAMES = /Chat Completions|Responses|Anthropic Messages/;

const TEXT = {
  en: {
    add: 'Add connection',
    name: 'Connection name',
    models: 'Model IDs',
    saveVerify: 'Save and verify',
    save: 'Save',
    edit: 'Edit',
    ready: 'Ready',
    unverified: 'Not verified',
    summary: '2 of 2 models ready',
    details: 'API details',
    advanced: 'Advanced',
    model: 'Model',
    run: 'Run',
    region: 'Personal model connections',
  },
  'zh-CN': {
    add: '添加连接',
    name: '连接名称',
    models: '模型 ID',
    saveVerify: '保存并验证',
    save: '保存',
    edit: '编辑',
    ready: '可用',
    unverified: '未验证',
    summary: '2 个模型中有 2 个可用',
    details: 'API 详情',
    advanced: '高级',
    model: '模型',
    run: '运行',
    region: '个人模型连接',
  },
} as const;

test.beforeEach(async ({ page, request }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
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

for (const locale of ['en', 'zh-CN'] as const) {
  for (const viewport of [
    { width: 1440, height: 1000 },
    { width: 390, height: 844 },
  ]) {
    test(`automatic detection, per-model verification and task choice (${locale}, ${viewport.width}px)`, async ({
      page,
    }, testInfo) => {
      const text = TEXT[locale];
      await page.setViewportSize(viewport);
      await page.addInitScript((value) => {
        localStorage.setItem('agents.locale', value);
        localStorage.setItem('agents.theme', 'light');
      }, locale);

      let saved: ReturnType<typeof connectionView> | null = null;
      const created: Record<string, unknown>[] = [];
      const patched: Record<string, unknown>[] = [];
      const verified: unknown[] = [];
      await page.route('**/api/v1/agent/model-connections', async (route) => {
        if (route.request().method() === 'GET') {
          return route.fulfill({
            json: { enabled: true, configured: true, connections: saved ? [saved] : [] },
          });
        }
        const input = route.request().postDataJSON();
        created.push(input);
        saved = connectionView({
          id: connectionId,
          name: input.name,
          base_url: input.base_url,
          model_status: input.models.map(unverified),
        });
        return route.fulfill({ status: 201, json: saved });
      });
      await page.route(`**/api/v1/agent/model-connections/${connectionId}`, async (route) => {
        const changes = route.request().postDataJSON();
        patched.push(changes);
        const known = new Map(saved!.model_status.map((status) => [status.model, status]));
        saved = connectionView({
          ...saved!,
          ...(changes.name ? { name: changes.name } : {}),
          model_status: (changes.models ?? saved!.models).map(
            (model: string) => known.get(model) ?? unverified(model),
          ),
          updated_at: `2026-10-28T0${patched.length}:00:00Z`,
        });
        return route.fulfill({ json: saved });
      });
      await page.route(`**/api/v1/agent/model-connections/${connectionId}/verify`, (route) => {
        const { model } = route.request().postDataJSON();
        verified.push(model);
        const status = model === 'chat-model' ? chatOnly(model) : messagesOnly(model);
        // Recorded on the connection, as the server keeps it.
        saved = connectionView({
          ...saved!,
          model_status: saved!.model_status.map((item) => (item.model === model ? status : item)),
        });
        return route.fulfill({ json: { ...status, recorded: true } });
      });

      await page.goto('/settings/my-settings');
      const region = page.getByRole('region', { name: text.region, exact: true });
      await page.getByRole('button', { name: text.add, exact: true }).click();
      // The ordinary form asks for no protocol.
      await expect(region.getByRole('checkbox')).toHaveCount(0);
      await expect(region.locator('form')).not.toContainText(PROTOCOL_NAMES);
      await expect(page.getByText(text.advanced, { exact: true })).toBeVisible();
      await page.getByLabel(text.name, { exact: true }).fill(name);
      await page.getByLabel('Base URL', { exact: true }).fill(baseUrl);
      await page.getByLabel('API key', { exact: true }).fill('fixture-key-not-a-credential');
      await page.getByLabel(text.models, { exact: true }).fill('chat-model\nclaude-model');
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
        viewport.width,
      );
      await region.screenshot({ path: testInfo.outputPath('connection-form.png') });
      expect(verified).toEqual([]);
      await page.getByRole('button', { name: text.saveVerify, exact: true }).click();

      const card = page.getByRole('article', { name });
      await expect(card.getByText(text.summary, { exact: true })).toBeVisible();
      expect(created).toEqual([
        {
          name,
          base_url: baseUrl,
          api_key: 'fixture-key-not-a-credential',
          models: ['chat-model', 'claude-model'],
        },
      ]);
      expect(verified).toEqual(['chat-model', 'claude-model']);
      await expect(card.getByText(text.ready, { exact: true })).toHaveCount(2);
      await expect(card).toContainText('claude-code, codex, kilo, opencode, pi');
      // Agents are named; protocols are not, until the diagnostics are opened.
      const details = card.locator('details').filter({ hasText: text.details });
      await expect(details.locator('ul').first()).toBeHidden();
      await region.screenshot({ path: testInfo.outputPath('connection-verified.png') });
      await details.locator('summary').click();
      await expect(details).toContainText(/codex[:：] ?Responses/);
      await expect(details).toContainText('Anthropic Messages');
      await region.screenshot({ path: testInfo.outputPath('connection-diagnostics.png') });
      await details.locator('summary').click();

      // Adding a model sends only the list; the new one waits for verification.
      await card.getByRole('button', { name: text.edit, exact: true }).click();
      // The label also holds the textarea's initial text, so find it by its accessible name.
      const field = region.getByRole('textbox', { name: text.models, exact: true });
      await field.fill('chat-model\nclaude-model\nnew-model');
      await region.getByRole('button', { name: text.save, exact: true }).click();
      await expect(card.getByText(text.unverified, { exact: true })).toBeVisible();
      await expect(card.getByText(text.ready, { exact: true })).toHaveCount(2);
      expect(patched).toEqual([{ models: ['chat-model', 'claude-model', 'new-model'] }]);
      expect(verified).toHaveLength(2);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
        viewport.width,
      );

      const jobs: Record<string, unknown>[] = [];
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
                name,
                models: ['chat-model', 'claude-model', 'new-model'],
                model_runtimes: {
                  'chat-model': ['claude-code', 'codex', 'kilo', 'opencode', 'pi'],
                  'claude-model': ['claude-code'],
                },
              },
            ],
          },
        }),
      );
      await page.route('**/api/v1/agent/jobs', async (route) => {
        if (route.request().method() !== 'POST') return route.continue();
        jobs.push(route.request().postDataJSON());
        // Captured, not run: no upstream provider is fabricated here.
        return route.fulfill({ status: 422, json: { detail: 'Fixture captured the choice.' } });
      });
      await page.goto('/?repo=fixture%2Frepository');
      const agent = page.getByRole('button', { name: 'Agent', exact: true });
      const model = page.getByRole('button', { name: text.model, exact: true });
      await agent.click();
      await page.getByRole('option', { name: 'codex', exact: true }).click();
      await model.click();
      // Codex: only the model verified for it, and no protocol in its label.
      await expect(
        page.getByRole('option', { name: `chat-model · ${name}`, exact: true }),
      ).toBeVisible();
      await expect(
        page.getByRole('option', { name: new RegExp('claude-model|new-model') }),
      ).toHaveCount(0);
      await expect(page.getByRole('listbox')).not.toContainText(PROTOCOL_NAMES);
      await page.screenshot({ path: testInfo.outputPath('codex-models.png') });
      await page.keyboard.press('Escape');

      await agent.click();
      await page.getByRole('option', { name: 'claude-code', exact: true }).click();
      await model.click();
      await expect(
        page.getByRole('option', { name: `chat-model · ${name}`, exact: true }),
      ).toBeVisible();
      await page.getByRole('option', { name: `claude-model · ${name}`, exact: true }).click();
      await page.locator('#new-task-prompt').fill('Run Claude Code on my provider');
      await page.locator('#new-task-prompt').press('Enter');
      await expect.poll(() => jobs.length).toBe(1);
      expect(jobs[0]).toMatchObject({
        runtime: 'claude-code',
        model: 'claude-model',
        model_connection_id: connectionId,
      });
      expect(jobs[0]).not.toHaveProperty('reasoning_effort');

      // Codex cannot run the Messages-only model: the pick is kept, marked, and holds Run.
      await agent.click();
      await page.getByRole('option', { name: 'codex', exact: true }).click();
      await expect(model).toContainText(locale === 'en' ? 'unavailable' : '不可用');
      await expect(page.getByRole('button', { name: text.run, exact: true })).toBeDisabled();
    });
  }
}

test('the manual declaration stays available behind Advanced', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('agents.locale', 'en');
    localStorage.setItem('agents.theme', 'light');
  });
  let submitted: Record<string, unknown> | null = null;
  await page.route('**/api/v1/agent/model-connections', async (route) => {
    if (route.request().method() === 'GET') {
      return route.fulfill({ json: { enabled: true, configured: true, connections: [] } });
    }
    submitted = route.request().postDataJSON();
    return route.fulfill({
      status: 201,
      json: {
        ...connectionView({
          id: connectionId,
          name,
          base_url: baseUrl,
          model_status: [
            { ...unverified('claude-model'), status: 'declared', runtimes: ['claude-code'] },
          ],
        }),
        protocol_mode: 'manual',
        manual_protocols: ['anthropic_messages'],
      },
    });
  });
  await page.goto('/settings/my-settings');
  const region = page.getByRole('region', { name: 'Personal model connections', exact: true });
  await page.getByRole('button', { name: 'Add connection', exact: true }).click();
  await page.getByLabel('Connection name', { exact: true }).fill(name);
  await page.getByLabel('Base URL', { exact: true }).fill(baseUrl);
  await page.getByLabel('API key', { exact: true }).fill('fixture-key-not-a-credential');
  await page.getByLabel('Model IDs', { exact: true }).fill('claude-model');
  await page.getByText('Advanced', { exact: true }).click();
  await page.getByRole('radio', { name: 'Declare the APIs this endpoint offers' }).check();
  await expect(region.getByRole('button', { name: 'Save', exact: true })).toBeDisabled();
  await page.getByRole('checkbox', { name: 'Anthropic Messages', exact: true }).check();
  await region.getByRole('button', { name: 'Save', exact: true }).click();
  const card = page.getByRole('article', { name });
  await expect(card.getByText('APIs declared manually', { exact: true })).toBeVisible();
  await expect(card.getByText('Declared', { exact: true })).toBeVisible();
  expect(submitted).toMatchObject({ models: ['claude-model'], protocols: ['anthropic_messages'] });
});

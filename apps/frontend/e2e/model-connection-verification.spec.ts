import { expect, test } from '@playwright/test';

import {
  chatOnly,
  connectionView,
  messagesOnly,
  refusedStatus,
  verificationRefusal,
} from './modelConnectionFixtures';

// Verify before saving, in the real Next app. The owner gives a name, a Base
// URL, a key and model IDs; "Verify and save" asks the server, which verifies
// every model first and stores nothing unless each has an agent that can use
// it. A refusal keeps every input. Model IDs can be picked from the
// provider's own list, which saves nothing. No protocol is asked for or named,
// in the form or on the card.

const connectionId = `mconn_${'e'.repeat(32)}`;
const name = 'Mixed provider';
const baseUrl = 'https://provider.example/v1';
const PROTOCOL_NAMES = /Chat Completions|Responses|Anthropic Messages/;

const TEXT = {
  en: {
    add: 'Add connection',
    name: 'Connection name',
    models: 'Model IDs',
    verifySave: 'Verify and save',
    save: 'Save',
    cancel: 'Cancel',
    edit: 'Edit',
    remove: (model: string) => `Remove ${model}`,
    refused: /Not saved/,
    reason: 'The model did not call the offered tool exactly once.',
    authReason: 'The provider refused the API key (401/403).',
    works: 'Works with',
    load: 'Load models from provider',
    pick: (model: string) => `Add ${model}`,
    model: 'Model',
    run: 'Run',
    region: 'Personal model connections',
    unavailable: 'unavailable',
  },
  'zh-CN': {
    add: '添加连接',
    name: '连接名称',
    models: '模型 ID',
    verifySave: '验证并保存',
    save: '保存',
    cancel: '取消',
    edit: '编辑',
    remove: (model: string) => `移除 ${model}`,
    refused: /未保存/,
    reason: '模型未按要求调用工具。',
    authReason: '服务商拒绝了 API key（401/403），请检查密钥。',
    works: '可用于',
    load: '从服务商获取模型',
    pick: (model: string) => `添加 ${model}`,
    model: '模型',
    run: '运行',
    region: '个人模型连接',
    unavailable: '不可用',
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
  for (const variant of [
    { width: 1440, height: 1000, theme: 'light' },
    { width: 390, height: 844, theme: 'dark' },
  ]) {
    test(`verify before saving, retry after a refusal, and choose per model (${locale}, ${variant.width}px, ${variant.theme})`, async ({
      page,
    }, testInfo) => {
      const text = TEXT[locale];
      await page.setViewportSize({ width: variant.width, height: variant.height });
      await page.addInitScript(
        ({ value, theme }) => {
          localStorage.setItem('agents.locale', value);
          localStorage.setItem('agents.theme', theme);
        },
        { value: locale, theme: variant.theme },
      );

      let saved: ReturnType<typeof connectionView> | null = null;
      const created: Record<string, unknown>[] = [];
      const patched: Record<string, unknown>[] = [];
      const listings: Record<string, unknown>[] = [];
      await page.route('**/api/v1/agent/model-connections/discover-models', async (route) => {
        listings.push(route.request().postDataJSON());
        return route.fulfill({ json: { models: ['chat-model', 'claude-model', 'other-model'] } });
      });
      await page.route('**/api/v1/agent/model-connections', async (route) => {
        if (route.request().method() === 'GET') {
          return route.fulfill({
            json: {
              enabled: true,
              configured: true,
              max_models: 8,
              connections: saved ? [saved] : [],
            },
          });
        }
        const input = route.request().postDataJSON();
        created.push(input);
        if ((input.models as string[]).includes('bad-model')) {
          // Verified first; one model is unusable, so nothing is stored.
          return route.fulfill({
            status: 422,
            json: verificationRefusal([
              chatOnly('chat-model'),
              messagesOnly('claude-model'),
              refusedStatus('bad-model', 'The model did not call the tool.'),
            ]),
          });
        }
        saved = connectionView({
          id: connectionId,
          name: input.name,
          base_url: input.base_url,
          model_status: [chatOnly('chat-model'), messagesOnly('claude-model')],
        });
        return route.fulfill({ status: 201, json: saved });
      });
      await page.route(`**/api/v1/agent/model-connections/${connectionId}`, async (route) => {
        const changes = route.request().postDataJSON();
        patched.push(changes);
        if (changes.api_key) {
          return route.fulfill({
            status: 422,
            json: verificationRefusal([
              {
                ...refusedStatus('chat-model', 'The provider refused the key.', 'auth'),
                status: 'failed',
              },
              {
                ...refusedStatus('claude-model', 'The provider refused the key.', 'auth'),
                status: 'failed',
              },
            ]),
          });
        }
        saved = { ...saved!, ...changes, updated_at: `2026-10-28T0${patched.length}:00:00Z` };
        return route.fulfill({ json: saved });
      });

      await page.goto('/settings/my-settings');
      const region = page.getByRole('region', { name: text.region, exact: true });
      await page.getByRole('button', { name: text.add, exact: true }).click();
      const form = region.locator('form');
      // Compact: no protocol control, radio, checkbox, textarea or native menu.
      await expect(form.getByRole('checkbox')).toHaveCount(0);
      await expect(form.getByRole('radio')).toHaveCount(0);
      await expect(form.locator('textarea, select, datalist')).toHaveCount(0);
      await expect(form).not.toContainText(PROTOCOL_NAMES);
      await page.getByLabel(text.name, { exact: true }).fill(name);
      await page.getByLabel('Base URL', { exact: true }).fill(baseUrl);
      await page.getByLabel('API key', { exact: true }).fill('fixture-key-not-a-credential');
      // Picking from the provider's list only fills the form.
      await form.getByRole('button', { name: text.load, exact: true }).click();
      await form.getByRole('button', { name: text.pick('chat-model'), exact: true }).click();
      expect(listings).toEqual([{ base_url: baseUrl, api_key: 'fixture-key-not-a-credential' }]);
      expect(created).toEqual([]);
      await expect(form).not.toContainText(PROTOCOL_NAMES);
      const modelInput = page.getByLabel(text.models, { exact: true });
      for (const model of ['claude-model', 'bad-model']) {
        await modelInput.fill(model);
        await modelInput.press('Enter');
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
        variant.width,
      );
      await form.screenshot({ path: testInfo.outputPath('connection-form.png') });

      await form.getByRole('button', { name: text.verifySave, exact: true }).click();
      await expect(form.getByRole('alert')).toContainText(text.refused);
      // Nothing was saved, and every input is still there to fix and retry.
      await expect(page.getByRole('article', { name })).toHaveCount(0);
      await expect(page.getByLabel('API key', { exact: true })).toHaveValue(
        'fixture-key-not-a-credential',
      );
      await expect(form).toContainText(text.reason);
      await form.screenshot({ path: testInfo.outputPath('connection-refused.png') });

      await form.getByRole('button', { name: text.remove('bad-model'), exact: true }).click();
      await form.getByRole('button', { name: text.verifySave, exact: true }).click();
      const card = page.getByRole('article', { name });
      await expect(card).toBeVisible();
      expect(created).toEqual([
        {
          name,
          base_url: baseUrl,
          api_key: 'fixture-key-not-a-credential',
          models: ['chat-model', 'claude-model', 'bad-model'],
        },
        {
          name,
          base_url: baseUrl,
          api_key: 'fixture-key-not-a-credential',
          models: ['chat-model', 'claude-model'],
        },
      ]);
      await expect(card).toContainText(`${text.works} Claude Code, Codex, Kilo Code, OpenCode, pi`);
      await expect(card).toContainText(`${text.works} Claude Code`);
      // No diagnostics on the card: no expander, no API names.
      await expect(card.locator('details, summary')).toHaveCount(0);
      await expect(card).not.toContainText(PROTOCOL_NAMES);
      await region.screenshot({ path: testInfo.outputPath('connection-saved.png') });

      // A new key is verified first; its refusal leaves the saved card as it was.
      await card.getByRole('button', { name: text.edit, exact: true }).click();
      await page.getByLabel('API key', { exact: true }).fill('fixture-replacement-key');
      await form.getByRole('button', { name: text.verifySave, exact: true }).click();
      await expect(form.getByRole('alert')).toContainText(text.refused);
      await expect(form).toContainText(text.authReason);
      await form.getByRole('button', { name: text.cancel, exact: true }).click();
      await expect(card).toContainText(`${text.works} Claude Code, Codex, Kilo Code, OpenCode, pi`);

      // A rename alone saves without verifying.
      await card.getByRole('button', { name: text.edit, exact: true }).click();
      await page.getByLabel(text.name, { exact: true }).fill('Renamed provider');
      await form.getByRole('button', { name: text.save, exact: true }).click();
      await expect(page.getByRole('article', { name: 'Renamed provider' })).toBeVisible();
      expect(patched).toEqual([
        { api_key: 'fixture-replacement-key' },
        { name: 'Renamed provider' },
      ]);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
        variant.width,
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
                models: ['chat-model', 'claude-model'],
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
      await expect(
        page.getByRole('option', { name: `chat-model · ${name}`, exact: true }),
      ).toBeVisible();
      await expect(page.getByRole('option', { name: /claude-model/ })).toHaveCount(0);
      await expect(page.getByRole('listbox')).not.toContainText(PROTOCOL_NAMES);
      await page.keyboard.press('Escape');

      await agent.click();
      await page.getByRole('option', { name: 'claude-code', exact: true }).click();
      await model.click();
      await page.getByRole('option', { name: `claude-model · ${name}`, exact: true }).click();
      await page.locator('#new-task-prompt').fill('Run Claude Code on my provider');
      await page.locator('#new-task-prompt').press('Enter');
      await expect.poll(() => jobs.length).toBe(1);
      expect(jobs[0]).toMatchObject({
        runtime: 'claude-code',
        model: 'claude-model',
        model_connection_id: connectionId,
      });

      // Codex cannot run the Messages-only model: the pick is kept, marked, and holds Run.
      await agent.click();
      await page.getByRole('option', { name: 'codex', exact: true }).click();
      await expect(model).toContainText(text.unavailable);
      await expect(page.getByRole('button', { name: text.run, exact: true })).toBeDisabled();
    });
  }
}

// Moved from dropdowns.spec.ts with the dropdown scope split, and adapted to
// verify-before-save. These cases run with an admin session and empty admin
// settings, the setup they were written against.
test.describe('verification without a protocol menu', () => {
  test.beforeEach(async ({ page }) => {
    await page.route('**/api/v1/session/me', (route) =>
      route.fulfill({
        json: {
          id: 'browser-owner',
          agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
        },
      }),
    );
    await page.route('**/api/admin/agent/settings', (route) =>
      route.fulfill({
        json: {
          revision: 1,
          definitions: [],
          values: {},
          secret_configured: {},
          bootstrap: [],
          setup: { required: false, groups: [], bootstrap_missing: [] },
        },
      }),
    );
  });

  for (const variant of [
    { name: 'desktop light', width: 1440, height: 900, theme: 'light' },
    { name: 'mobile dark', width: 390, height: 844, theme: 'dark' },
  ]) {
    test(`verify and save needs no protocol menu and fits the viewport (${variant.name})`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width: variant.width, height: variant.height });
      await page.addInitScript((theme) => {
        localStorage.setItem('agents.locale', 'en');
        localStorage.setItem('agents.theme', theme);
      }, variant.theme);
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
      const posted: unknown[] = [];
      let release!: () => void;
      const allowed = new Promise<void>((resolve) => {
        release = resolve;
      });
      let saved: ReturnType<typeof connectionView> | null = null;
      await page.route('**/api/v1/agent/model-connections', async (route) => {
        if (route.request().method() === 'GET') {
          return route.fulfill({
            json: { enabled: true, configured: true, connections: saved ? [saved] : [] },
          });
        }
        posted.push(route.request().postDataJSON());
        // The server verifies before it answers; the form waits for it.
        await allowed;
        saved = connectionView({
          id: connectionId,
          name: 'Fixture provider',
          base_url: 'https://provider.example/v1',
          model_status: [chatOnly('fixture-small'), chatOnly('fixture-large')],
        });
        return route.fulfill({ status: 201, json: saved });
      });
      await page.goto('/settings/my-settings');
      await page.getByRole('button', { name: 'Add connection', exact: true }).click();
      const form = page.getByRole('region', { name: 'Personal model connections' }).locator('form');
      await expect(form.getByRole('combobox')).toHaveCount(0);
      await expect(page.locator('select:visible')).toHaveCount(0);
      await expect(page.locator('datalist')).toHaveCount(0);
      await page.getByLabel('Connection name', { exact: true }).fill('Fixture provider');
      await page.getByLabel('Base URL', { exact: true }).fill('https://provider.example/v1');
      await page.getByLabel('API key', { exact: true }).fill('fixture-key-not-a-credential');
      const models = page.getByLabel('Model IDs', { exact: true });
      await models.fill('fixture-small');
      await models.press('Enter');
      await models.fill('fixture-large');
      expect(posted).toEqual([]);
      await form.getByRole('button', { name: 'Verify and save', exact: true }).click();
      await expect.poll(() => posted.length).toBe(1);
      await expect(form.getByRole('button', { name: 'Verifying…', exact: true })).toBeDisabled();
      // A pending save can still commit on the server, so Cancel is disabled.
      await expect(form.getByRole('button', { name: 'Cancel', exact: true })).toBeDisabled();
      expect(posted).toEqual([
        {
          name: 'Fixture provider',
          base_url: 'https://provider.example/v1',
          api_key: 'fixture-key-not-a-credential',
          models: ['fixture-small', 'fixture-large'],
        },
      ]);
      await form.screenshot({
        path: testInfo.outputPath('verify-and-save-pending.png'),
        animations: 'disabled',
      });
      release();
      const card = page.getByRole('article', { name: 'Fixture provider' });
      await expect(card.getByText(/^Works with/)).toHaveCount(2);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
        variant.width,
      );
      await card.screenshot({
        path: testInfo.outputPath('verified-connection.png'),
        animations: 'disabled',
      });
    });
  }
});

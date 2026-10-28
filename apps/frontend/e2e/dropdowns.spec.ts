import { expect, test } from '@playwright/test';

const tableNames = [
  'agent_admin_audit_log',
  'agent_attempt_resources',
  'agent_attempts',
  'agent_job_events',
  'agent_jobs',
  'agent_runner_hosts',
  'agent_sandbox_snapshots',
  'agent_sandboxes',
  'agent_snapshot_jobs',
  'agent_thread_messages',
  'agent_thread_sandbox_memberships',
  'agent_threads',
  'agent_tool_call_resources',
];

test.beforeEach(async ({ page, request }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
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

test.afterEach(async ({ request }) => {
  const state = await (await request.get('/__fixture/state')).json();
  expect(state.unknown).toEqual([]);
});

for (const variant of [
  { name: 'desktop light', width: 1440, height: 900, theme: 'light' },
  { name: 'mobile dark', width: 390, height: 844, theme: 'dark' },
]) {
  test(`database dropdowns preserve query values and fit the viewport (${variant.name})`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: variant.width, height: variant.height });
    await page.addInitScript((theme) => localStorage.setItem('agents.theme', theme), variant.theme);
    await page.route('**/api/admin/agent/database-tools', (route) =>
      route.fulfill({ json: { enabled: false, available: false, url: null } }),
    );
    await page.route('**/api/admin/agent/database/tables', (route) =>
      route.fulfill({
        json: {
          tables: tableNames.map((name) => ({
            name,
            columns: [
              { name: 'id', data_type: 'uuid', nullable: false },
              { name: 'title', data_type: 'text', nullable: true },
            ],
            primary_key: ['id'],
          })),
        },
      }),
    );
    let submitted: unknown;
    await page.route('**/api/admin/agent/database/query', async (route) => {
      submitted = route.request().postDataJSON();
      await route.fulfill({
        json: {
          table: 'agent_jobs',
          columns: ['id', 'title'],
          rows: [],
          limit: 100,
          offset: 0,
          has_more: false,
          elapsed_ms: 1,
          text_limit: 1000,
        },
      });
    });
    await page.goto('/admin/database');
    const table = page.getByRole('combobox', { name: 'Table', exact: true });
    await table.click();
    const list = page.getByRole('listbox');
    await expect(list).toBeVisible();
    const bounds = await list.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.y).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(variant.width);
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(variant.height);
    await page.screenshot({
      path: testInfo.outputPath('database-dropdown.png'),
      animations: 'disabled',
    });
    await page.getByRole('combobox', { name: 'Search…', exact: true }).fill('agent_jobs');
    await expect(page.getByRole('option')).toHaveCount(1);
    await page.getByRole('option', { name: 'agent_jobs', exact: true }).click();
    await expect(table).toContainText('agent_jobs');
    await expect(
      page.getByRole('combobox', { name: 'Sort direction', exact: true }),
    ).toBeDisabled();

    await page.getByRole('button', { name: 'Add filter', exact: true }).click();
    await page.getByRole('combobox', { name: 'Column 1', exact: true }).click();
    await page.getByRole('option', { name: 'title', exact: true }).click();
    await page.getByLabel('Value 1').fill('worker%_one');
    await page.getByRole('combobox', { name: 'Sort by', exact: true }).click();
    await page.getByRole('option', { name: 'id', exact: true }).click();
    await page.getByRole('combobox', { name: 'Sort direction', exact: true }).click();
    await page.keyboard.press('End');
    await page.keyboard.press('Enter');
    await expect(page.getByRole('combobox', { name: 'Sort direction', exact: true })).toContainText(
      'Descending',
    );
    await page.getByRole('combobox', { name: 'Rows per page', exact: true }).click();
    await page.getByRole('option', { name: '100', exact: true }).click();
    const rows = page.getByRole('combobox', { name: 'Rows per page', exact: true });
    await rows.click();
    await page.keyboard.press('Tab');
    await expect(page.getByRole('listbox')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Run query', exact: true })).toBeFocused();
    await rows.focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('listbox')).toBeVisible();
    await page.keyboard.press('Shift+Tab');
    await expect(page.getByRole('listbox')).toHaveCount(0);
    await expect(page.getByRole('combobox', { name: 'Sort direction', exact: true })).toBeFocused();
    await page.getByRole('button', { name: 'Run query', exact: true }).click();
    await expect
      .poll(() => submitted)
      .toEqual({
        table: 'agent_jobs',
        filters: [{ column: 'title', operator: 'eq', value: 'worker%_one' }],
        order_by: 'id',
        order_direction: 'desc',
        limit: 100,
        offset: 0,
      });
    await expect(page.locator('select:visible')).toHaveCount(0);
  });
}

test('a dropdown in an environment dialog owns Escape and releases Tab to the dialog', async ({
  page,
}) => {
  await page.route('**/api/v1/agent/teams', (route) => route.fulfill({ json: { teams: [] } }));
  await page.goto('/settings/environments');
  await page.getByRole('button', { name: 'New environment', exact: true }).click();
  const dialog = page.getByRole('dialog');
  const repository = dialog.getByRole('combobox', { name: 'Repository', exact: true });
  await repository.click();
  await expect(page.getByRole('listbox')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('listbox')).toHaveCount(0);
  await expect(dialog).toBeVisible();
  await expect(repository).toBeFocused();
  await repository.click();
  await page.getByRole('option', { name: 'fixture/repository', exact: true }).click();
  await expect(repository).toContainText('fixture/repository');
  await repository.click();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('listbox')).toHaveCount(0);
  await expect(dialog).toBeVisible();
  await expect(repository).not.toBeFocused();
  await expect(dialog.locator(':focus')).toHaveCount(1);
  await expect(page.locator('select:visible')).toHaveCount(0);
});

test('default model suggestions preserve free text and support keyboard and pointer selection', async ({
  page,
}, testInfo) => {
  await page.route('**/api/admin/agent/settings', (route) =>
    route.fulfill({
      json: {
        revision: 1,
        definitions: [
          {
            key: 'default_model',
            label: 'Default model for new tasks',
            description: 'The model a new task starts on.',
            category: 'Execution',
            kind: 'string',
            default: 'deepseek-v4-flash',
            secret: false,
          },
        ],
        values: { default_model: 'deepseek-v4-flash' },
        secret_configured: {},
        bootstrap: [],
        setup: { required: false, groups: [], bootstrap_missing: [] },
      },
    }),
  );
  await page.route('**/api/admin/agent/runner-hosts', (route) =>
    route.fulfill({ json: { hosts: [], host_setting_definitions: [], runtime_versions: {} } }),
  );
  await page.route('**/api/admin/agent/sandbox-archive-settings', (route) =>
    route.fulfill({ json: null }),
  );
  await page.route('**/api/v1/agent/model-preference', (route) =>
    route.fulfill({
      json: {
        default_model: null,
        last_model: null,
        effective_model: 'deepseek-v4-flash',
        deployment_default: 'deepseek-v4-flash',
        models: ['deepseek-v4-flash', 'glm-5.1'],
      },
    }),
  );
  await page.goto('/admin/settings');
  await page.getByRole('button', { name: 'Edit Sandbox execution settings', exact: true }).click();
  const model = page.getByRole('combobox', { name: 'Default model for new tasks', exact: true });
  await model.fill('glm');
  await expect(page.getByRole('option', { name: 'glm-5.1', exact: true })).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath('model-suggestions.png'),
    animations: 'disabled',
  });
  await page.keyboard.press('Escape');
  await expect(page.getByRole('listbox')).toHaveCount(0);
  await expect(model).toHaveValue('glm');
  await page.keyboard.press('ArrowDown');
  await expect(page.getByRole('option')).toHaveCount(2);
  await page.keyboard.press('ArrowDown');
  await page.keyboard.press('Enter');
  await expect(model).toHaveValue('glm-5.1');
  await model.fill('deepseek');
  await page.getByRole('option', { name: 'deepseek-v4-flash', exact: true }).click();
  await expect(model).toHaveValue('deepseek-v4-flash');
  await expect(model).toBeFocused();
  await model.fill('custom-model');
  await page.keyboard.press('Tab');
  await expect(model).toHaveValue('custom-model');
  await expect(page.getByText(/Your account is not offered this model/)).toBeVisible();
  await expect(page.locator('datalist')).toHaveCount(0);
});

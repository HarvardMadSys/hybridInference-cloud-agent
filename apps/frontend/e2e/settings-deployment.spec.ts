import { expect, test } from '@playwright/test';

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
        definitions: [
          {
            key: 'pgadmin_url',
            label: 'pgAdmin URL',
            category: 'Database tools',
            kind: 'string',
            default: '',
            secret: false,
            description: 'Database tools address.',
          },
        ],
        values: { pgadmin_url: '' },
        secret_configured: {},
        bootstrap: [
          {
            id: 'settings_root_key',
            key: 'AGENT_SETTINGS_ENCRYPTION_KEY / AGENT_SESSION_SECRET',
            label: 'Settings encryption key',
            description:
              'A dedicated root or the deployment session-secret fallback protects stored secrets.',
            requirement: 'required',
            state: 'fallback',
            actions: [{ target: 'key_rotation_guide', label: 'Read key management notes' }],
          },
          {
            id: 'compute_config_file',
            key: 'AGENT_COMPUTE_CONFIG',
            label: 'Compute deployment profiles',
            description: 'Manage registered profiles in Compute setup.',
            requirement: 'conditional',
            state: 'not_used',
            actions: [{ target: 'compute_setup', label: 'Open Compute setup' }],
          },
          {
            id: 'pgadmin_bootstrap',
            key: 'AGENT_PGADMIN_BOOTSTRAP_URL / AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL / AGENT_PGADMIN_BOOTSTRAP_DIR',
            label: 'pgAdmin first-time setup',
            description: 'Edit the saved URL or open Admin Database.',
            requirement: 'optional',
            state: 'unknown',
            actions: [{ target: 'pgadmin_url', label: 'Edit pgAdmin URL' }],
          },
        ],
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
});

test.afterEach(async ({ request }) => {
  const state = await (await request.get('/__fixture/state')).json();
  expect(state.unknown).toEqual([]);
});

for (const width of [1440, 390]) {
  test(`deployment links, deep links and drafts work at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/admin/settings#application-bootstrap-AGENT_COMPUTE_CONFIG');
    const panel = page.getByRole('tabpanel', { name: 'Deployment', exact: true });
    await expect(panel).toBeVisible();
    await expect(page.locator('#deployment-compute_config_file')).toBeFocused();
    await expect(page.getByRole('link', { name: 'Open Compute setup' })).toHaveAttribute(
      'href',
      '/admin/hosts#compute-setup',
    );
    await expect(page.getByRole('link', { name: 'Read key management notes' })).toHaveAttribute(
      'target',
      '_blank',
    );
    const bounds = await panel.boundingBox();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(width);
    expect(await panel.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(
      true,
    );
    await page.screenshot({ path: testInfo.outputPath('deployment-setup.png'), fullPage: true });

    await page.getByRole('button', { name: 'Edit pgAdmin URL' }).click();
    await expect(page.locator('#application-setting-pgadmin_url')).toBeFocused();
    await expect(page).toHaveURL(/#application-setting-pgadmin_url$/);
    await page.getByRole('button', { name: 'Edit Database tools settings', exact: true }).click();
    const field = page.getByRole('textbox', { name: 'pgAdmin URL', exact: true });
    await field.fill('https://pgadmin.example.test');
    await page.getByRole('tab', { name: 'Deployment', exact: true }).click();
    await expect(panel).toBeVisible();
    await page.getByRole('tab', { name: 'Operations', exact: true }).click();
    await expect(field).toHaveValue('https://pgadmin.example.test');
    await page.reload();
    await expect(page.getByRole('tabpanel', { name: 'Operations', exact: true })).toBeVisible();
    await expect(page.locator('#application-setting-pgadmin_url')).toBeFocused();
  });
}

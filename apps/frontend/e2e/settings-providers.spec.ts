import { expect, test, type Page } from '@playwright/test';

type Value = string | number | boolean;
type Field = [key: string, label: string, kind: string, value: Value, extra?: object];

// A slice of the server's account registry, labelled as the server labels it.
const FIELDS: Field[] = [
  ['accounts.email_provider', 'Email provider', 'string', 'smtp'],
  ['accounts.smtp_host', 'SMTP host', 'string', 'smtp.example.test'],
  ['accounts.smtp_password', 'SMTP password', 'string', '', { secret: true }],
  ['accounts.tencent_ses_region', 'Tencent SES region', 'string', 'ap-guangzhou'],
  ['accounts.tencent_ses_from_email', 'Tencent SES sender address', 'string', ''],
  ['accounts.sms_enabled', 'Phone sign-in', 'boolean', false],
  [
    'accounts.sms_provider',
    'Text message provider',
    'string',
    'none',
    {
      choices: ['none', 'tencent'],
    },
  ],
  ['accounts.sms_code_ttl_s', 'Code lifetime (seconds)', 'integer', 300],
  ['accounts.tencent_secret_id', 'Tencent Cloud SecretId', 'string', '', { secret: true }],
  ['accounts.tencent_secret_key', 'Tencent Cloud SecretKey', 'string', '', { secret: true }],
  ['accounts.tencent_sms_app_id', 'Tencent SMS application ID', 'string', ''],
  ['accounts.turnstile_site_key', 'Turnstile site key', 'string', ''],
];

function settings(revision: number, values: Record<string, Value>) {
  return {
    revision,
    definitions: FIELDS.map(([key, label, kind, value, extra]) => ({
      key,
      label,
      kind,
      default: value,
      category: 'Accounts',
      description: '',
      secret: false,
      ...extra,
    })),
    values: Object.fromEntries(FIELDS.map(([key, , , value]) => [key, values[key] ?? value])),
    secret_configured: {},
    bootstrap: [],
    setup: { required: false, groups: [], bootstrap_missing: [] },
  };
}

let writes: unknown[] = [];

test.beforeEach(async ({ page, request }) => {
  expect((await request.post('/__fixture/reset')).ok()).toBe(true);
  writes = [];
  let current = settings(1, {});
  await page.route('**/api/v1/session/me', (route) =>
    route.fulfill({
      json: {
        id: 'browser-owner',
        agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
      },
    }),
  );
  await page.route('**/api/admin/agent/settings', async (route) => {
    if (route.request().method() === 'PUT') {
      const body = route.request().postDataJSON() as { settings: Record<string, Value> };
      writes.push(body);
      current = settings(current.revision + 1, { ...current.values, ...body.settings });
    }
    await route.fulfill({ json: current });
  });
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

const setting = (page: Page, key: string) => page.locator(`[id="application-setting-${key}"]`);

async function search(page: Page, query: string) {
  await page.getByRole('searchbox', { name: 'Search settings' }).fill(query);
  return page.getByRole('region', { name: 'Settings search results' }).getByRole('button');
}

test('mail and phone sign-in show only the selected provider’s fields', async ({
  page,
}, testInfo) => {
  // Tall enough that a screenshot shows every account group; the page scrolls inside.
  await page.setViewportSize({ width: 1440, height: 2600 });
  await page.goto(`/admin/settings?tab=application-${encodeURIComponent('Identity & access')}`);
  await expect(setting(page, 'accounts.smtp_host')).toHaveText('smtp.example.test');
  for (const hidden of [
    'accounts.tencent_ses_region',
    'accounts.sms_provider',
    'accounts.tencent_sms_app_id',
    'accounts.tencent_secret_id',
  ]) {
    await expect(setting(page, hidden)).toHaveCount(0);
  }
  await expect(page.getByRole('heading', { name: 'Tencent Cloud credentials' })).toHaveCount(0);
  await expect(setting(page, 'accounts.sms_enabled')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('1-smtp-saved.png'), fullPage: true });

  // Choosing Tencent SES shows its fields and the credentials it needs, unsaved.
  await page.getByRole('button', { name: 'Edit Account email settings' }).click();
  await setting(page, 'accounts.smtp_host').fill('draft.example.test');
  await setting(page, 'accounts.email_provider').fill('tencent_ses');
  await expect(setting(page, 'accounts.smtp_host')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Tencent Cloud credentials' })).toBeVisible();
  await setting(page, 'accounts.tencent_ses_from_email').fill('noreply@example.test');
  await expect(await search(page, 'SMTP host')).toHaveCount(0);
  const found = await search(page, 'SecretKey');
  await expect(found).toHaveCount(1);
  await expect(found).toHaveAccessibleName('Open Tencent Cloud SecretKey in Identity & access');
  await page.getByRole('searchbox', { name: 'Search settings' }).fill('');
  await page.screenshot({ path: testInfo.outputPath('2-ses-draft.png'), fullPage: true });

  await page.getByRole('button', { name: 'Review Account email changes' }).click();
  const dialog = page.getByRole('dialog', { name: 'Review changes' });
  await expect(dialog.getByText('2 changes')).toBeVisible();
  await dialog.getByRole('button', { name: 'Apply changes' }).click();
  await expect(page.getByText('Account email settings saved.')).toBeVisible();
  expect(writes).toEqual([
    {
      settings: {
        'accounts.email_provider': 'tencent_ses',
        'accounts.tencent_ses_from_email': 'noreply@example.test',
      },
      expected_revision: 1,
    },
  ]);

  // Phone sign-in is turned on and given Tencent from its own editor.
  await page.getByRole('button', { name: 'Edit Phone sign-in settings' }).click();
  await setting(page, 'accounts.sms_enabled').click();
  await page.getByRole('option', { name: 'Enabled' }).click();
  await expect(setting(page, 'accounts.sms_code_ttl_s')).toBeVisible();
  await expect(setting(page, 'accounts.tencent_sms_app_id')).toHaveCount(0);
  await setting(page, 'accounts.sms_provider').click();
  await page.getByRole('option', { name: 'tencent' }).click();
  await setting(page, 'accounts.tencent_sms_app_id').fill('1400000000');
  await page.screenshot({ path: testInfo.outputPath('3-sms-tencent-draft.png'), fullPage: true });
  await page.getByRole('button', { name: 'Review Phone sign-in changes' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Apply changes' }).click();
  await expect(page.getByText('Phone sign-in settings saved.')).toBeVisible();
  expect(writes[1]).toEqual({
    settings: {
      'accounts.sms_enabled': true,
      'accounts.sms_provider': 'tencent',
      'accounts.tencent_sms_app_id': '1400000000',
    },
    expected_revision: 2,
  });
  await expect(setting(page, 'accounts.smtp_host')).toHaveCount(0);
});

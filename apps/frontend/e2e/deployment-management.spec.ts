import { expect, test, type Locator, type Page } from '@playwright/test';

// Exported from the backend's management catalog and deployment contract, so
// every label the page derives from them is the real one.
import contract from './deployment-contract.json';

// The production Next app in a real browser, against a fixture of the admin API
// that holds what the real routes return. Sealing, the forms and every state
// shown run for real; what the control plane does with a request has its own
// tests over real databases.

const evidence = process.env.CLOUD_AGENT_E2E_EVIDENCE;

// A real P-256 public key, so the browser really seals to it.
const PUBLIC_KEY =
  'BORQnMqmnLhuHCAN7oWYoxg6QRUm4DbG8x1Yh5MxOq5cwPiWh0xVMIlZGztSbAq+prytqBZcWO/LI6c+VB9FuJo=';
const CONTROL_ID = 'a'.repeat(32);
const RELEASE = '7c2d9f04e1b3a6c8d5f2e9b0a4c7d1e6f3b8a2c5';
const PREVIOUS = '1e4b7a9c2d5f8e0b3a6c9d2f5e8b1a4c7d0f3e6b';
const LONG_HOST = 'gpu-a100-eu-west-3-rack-12-node-07';
const NOW = '2026-10-30T09:40:00+00:00';

/** What must never be shown: configuration names, codes, commands, files, process ids. */
const PLUMBING = [
  /\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b/,
  /\b[a-z]+_[a-z0-9_]+\b/,
  /python -m|deploy\.agent|unfence|--root|--operation-id/,
  /\.env\b|AGENTS\.md|authorized_keys|policy\.json/,
  /\b(?:api|mcp)@/,
];

function machine(
  id: string,
  name: string,
  role: 'control-plane' | 'host',
  observed: Record<string, unknown>,
  online = true,
) {
  return {
    agent_id: id,
    name,
    role,
    machine: name,
    public_key: PUBLIC_KEY,
    enrolled_at: '2026-10-01T08:00:00+00:00',
    last_report_at: online ? NOW : '2026-10-30T07:12:00+00:00',
    revoked: false,
    online,
    observed: {
      deployed_commit: RELEASE,
      config_rev: 3,
      config_versions: [1, 2, 3],
      managed_keys: [],
      unmanaged_keys: [],
      legacy_names_present: [],
      releases: {
        ['e'.repeat(64)]: { source_commit: RELEASE, database_revision: '0112' },
        ['d'.repeat(64)]: { source_commit: PREVIOUS, database_revision: '0111' },
      },
      services: {},
      policy: {
        allowed: {},
        kata: [],
        has_kata_helper: role === 'host',
        has_database_tools: false,
      },
      secret_ids: {},
      ...observed,
    },
    local_operations: [],
  };
}

function agents() {
  return [
    machine(CONTROL_ID, 'control-1', 'control-plane', {
      config_rev: 7,
      config_versions: [3, 4, 5, 6, 7],
      unmanaged_keys: ['A', 'B', 'C'],
      legacy_names_present: ['AGENT_WORKSPACE_BROKER_URL', 'AGENT_RUNNER_REPLICAS'],
      services: { 'control-plane': true, 'mcp-relay': true, web: true, edge: true },
      database: {
        host: 'db.internal',
        port: 5432,
        database: 'cloudagent',
        user: 'cloudagent_app',
        query: '',
      },
      policy: {
        allowed: { AGENT_OPERATION_PROFILING_MODE: ['disabled', 'admin'] },
        kata: [],
        has_kata_helper: false,
        has_database_tools: true,
      },
    }),
    machine('1'.repeat(32), 'gpu-1', 'host', {
      kata: { version: '3.10.1', sha256: 'f'.repeat(64) },
      policy: {
        allowed: { AGENT_SANDBOX_RUNTIME: ['io.containerd.kata.v2', 'kata'] },
        kata: [],
        has_kata_helper: true,
        has_database_tools: false,
      },
    }),
    machine(
      '2'.repeat(32),
      'gpu-2',
      'host',
      { deployed_commit: PREVIOUS, kata: { version: '3.9.0', sha256: 'f'.repeat(64) } },
      false,
    ),
    machine('3'.repeat(32), LONG_HOST, 'host', {
      kata: { version: '3.10.1', sha256: 'f'.repeat(64) },
    }),
  ];
}

function workflow(partial: Record<string, unknown>) {
  return {
    workflow_id: 'b'.repeat(32),
    state: 'running',
    step: '',
    detail: '',
    created_by: 'admin:operator',
    created_at: '2026-10-30T09:12:00+00:00',
    updated_at: NOW,
    ...partial,
  };
}

function intent(
  id: string,
  agentId: string,
  operation: string,
  state: string,
  detail = '',
  created = '2026-10-30T09:20:00+00:00',
) {
  return {
    intent_id: id,
    agent_id: agentId,
    seq: 1,
    operation,
    params: {},
    state,
    detail,
    evidence: {},
    workflow_id: null,
    created_by: 'admin:operator',
    created_at: created,
  };
}

interface Backend {
  overview: Record<string, unknown> & { workflows: unknown[]; maintenance: unknown };
  started: Record<string, unknown>[];
  intents: Record<string, unknown>[];
  confirmed: string[];
}

function backendState(): Backend {
  return {
    overview: {
      maintenance: { active: false, progress: {} },
      agents: agents(),
      intents: [
        intent('i1', CONTROL_ID, 'apply_config', 'succeeded'),
        intent('i2', '1'.repeat(32), 'kata_install', 'succeeded', '', '2026-10-30T08:55:00+00:00'),
        intent('i3', '2'.repeat(32), 'rollback_config', 'pending', '', '2026-10-30T08:40:00+00:00'),
        intent(
          'i4',
          '3'.repeat(32),
          'release_prepare',
          'failed',
          'The release image could not be verified.',
          '2026-10-30T08:10:00+00:00',
        ),
      ],
      workflows: [
        workflow({ kind: 'rollout', state: 'succeeded', step: 'activate_control_plane' }),
        workflow({
          workflow_id: 'c'.repeat(32),
          kind: 'database_connection',
          state: 'succeeded',
          step: 'verify',
          created_at: '2026-10-29T16:00:00+00:00',
        }),
      ],
      contract: {
        operations: {},
        config_keys: contract.config_keys,
        services: contract.services,
      },
    },
    started: [],
    intents: [],
    confirmed: [],
  };
}

const STATUS = {
  settings: {
    saved_revision: 9,
    reporting: 3,
    adopted: 2,
    restart_required: { mcp_allow_plain_http: ['mcp@cp-1'] },
  },
  instances: [
    ['api', 'cp-1', true, []],
    ['mcp', 'cp-1', false, ['mcp_allow_plain_http']],
    ['api', 'cp-2', true, []],
  ].map(([role, host, adopted, restart], index) => ({
    instance_id: `instance-${index}`,
    role,
    machine: host,
    build_sha: RELEASE,
    capabilities: [],
    adopted_settings_revision: adopted ? 9 : 8,
    adopted,
    restart_required: restart,
    current: true,
    stopped: false,
    last_seen_at: NOW,
    root_key_source: 'dedicated',
    root_key_id: 'k1',
  })),
  unidentified_sessions: [
    { application_name: 'psql', user: 'cloudagent_app', client_addr: '', backend_start: null },
  ],
  hosts: [],
};

const HOST_CREDENTIALS = {
  signing_key_id: 'k',
  hosts: [
    ['gpu-1', 'online', true, null, false],
    ['gpu-2', 'offline', false, 'gpu-2 is offline', false],
    [LONG_HOST, 'online', false, null, true],
  ].map(([host, status, current, lag, requested]) => ({
    host,
    status,
    current_key: current,
    key_known: true,
    renews: true,
    renewal_pending: false,
    renewal_requested: requested,
    last_heartbeat_at: NOW,
    lag,
  })),
};

const CREDENTIALS = [
  ['nightly-usage-report', ['admin'], '2026-10-30T02:00:00+00:00', null],
  ['release-pipeline', ['admin', 'deployment'], '2026-10-29T18:30:00+00:00', null],
  ['old-dashboard', ['admin'], null, '2026-10-12T10:00:00+00:00'],
].map(([name, scopes, used, revoked], index) => ({
  credential_id: `credential-${index}`,
  name,
  scopes,
  created_by: 'admin:operator',
  created_at: '2026-09-01T00:00:00+00:00',
  expires_at: '2026-12-01T00:00:00+00:00',
  revoked_at: revoked,
  last_used_at: used,
}));

async function routes(page: Page, backend: Backend, theme: 'light' | 'dark') {
  await page.addInitScript((chosen) => {
    localStorage.setItem('agents.locale', 'en');
    localStorage.setItem('agents.theme', chosen);
  }, theme);
  await page.route('**/api/v1/session/me', (route) =>
    route.fulfill({
      json: {
        id: 'fixture-user',
        username: 'operator',
        agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
      },
    }),
  );
  await page.route('**/api/admin/agent/deployments', (route) =>
    route.fulfill({ json: backend.overview }),
  );
  await page.route('**/api/admin/agent/deployments/workflows', async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    backend.started.push(body);
    await route.fulfill({ json: workflow({ kind: body.kind }) });
  });
  await page.route('**/api/admin/agent/deployments/agents/*/intents', async (route) => {
    backend.intents.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({ json: intent('new', CONTROL_ID, 'apply_config', 'pending') });
  });
  await page.route('**/api/admin/agent/deployments/workflows/*/confirm-far-end', async (route) => {
    backend.confirmed.push((route.request().postDataJSON() as { token: string }).token);
    await route.fulfill({ json: workflow({ kind: 'tunnel_keys' }) });
  });
  await page.route('**/api/admin/agent/deployments/host-credentials', (route) =>
    route.fulfill({ json: HOST_CREDENTIALS }),
  );
  await page.route('**/api/admin/agent/credentials', (route) =>
    route.fulfill({ json: { credentials: CREDENTIALS } }),
  );
  await page.route('**/api/admin/agent/management/status', (route) =>
    route.fulfill({ json: STATUS }),
  );
  await page.route('**/api/admin/agent/management/catalog', (route) =>
    route.fulfill({ json: contract.catalog }),
  );
}

async function open(
  page: Page,
  { theme = 'light', tab = '' }: { theme?: 'light' | 'dark'; tab?: string } = {},
): Promise<Backend> {
  const backend = backendState();
  await routes(page, backend, theme);
  await page.goto(`/admin/deployment${tab ? `?tab=${tab}` : ''}`);
  await expect(page.getByRole('tablist', { name: 'Deployment sections' })).toBeVisible();
  await expect(page.getByText('Partly in use')).toBeAttached();
  return backend;
}

function content(page: Page): Locator {
  return page.getByRole('tablist', { name: 'Deployment sections' }).locator('xpath=..');
}

async function openTab(page: Page, name: string) {
  await page.getByRole('tab', { name }).click();
  await expect(page.getByRole('tab', { name })).toHaveAttribute('aria-selected', 'true');
}

/** Values the administrator chose (an account name may hold an underscore) are allowed. */
const USER_VALUES = ['cloudagent_app'];

async function expectPlainWords(locator: Locator) {
  let text = await locator.innerText();
  for (const value of USER_VALUES) text = text.replaceAll(value, '');
  for (const pattern of PLUMBING) expect(text).not.toMatch(pattern);
}

/** How much more there is to see in whatever scrolls the page inside the app shell. */
async function hiddenHeight(anchor: Locator): Promise<number> {
  return anchor.evaluate((element) => {
    let hidden = 0;
    for (let node = element.parentElement; node; node = node.parentElement) {
      hidden = Math.max(hidden, node.scrollHeight - node.clientHeight);
    }
    return hidden;
  });
}

/** A full-page capture: the window grows until nothing is scrolled out of view. */
async function fullPage(page: Page, name: string, anchor: Locator = content(page)) {
  if (!evidence) return;
  const viewport = page.viewportSize() ?? { width: 1440, height: 900 };
  await page.setViewportSize({
    width: viewport.width,
    height: viewport.height + (await hiddenHeight(anchor)),
  });
  await page.screenshot({ path: `${evidence}/${name}.png` });
  await page.setViewportSize(viewport);
}

async function closeup(locator: Locator, name: string) {
  if (!evidence) return;
  const page = locator.page();
  const viewport = page.viewportSize() ?? { width: 1280, height: 720 };
  // A card taller than the window is captured whole, with the page around it laid out.
  await page.setViewportSize({
    width: viewport.width,
    height: viewport.height + (await hiddenHeight(locator)),
  });
  await locator.scrollIntoViewIfNeeded();
  await locator.screenshot({ path: `${evidence}/${name}.png` });
  await page.setViewportSize(viewport);
}

const SECTIONS = [
  'Overview',
  'Machines',
  'Releases',
  'Keys and credentials',
  'Database',
  'Activity',
];
const SHOT_NAMES: Record<string, string> = {
  Overview: 'overview',
  Machines: 'machines',
  Releases: 'releases',
  'Keys and credentials': 'keys',
  Database: 'database',
  Activity: 'activity',
};

for (const [width, theme] of [
  [1440, 'light'],
  [390, 'light'],
  [1440, 'dark'],
  [390, 'dark'],
] as const) {
  test(`every section reads in plain words and fits ${width}px, ${theme}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await open(page, { theme });
    for (const section of SECTIONS) {
      await openTab(page, section);
      const panel = page.getByRole('tabpanel', { name: section });
      await expect(panel).toBeVisible();
      await expectPlainWords(content(page));
      const fits = await page.evaluate(
        () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
      );
      expect(fits, section).toBe(true);
      const overflowing = await panel.evaluate(
        (element) => element.scrollWidth > element.clientWidth,
      );
      expect(overflowing, section).toBe(false);
      await fullPage(page, `deployment-${SHOT_NAMES[section]}-${width}-${theme}`);
    }
  });
}

test('the overview says what is in use, as reported', async ({ page }) => {
  await open(page);
  const overview = page.getByRole('tabpanel', { name: 'Overview' });
  await expect(overview.getByText('2 of 3 running processes use the saved settings')).toBeVisible();
  await expect(
    overview.getByText('1 saved setting takes effect after a restart of the MCP listener on cp-1.'),
  ).toBeVisible();
  await expect(overview.getByText('3 reporting')).toBeVisible();
  await expect(overview.getByText('1 not reporting')).toBeVisible();
  await expect(overview.getByRole('link', { name: 'Open Keys and credentials' })).toBeVisible();
  // Each application settings category opens its own Settings tab.
  await expect(overview.getByRole('link', { name: 'Gateway', exact: true })).toHaveAttribute(
    'href',
    '/admin/settings?tab=application-Gateway',
  );
  await closeup(
    overview.getByRole('region', { name: 'Status', exact: true }),
    'closeup-overview-status',
  );
});

test('a machine change is reviewed, then applied in plain values', async ({ page }) => {
  const backend = await open(page, { tab: 'machines' });
  const card = page.getByRole('region', { name: 'control-1', exact: true });
  await expect(card.getByLabel('Web app port')).toHaveAttribute('type', 'number');
  await expect(page.getByLabel(/AGENT_|ADMIN_TOKEN/)).toHaveCount(0);
  // A typed form, not a place to paste a connection string or a variable.
  await expect(page.getByPlaceholder(/postgresql:/)).toHaveCount(0);
  await card.getByLabel('Web app port').fill('3010');
  await card.getByLabel('Web app listen address').fill('0.0.0.0');
  await closeup(card, 'closeup-machine-configuration');
  await card.getByRole('button', { name: 'Review changes' }).click();
  const dialog = page.getByRole('dialog', { name: 'Apply to control-1?' });
  await expect(
    dialog.getByText('The API, MCP listener, web app and edge proxy restart.', { exact: false }),
  ).toBeVisible();
  await expectPlainWords(dialog);
  await closeup(dialog, 'closeup-machine-review');
  await dialog.getByRole('button', { name: 'Apply changes' }).click();
  await expect.poll(() => backend.intents.length).toBe(1);
  expect(backend.intents[0]).toMatchObject({
    operation: 'apply_config',
    params: { changes: { AGENT_WEB_PORT: '3010', AGENT_WEB_BIND: '0.0.0.0' } },
  });
  await expect(page.getByRole('status').filter({ hasText: 'Sent to control-1' })).toBeVisible();
});

test('a server offers only what its policy allows, in words', async ({ page }) => {
  await open(page, { tab: 'machines' });
  const card = page.getByRole('region', { name: 'gpu-1', exact: true });
  await card.getByRole('combobox', { name: 'Area' }).click();
  await page.getByRole('option', { name: 'Sandbox isolation and network' }).click();
  await card.getByRole('combobox', { name: 'Sandbox runtime' }).click();
  const options = page.getByRole('listbox');
  await expect(options.getByRole('option')).toHaveText([
    'Keep current value',
    'Kata virtual machines (containerd)',
    'Kata virtual machines',
  ]);
  await closeup(card, 'closeup-server-policy-choices');
  await page.keyboard.press('Escape');
});

test('a credential key rotation is reviewed, started from its row and reports its steps', async ({
  page,
}) => {
  const backend = await open(page, { tab: 'keys' });
  await expect(page.getByText('gpu-2 is offline')).toBeVisible();
  await page.getByRole('button', { name: 'Rotate User credential key' }).click();
  const dialog = page.getByRole('dialog', { name: 'Rotate the user credential key?' });
  await expectPlainWords(dialog);
  await closeup(dialog, 'closeup-rotation-review');
  await dialog.getByRole('button', { name: 'Start rotation' }).click();
  await expect.poll(() => backend.started.length).toBe(1);
  expect(backend.started[0]).toMatchObject({
    kind: 'credential_key',
    params: { key: 'user' },
    confirm: 'credential_key',
  });
  backend.overview.workflows = [
    workflow({
      kind: 'credential_key',
      params: { key: 'user', new_key_id: 'k2' },
      state: 'waiting',
      step: 'reencrypt',
      detail: 're-encrypting environment_values',
      progress: { totals: { rewritten: 128 } },
    }),
  ];
  await page.reload();
  const row = page.locator('[data-workflow="credential_key"]');
  await expect(row.getByText('Re-encrypt stored credentials')).toBeVisible();
  await expect(row.getByText('128 values re-encrypted')).toBeVisible();
  await expect(row).not.toContainText('environment_values');
  await expect(page.getByRole('button', { name: 'Rotate User credential key' })).toBeDisabled();
  await closeup(
    page.getByRole('region', { name: 'Rotations', exact: true }),
    'closeup-rotation-progress',
  );
  await closeup(page.getByRole('region', { name: 'Keys', exact: true }), 'closeup-keys');
  await closeup(
    page.getByRole('region', { name: 'Administrator API credentials', exact: true }),
    'closeup-api-credentials',
  );
});

test('a new recovery token is shown once and never by its variable name', async ({ page }) => {
  const backend = await open(page, { tab: 'keys' });
  await page.getByRole('button', { name: 'Rotate Recovery administrator token' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Start rotation' }).click();
  await expect.poll(() => backend.started.length).toBe(1);
  const panel = page.getByRole('status').filter({ hasText: 'New recovery administrator token' });
  await expect(panel).toBeVisible();
  const token = (await panel.locator('code').innerText()).trim();
  expect(token.length).toBeGreaterThan(30);
  expect(JSON.stringify(backend.started[0])).not.toContain(token);
  await expect(panel).not.toContainText('ADMIN_TOKEN');
  await closeup(panel, 'closeup-one-time-token');
  await panel.getByRole('button', { name: 'I have stored it' }).click();
  await expect(page.getByText(token)).toHaveCount(0);
});

test('a far end nothing can edit is shown and confirmed by an administrator', async ({ page }) => {
  const backend = await open(page, { tab: 'keys' });
  const key = `ssh-ed25519 ${'A'.repeat(43)}`;
  backend.overview.workflows = [
    workflow({
      kind: 'tunnel_keys',
      state: 'waiting',
      step: 'rolling',
      detail: 'gpu-1: add these keys on the far end, then confirm: gateway',
      progress: {
        phases: { 'gpu-1': 'authorize', 'gpu-2': 'generate', [LONG_HOST]: 'done' },
        attention: { token: 'gpu-1:add', host: 'gpu-1', action: 'add', tunnels: { gateway: key } },
      },
    }),
  ];
  await page.reload();
  await expect(page.getByText(key)).toBeVisible();
  await expect(page.getByText('Gateway tunnel')).toBeVisible();
  await expectPlainWords(page.getByRole('region', { name: 'Rotations', exact: true }));
  await closeup(
    page.getByRole('region', { name: 'Rotations', exact: true }),
    'closeup-tunnel-far-end',
  );
  await page.getByRole('button', { name: 'I added these keys' }).click();
  await expect.poll(() => backend.confirmed).toEqual(['gpu-1:add']);
});

test('a database move is reviewed, sealed in this browser and shows maintenance', async ({
  page,
}) => {
  const backend = await open(page, { tab: 'database' });
  await expect(page.getByLabel('Database host')).toHaveAttribute(
    'placeholder',
    'Current: db.internal',
  );
  await page.getByRole('radio', { name: /Move the data to an empty database/ }).check();
  await page.getByLabel('Database host').fill('db2.internal');
  await page.getByLabel('Database name').fill('agent');
  await page.getByLabel('Database user').fill('agent');
  await page.getByLabel('Database password').fill('a-long-target-password');
  await closeup(
    page.getByRole('region', { name: 'Change the database', exact: true }),
    'closeup-database-form',
  );
  await page.getByRole('button', { name: 'Move to this database' }).click();
  const dialog = page.getByRole('dialog', { name: 'Move every record to this database?' });
  await expect(dialog.getByText('db2.internal:5432')).toBeVisible();
  await expectPlainWords(dialog);
  await closeup(dialog, 'closeup-database-move-review');
  await dialog.getByRole('button', { name: 'Start move' }).click();
  await expect.poll(() => backend.started.length).toBe(1);
  const request = backend.started[0];
  expect(request.kind).toBe('database_migration');
  expect(JSON.stringify(request)).not.toContain('a-long-target-password');
  expect(JSON.stringify(request)).not.toContain('db2.internal');
  await expect(page.getByLabel('Database password')).toHaveValue('');

  backend.overview.maintenance = {
    active: true,
    since: '2026-10-30T10:00:00+00:00',
    workflow_id: 'b'.repeat(32),
    progress: {
      [CONTROL_ID]: {
        operations: [{ operation: 'database_migrate', phase: 'fence', state: 'running' }],
        received_at: '2026-10-30T10:01:00+00:00',
      },
    },
  };
  backend.overview.workflows = [
    workflow({ kind: 'database_migration', state: 'waiting', step: 'migrate' }),
  ];
  await page.route('**/api/v1/agent/site-config', (route) =>
    route.fulfill({
      json: { branding: {}, features: {}, maintenance: { active: true } },
    }),
  );
  await page.reload();
  await expect(page.getByText('Changes are paused while the database moves.')).toBeVisible();
  await expect(page.getByText(/Last step reported: Old database fenced/)).toBeVisible();
  await expect(page.getByText(/Scheduled maintenance\. The database is being moved/)).toBeVisible();
  await page.getByRole('radio', { name: /Move the data to an empty database/ }).check();
  await expect(page.getByRole('button', { name: 'Move to this database' })).toBeDisabled();
  await expectPlainWords(content(page));
  await fullPage(page, 'deployment-database-move-in-progress-1440-light');
  await closeup(
    page.getByRole('status').filter({ hasText: 'Scheduled maintenance' }),
    'closeup-maintenance-banner',
  );
});

test('failures read as what happened, not as codes', async ({ page }) => {
  const backend = await open(page, { tab: 'database' });
  backend.overview.workflows = [
    workflow({
      kind: 'database_connection',
      state: 'failed',
      step: 'probe',
      detail: 'probe_token mismatch on agent_database_probes',
    }),
    workflow({
      workflow_id: 'd'.repeat(32),
      kind: 'root_key',
      state: 'failed',
      step: 'reencrypt',
      detail: 'Two saved secrets could not be read with either key.',
    }),
  ];
  await page.reload();
  const changes = page.getByRole('region', { name: 'Database changes', exact: true });
  await expect(changes.getByText('Stopped before finishing.')).toBeVisible();
  await expect(changes).not.toContainText('probe_token');
  await closeup(changes, 'closeup-database-failure');
  await openTab(page, 'Keys and credentials');
  await expect(
    page.getByText('Two saved secrets could not be read with either key.'),
  ).toBeVisible();
  await expect(page.getByRole('button', { name: 'Continue from this step' })).toBeVisible();
  await closeup(
    page.getByRole('region', { name: 'Rotations', exact: true }),
    'closeup-rotation-failure',
  );

  await page.route('**/api/admin/agent/deployments', (route) =>
    route.fulfill({ status: 503, json: { detail: 'upstream_unavailable' } }),
  );
  await page.reload();
  const alert = page.getByRole('alert').filter({ hasText: 'not answering' });
  await expect(alert).toContainText('not answering right now');
  await expect(alert).not.toContainText('upstream_unavailable');
  await closeup(alert, 'closeup-unreachable');
});

test('the settings page, at the same size, for comparison', async ({ page }) => {
  await routes(page, backendState(), 'light');
  await page.route('**/api/admin/agent/settings', (route) =>
    route.fulfill({
      json: {
        revision: 9,
        definitions: [
          ['gateway_url', 'Gateway URL', 'string', 'Where model requests are sent.'],
          ['gateway_timeout_s', 'Request timeout', 'integer', 'Seconds before a request fails.'],
          ['gateway_api_key', 'Gateway API key', 'secret', 'Used for every model request.'],
          ['mcp_allow_plain_http', 'Allow plain HTTP MCP servers', 'boolean', 'Only for testing.'],
        ].map(([key, label, kind, description]) => ({
          key,
          label,
          category: 'Gateway',
          kind: kind === 'secret' ? 'string' : kind,
          default: kind === 'boolean' ? false : kind === 'integer' ? 60 : '',
          secret: kind === 'secret',
          description,
          apply_mode: key === 'mcp_allow_plain_http' ? 'restart' : 'immediate',
        })),
        values: {
          gateway_url: 'https://gateway.example.org',
          gateway_timeout_s: 120,
          mcp_allow_plain_http: false,
        },
        secret_configured: { gateway_api_key: true },
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
  await page.goto('/admin/settings');
  await expect(page.getByText('Gateway URL')).toBeVisible();
  await expect(
    page.getByText('Saved. 2 of 3 running processes use these settings so far.'),
  ).toBeVisible();
  await page.setViewportSize({ width: 1440, height: 900 });
  await fullPage(
    page,
    'settings-reference-1440-light',
    page.getByRole('tablist', { name: 'Settings sections' }),
  );
});

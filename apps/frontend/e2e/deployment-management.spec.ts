import { expect, test, type Page } from '@playwright/test';

// The production Next app in a real browser, against a fixture of the admin API
// that holds what the real routes return. Sealing, the forms and every state
// shown run for real; what the control plane does with a request has its own
// tests over real databases.

const evidence = process.env.CLOUD_AGENT_E2E_EVIDENCE;

const AGENT_ID = 'a'.repeat(32);
// A real P-256 public key, so the browser really seals to it.
const PUBLIC_KEY =
  'BORQnMqmnLhuHCAN7oWYoxg6QRUm4DbG8x1Yh5MxOq5cwPiWh0xVMIlZGztSbAq+prytqBZcWO/LI6c+VB9FuJo=';

function agent() {
  return {
    agent_id: AGENT_ID,
    name: 'control-1',
    role: 'control-plane',
    machine: 'cp-1',
    public_key: PUBLIC_KEY,
    enrolled_at: '2026-10-30T00:00:00+00:00',
    last_report_at: '2026-10-30T00:00:00+00:00',
    revoked: false,
    online: true,
    observed: {
      deployed_commit: 'c'.repeat(40),
      config_rev: 2,
      config_versions: [0, 1, 2],
      managed_keys: [],
      unmanaged_keys: [],
      legacy_names_present: [],
      releases: {},
      services: { 'control-plane': true, 'mcp-relay': true },
      database: { host: 'db.internal', port: 5432, database: 'cloudagent', user: 'ca', query: '' },
      policy: { allowed: {}, kata: [], has_kata_helper: false, has_database_tools: true },
      secret_ids: {},
    },
    local_operations: [],
  };
}

interface Backend {
  overview: Record<string, unknown>;
  started: Record<string, unknown>[];
  confirmed: string[];
}

function workflow(partial: Record<string, unknown>) {
  return {
    workflow_id: 'b'.repeat(32),
    state: 'running',
    step: '',
    detail: '',
    created_by: 'admin:operator',
    created_at: '2026-10-30T00:00:00+00:00',
    updated_at: '2026-10-30T00:00:00+00:00',
    ...partial,
  };
}

async function open(page: Page): Promise<Backend> {
  const backend: Backend = {
    overview: {
      maintenance: { active: false, progress: {} },
      agents: [agent()],
      intents: [],
      workflows: [],
      contract: {
        operations: {},
        config_keys: {
          'control-plane': {
            AGENT_WEB_PORT: {
              kind: 'port',
              item: 'deployment.control_plane_listeners',
              services: ['web'],
              choices: [],
              boundary: '',
            },
            AGENT_WEB_BIND: {
              kind: 'bind',
              item: 'deployment.control_plane_listeners',
              services: ['web'],
              choices: [],
              boundary: '',
            },
            AGENT_OPERATION_PROFILING_MODE: {
              kind: 'choice',
              item: 'deployment.profiling',
              services: ['control-plane', 'mcp-relay'],
              choices: ['disabled', 'environment', 'admin'],
              boundary: 'policy',
            },
            ADMIN_TOKEN: {
              kind: 'secret',
              item: 'security.admin_token',
              services: ['control-plane'],
              choices: [],
              boundary: '',
            },
          },
          host: {},
        },
        services: {},
      },
    },
    started: [],
    confirmed: [],
  };
  await page.addInitScript(() => {
    localStorage.setItem('agents.locale', 'en');
    localStorage.setItem('agents.theme', 'light');
  });
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
  await page.route('**/api/admin/agent/deployments/workflows/*/confirm-far-end', async (route) => {
    backend.confirmed.push((route.request().postDataJSON() as { token: string }).token);
    await route.fulfill({ json: workflow({ kind: 'tunnel_keys' }) });
  });
  await page.route('**/api/admin/agent/deployments/host-credentials', (route) =>
    route.fulfill({
      json: {
        signing_key_id: 'k',
        hosts: [
          {
            host: 'gpu-1',
            status: 'online',
            current_key: true,
            key_known: true,
            renews: true,
            renewal_pending: false,
            renewal_requested: false,
            last_heartbeat_at: null,
            lag: null,
          },
          {
            host: 'gpu-2',
            status: 'offline',
            current_key: false,
            key_known: false,
            renews: false,
            renewal_pending: false,
            renewal_requested: false,
            last_heartbeat_at: null,
            lag: 'gpu-2 is offline',
          },
        ],
      },
    }),
  );
  await page.route('**/api/admin/agent/credentials', (route) =>
    route.fulfill({ json: { credentials: [] } }),
  );
  await page.route('**/api/admin/agent/management/status', (route) =>
    route.fulfill({
      json: {
        settings: { saved_revision: 9, reporting: 2, adopted: 1, restart_required: {} },
        instances: [],
        unidentified_sessions: [],
        hosts: [],
      },
    }),
  );
  await page.route('**/api/admin/agent/management/catalog', (route) =>
    route.fulfill({
      json: {
        methods: ['workflow'],
        apply_modes: [],
        items: [
          {
            id: 'security.credential_keys',
            label: 'Credential encryption keys',
            group: 'Security',
            method: 'workflow',
            sources: [{ kind: 'settings', reader: 'control plane', meaning: 'saved keys' }],
            owner: 'workflow:credential_key',
            description: 'Every stored value is re-encrypted before the previous key is dropped.',
            apply_mode: 'next_operation',
            restart_scope: [],
            role: null,
            env_names: [],
            setting_keys: [],
            bounded: false,
          },
        ],
      },
    }),
  );
  await page.goto('/admin/deployment');
  await expect(page.getByRole('heading', { name: 'Keys and credentials' })).toBeVisible();
  return backend;
}

/** The page scrolls inside the shell, so each section is captured on its own. */
async function shot(page: Page, name: string, heading: string) {
  if (!evidence) return;
  const section = page.locator('section', { has: page.getByRole('heading', { name: heading }) });
  await section.first().screenshot({ path: `${evidence}/${name}.png` });
}

for (const width of [1440, 390]) {
  test(`keys, database and configuration fit ${width}px with nothing raw`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await open(page);
    await expect(page.getByText('Now connected to')).toBeVisible();
    await expect(page.getByText('gpu-2 is offline')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Keys and credentials' })).toBeVisible();
    // A typed form, not a place to paste a connection string or a variable.
    await expect(page.getByPlaceholder(/postgresql:/)).toHaveCount(0);
    await expect(page.getByLabel(/\(ADMIN_TOKEN\)/)).toHaveCount(0);
    await expect(page.getByLabel('Port (AGENT_WEB_PORT)')).toHaveAttribute('type', 'number');
    await shot(page, `keys-${width}`, 'Keys and credentials');
    await shot(page, `database-${width}`, 'Database');
    await shot(page, `configuration-map-${width}`, 'Where configuration lives');
    await shot(page, `machines-${width}`, 'Machines');
    const fits = await page.evaluate(
      () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    );
    expect(fits).toBe(true);
  });
}

test('a credential key rotation starts from its row and reports its steps', async ({ page }) => {
  const backend = await open(page);
  page.on('dialog', (dialog) => dialog.accept());
  await page.getByRole('button', { name: 'Rotate User credential key' }).click();
  await expect.poll(() => backend.started.length).toBe(1);
  expect(backend.started[0]).toMatchObject({
    kind: 'credential_key',
    params: { key: 'user' },
    confirm: 'credential_key',
  });
  backend.overview.workflows = [
    workflow({
      kind: 'credential_key',
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
  await expect(page.getByRole('button', { name: 'Rotate User credential key' })).toBeDisabled();
  await shot(page, 'credential-key-rotation', 'Operations');
});

test('a far end nothing can edit is shown and confirmed by an administrator', async ({ page }) => {
  const backend = await open(page);
  const key = `ssh-ed25519 ${'A'.repeat(43)}`;
  backend.overview.workflows = [
    workflow({
      kind: 'tunnel_keys',
      state: 'waiting',
      step: 'rolling',
      detail: 'gpu-1: add these keys on the far end, then confirm: gateway',
      progress: {
        phases: { 'gpu-1': 'authorize', 'gpu-2': 'generate' },
        attention: { token: 'gpu-1:add', host: 'gpu-1', action: 'add', tunnels: { gateway: key } },
      },
    }),
  ];
  await page.reload();
  await expect(page.getByText(key)).toBeVisible();
  await shot(page, 'tunnel-far-end', 'Operations');
  await page.getByRole('button', { name: 'I added these keys' }).click();
  await expect.poll(() => backend.confirmed).toEqual(['gpu-1:add']);
});

test('a database move seals the target in this browser and shows maintenance', async ({ page }) => {
  const backend = await open(page);
  page.on('dialog', (dialog) => dialog.accept());
  await page.getByLabel('Move the data to an empty database').check();
  await page.getByLabel('Database host').fill('db2.internal');
  await page.getByLabel('Database name').fill('agent');
  await page.getByLabel('Database user').fill('agent');
  await page.getByLabel('Database password').fill('a-long-target-password');
  await page.getByRole('button', { name: 'Move to this database' }).click();
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
      [AGENT_ID]: {
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
  await expect(page.getByText('Changes are refused while the database moves.')).toBeVisible();
  await expect(page.getByText(/Last step reported: Old database fenced/)).toBeVisible();
  await expect(page.getByText(/Maintenance: the database is moving/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Move to this database' })).toBeDisabled();
  await shot(page, 'database-move-maintenance', 'Operations');
  await page
    .getByRole('status')
    .first()
    .screenshot({ path: evidence ? `${evidence}/maintenance-banner.png` : undefined });
});

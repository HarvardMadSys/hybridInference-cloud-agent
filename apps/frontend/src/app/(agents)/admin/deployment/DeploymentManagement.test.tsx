// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as api from '@/lib/api/adminManagement';
import { DeploymentManagement } from './DeploymentManagement';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, '', '/');
});

/**
 * What must never be shown as a label or an instruction: configuration names,
 * snake_case codes, commands, files and process ids. The API speaks in them;
 * the page translates.
 */
const PLUMBING = [
  /\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b/,
  /\b[a-z]+_[a-z0-9_]+\b/,
  /python -m|deploy\.agent|unfence|--root|--operation-id/,
  /\.env\b|AGENTS\.md|authorized_keys|policy\.json/,
  /\b(?:api|mcp)@/,
];

function expectNoPlumbing(text: string | null | undefined) {
  for (const pattern of PLUMBING) expect(text ?? '').not.toMatch(pattern);
}

const overview = (agents: api.DeploymentAgent[]): api.DeploymentOverview => ({
  agents,
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
        COMPOSE_PROFILES: {
          kind: 'choice',
          item: 'deployment.optional_services',
          services: ['pgadmin', 'edge'],
          choices: ['', 'edge', 'pgadmin', 'edge,pgadmin'],
          boundary: '',
        },
        AGENT_WEB_SECRET: {
          kind: 'secret',
          item: 'web.oauth',
          services: ['web'],
          choices: [],
          boundary: '',
        },
        ADMIN_TOKEN: {
          kind: 'secret',
          item: 'security.admin_token',
          services: ['control-plane'],
          choices: [],
          boundary: '',
        },
        AGENT_DATABASE_URL: {
          kind: 'secret',
          item: 'database.connection',
          services: ['control-plane'],
          choices: [],
          boundary: '',
        },
      },
      host: {},
    },
    services: {},
  },
});

const status: api.ManagementStatus = {
  settings: {
    saved_revision: 4,
    reporting: 2,
    adopted: 1,
    restart_required: { mcp_allow_plain_http: ['mcp@cp-1'] },
  },
  instances: [
    {
      instance_id: 'i1',
      role: 'mcp',
      machine: 'cp-1',
      build_sha: 'd'.repeat(40),
      capabilities: [],
      adopted_settings_revision: 3,
      adopted: false,
      restart_required: ['mcp_allow_plain_http'],
      current: true,
      stopped: false,
      last_seen_at: '',
      root_key_source: null,
      root_key_id: null,
    },
  ],
  unidentified_sessions: [
    { application_name: 'psql', user: 'u', client_addr: '', backend_start: null },
  ],
  hosts: [],
};

const catalogItem = (partial: Partial<api.CatalogItem>): api.CatalogItem => ({
  id: 'x',
  label: 'x',
  group: 'Deployment',
  method: 'apply',
  sources: [{ kind: 'deployment_store', reader: 'Compose', meaning: 'control-plane .env' }],
  owner: 'deployment:apply_config',
  description: 'See AGENTS.md section 7 and the host .env.',
  apply_mode: 'restart',
  restart_scope: [],
  role: 'control-plane',
  env_names: ['AGENT_WEB_PORT'],
  setting_keys: [],
  bounded: false,
  ...partial,
});

const catalog: api.ManagementCatalog = {
  methods: ['apply', 'workflow', 'observe'],
  apply_modes: [],
  items: [
    catalogItem({ id: 'deployment.control_plane_listeners', label: 'Control-plane listeners' }),
    catalogItem({ id: 'web.oauth', label: 'Sign-in secret', description: 'Used for sign-in.' }),
    catalogItem({
      id: 'security.admin_token',
      label: 'Deployment ADMIN_TOKEN',
      method: 'workflow',
    }),
    catalogItem({
      id: 'hosts.tunnel_keys',
      label: 'Tunnel key item',
      group: 'Security',
      method: 'workflow',
      owner: 'workflow:tunnel_keys',
    }),
    catalogItem({
      id: 'deployment.legacy_names',
      label: 'Retired variable names',
      method: 'observe',
      owner: 'deployment:observe',
    }),
    catalogItem({ id: 'future.item', label: 'AGENT_FUTURE_THING', description: 'uses foo_bar' }),
  ],
};

function mock(
  agents: api.DeploymentAgent[],
  extra: Partial<api.DeploymentOverview> = {},
  withCatalog: api.ManagementCatalog | null = null,
) {
  vi.spyOn(api, 'getDeployments').mockResolvedValue({ ...overview(agents), ...extra });
  vi.spyOn(api, 'getManagementStatus').mockResolvedValue(status);
  vi.spyOn(api, 'listCredentials').mockResolvedValue({ credentials: [] });
  if (withCatalog) vi.spyOn(api, 'getManagementCatalog').mockResolvedValue(withCatalog);
  else vi.spyOn(api, 'getManagementCatalog').mockRejectedValue(new Error('not there'));
  vi.spyOn(api, 'getHostCredentials').mockResolvedValue({
    signing_key_id: 'k',
    hosts: [host],
  });
}

const host: api.HostCredentialView = {
  host: 'gpu-1',
  status: 'online',
  current_key: false,
  key_known: true,
  renews: true,
  renewal_pending: false,
  renewal_requested: false,
  last_heartbeat_at: null,
  lag: 'gpu-1 has not received a renewal yet',
};

const workflow = (partial: Partial<api.DeploymentWorkflow>): api.DeploymentWorkflow => ({
  workflow_id: 'b'.repeat(32),
  kind: 'credential_key',
  state: 'waiting',
  step: 'reencrypt',
  detail: '',
  created_by: 'admin:a',
  created_at: '',
  updated_at: '',
  ...partial,
});

const agent: api.DeploymentAgent = {
  agent_id: 'a'.repeat(32),
  name: 'cp-1',
  role: 'control-plane',
  machine: 'm1',
  public_key:
    'BORQnMqmnLhuHCAN7oWYoxg6QRUm4DbG8x1Yh5MxOq5cwPiWh0xVMIlZGztSbAq+prytqBZcWO/LI6c+VB9FuJo=',
  enrolled_at: '',
  last_report_at: '',
  revoked: false,
  online: true,
  observed: { deployed_commit: 'c'.repeat(40), config_rev: 2, config_versions: [0, 1, 2] },
  local_operations: [],
};

function openTab(name: string) {
  fireEvent.click(screen.getByRole('tab', { name }));
}

function choose(combobox: string, option: string) {
  fireEvent.click(screen.getByRole('combobox', { name: combobox }));
  fireEvent.click(screen.getByRole('option', { name: option }));
}

function confirmDialog(button: string) {
  const dialog = screen.getByRole('dialog');
  expectNoPlumbing(dialog.textContent);
  fireEvent.click(within(dialog).getByRole('button', { name: button }));
}

describe('deployment management', () => {
  it('reports adoption truthfully and offers enrollment with no machines', async () => {
    mock([]);
    render(<DeploymentManagement />);
    expect(await screen.findByText(/1 of 2 running processes use the saved settings/)).toBeTruthy();
    expect(screen.getByText(/1 connection does not report which settings it uses/)).toBeTruthy();
    expect(
      screen.getByText(/1 saved setting takes effect after a restart of the MCP listener on cp-1/),
    ).toBeTruthy();
    expect(screen.getByText('MCP listener on cp-1')).toBeTruthy();
    openTab('Machines');
    expect(screen.getByText(/No machine has a deployment agent yet/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Issue enrollment token' })).toBeTruthy();
  });

  it('issues an enrollment token once, without telling anyone to run a command', async () => {
    mock([]);
    vi.spyOn(api, 'createEnrollToken').mockResolvedValue({
      token: 'cde_one-time-token',
      expires_in_seconds: 3600,
    });
    render(<DeploymentManagement />);
    await screen.findByText(/running processes/);
    openTab('Machines');
    choose('Machine role', 'Control plane');
    fireEvent.click(screen.getByRole('button', { name: 'Issue enrollment token' }));
    expect(await screen.findByText('cde_one-time-token')).toBeTruthy();
    expect(screen.getByText('Enrollment token for a control plane machine')).toBeTruthy();
    expect(api.createEnrollToken).toHaveBeenCalledWith('control-plane');
    const enroll = screen.getByRole('region', { name: 'Enroll a machine' });
    expectNoPlumbing(enroll.textContent);
    fireEvent.click(screen.getByRole('button', { name: 'I have stored it' }));
    expect(screen.queryByText('cde_one-time-token')).toBeNull();
  });

  it('configures a machine by item, plain values in the clear and a secret only sealed', async () => {
    mock([agent], {}, catalog);
    const intent = vi.spyOn(api, 'createIntent').mockResolvedValue({} as api.DeploymentIntent);
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    openTab('Machines');
    const port = await screen.findByLabelText('Web app port');
    expect(port.getAttribute('type')).toBe('number');
    fireEvent.change(port, { target: { value: '3010' } });
    fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    const review = screen.getByRole('dialog', { name: 'Apply to cp-1?' });
    expect(within(review).getByText('3010')).toBeTruthy();
    expect(within(review).getByText(/The web app restarts\./)).toBeTruthy();
    confirmDialog('Apply changes');
    await waitFor(() => expect(intent).toHaveBeenCalledTimes(1));
    expect(intent.mock.calls[0][1]).toEqual({
      operation: 'apply_config',
      params: { changes: { AGENT_WEB_PORT: '3010' } },
    });

    choose('Area', 'Sign-in secret');
    fireEvent.change(screen.getByLabelText('Web secret'), {
      target: { value: 'a-secret-value' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    expect(within(screen.getByRole('dialog')).getByText('New value · hidden')).toBeTruthy();
    expect(screen.getByRole('dialog').textContent).not.toContain('a-secret-value');
    confirmDialog('Apply changes');
    await waitFor(() => expect(intent).toHaveBeenCalledTimes(2));
    const sealedRequest = intent.mock.calls[1][1];
    expect(JSON.stringify(sealedRequest)).not.toContain('a-secret-value');
    expect(sealedRequest.sealed?.AGENT_WEB_SECRET?.v).toBe(1);
  });

  it('can choose an empty choice, distinct from keeping the current value', async () => {
    mock([agent], {}, catalog);
    const intent = vi.spyOn(api, 'createIntent').mockResolvedValue({} as api.DeploymentIntent);
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    openTab('Machines');
    choose('Area', 'Bundled services');
    choose('Bundled services', 'None');
    fireEvent.click(screen.getByRole('button', { name: 'Review changes' }));
    confirmDialog('Apply changes');
    await waitFor(() => expect(intent).toHaveBeenCalledTimes(1));
    expect(intent.mock.calls[0][1].params).toEqual({ changes: { COMPOSE_PROFILES: '' } });
  });

  it('keeps keys with a guided operation out of direct configuration', async () => {
    mock([agent], {}, catalog);
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    openTab('Machines');
    fireEvent.click(screen.getByRole('combobox', { name: 'Area' }));
    const options = screen.getAllByRole('option').map((option) => option.textContent);
    expect(options).toEqual(['Addresses and ports', 'Bundled services', 'Sign-in secret']);
    expectNoPlumbing(screen.getByRole('listbox').textContent);
  });

  it('starts each key rotation in its own words, with nothing typed', async () => {
    mock([agent]);
    const start = vi.spyOn(api, 'startWorkflow').mockResolvedValue(workflow({}));
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    openTab('Keys and credentials');
    await screen.findByText('gpu-1');
    fireEvent.click(screen.getByRole('button', { name: 'Rotate User credential key' }));
    confirmDialog('Start rotation');
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    expect(start.mock.calls[0][0]).toMatchObject({
      kind: 'credential_key',
      params: { key: 'user' },
    });
    expect(start.mock.calls[0][0].secret?.length).toBeGreaterThan(30);
    fireEvent.click(screen.getByRole('button', { name: 'Rotate Tunnel keys' }));
    confirmDialog('Start rotation');
    await waitFor(() => expect(start).toHaveBeenCalledTimes(2));
    expect(start.mock.calls[1][0]).toEqual({ kind: 'tunnel_keys' });
  });

  it('does nothing when a rotation is cancelled in its review', async () => {
    mock([agent]);
    const start = vi.spyOn(api, 'startWorkflow').mockResolvedValue(workflow({}));
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    openTab('Keys and credentials');
    fireEvent.click(screen.getByRole('button', { name: 'Rotate Settings root key' }));
    fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(start).not.toHaveBeenCalled();
  });

  it('shows a new recovery token once, sealed for the machines and never named by its variable', async () => {
    mock([agent]);
    const start = vi.spyOn(api, 'startWorkflow').mockResolvedValue(workflow({}));
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    openTab('Keys and credentials');
    fireEvent.click(screen.getByRole('button', { name: 'Rotate Recovery administrator token' }));
    confirmDialog('Start rotation');
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    const request = start.mock.calls[0][0];
    expect(request.kind).toBe('admin_token');
    expect(request.sealed_by_agent?.[agent.agent_id].ADMIN_TOKEN.v).toBe(1);
    const shown = await screen.findByText('New recovery administrator token');
    const panel = shown.closest('[role="status"]') as HTMLElement;
    const token = within(panel).getByText(/^[A-Za-z0-9_-]{30,}$/).textContent ?? '';
    expect(JSON.stringify(request)).not.toContain(token);
    // The token itself may hold an underscore; everything around it is words.
    expectNoPlumbing(document.body.textContent?.replace(token, ''));
  });

  it('shows where a rotation stands and refuses a second one', async () => {
    mock([agent], {
      workflows: [
        workflow({
          params: { key: 'user', new_key_id: 'k2' },
          progress: { totals: { rewritten: 12, unreadable: 1 } },
          detail: 're-encrypting mcp_access',
        }),
      ],
    });
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    openTab('Keys and credentials');
    const rotations = screen.getByRole('region', { name: 'Rotations' });
    expect(within(rotations).getByText('User credential key')).toBeTruthy();
    const current = within(rotations).getByText('Re-encrypt stored credentials').closest('li');
    expect(current?.getAttribute('aria-current')).toBe('step');
    expect(within(rotations).getByText(/12 values re-encrypted, 1 unreadable/)).toBeTruthy();
    // A machine's own wording with a table name stays out of the page.
    expect(rotations.textContent).not.toContain('mcp_access');
    expect(
      (screen.getByRole('button', { name: 'Rotate User credential key' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
    // Only the user key rotates; the compute key waits rather than claiming to rotate too.
    const keys = screen.getByRole('region', { name: 'Keys' });
    expect(within(keys).getAllByText('Rotating')).toHaveLength(1);
    expect(
      within(keys).getByText('Available when the other credential key finishes.'),
    ).toBeTruthy();
    expect(
      (screen.getByRole('button', { name: 'Rotate Compute credential key' }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it('asks an administrator to change a far end and sends the confirmation', async () => {
    const key = `ssh-ed25519 ${'A'.repeat(43)}`;
    mock([agent], {
      workflows: [
        workflow({
          kind: 'tunnel_keys',
          step: 'rolling',
          progress: {
            phases: { 'gpu-1': 'authorize' },
            attention: {
              token: 'gpu-1:add',
              host: 'gpu-1',
              action: 'add',
              tunnels: { gateway: key },
            },
          },
        }),
      ],
    });
    const confirm = vi
      .spyOn(api, 'confirmFarEnd')
      .mockResolvedValue(workflow({ kind: 'tunnel_keys' }));
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    openTab('Keys and credentials');
    expect(screen.getByText(key)).toBeTruthy();
    expect(screen.getByText('Gateway tunnel')).toBeTruthy();
    expect(screen.getByText(/waiting for the far end to accept them/)).toBeTruthy();
    expectNoPlumbing(screen.getByRole('region', { name: 'Rotations' }).textContent);
    fireEvent.click(screen.getByRole('button', { name: 'I added these keys' }));
    await waitFor(() => expect(confirm).toHaveBeenCalledWith('b'.repeat(32), 'gpu-1:add'));
  });

  it('renews one server, and sends no password in the clear for a database move', async () => {
    const withDatabase: api.DeploymentAgent = {
      ...agent,
      observed: {
        ...agent.observed,
        database: { host: 'db', port: 5432, database: 'cloudagent', user: 'ca', query: '' },
        policy: { allowed: {}, kata: [], has_kata_helper: false, has_database_tools: true },
      },
    };
    mock([withDatabase]);
    const renew = vi.spyOn(api, 'renewHostCredential').mockResolvedValue({ requested: true });
    const start = vi.spyOn(api, 'startWorkflow').mockResolvedValue(workflow({}));
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    openTab('Keys and credentials');
    fireEvent.click(await screen.findByRole('button', { name: 'Renew gpu-1' }));
    confirmDialog('Renew');
    await waitFor(() => expect(renew).toHaveBeenCalledWith('gpu-1'));

    openTab('Database');
    expect(screen.getByText('cloudagent')).toBeTruthy();
    fireEvent.click(screen.getByRole('radio', { name: /Move the data to an empty database/ }));
    fireEvent.change(screen.getByLabelText('Database host'), { target: { value: 'db2.example' } });
    fireEvent.change(screen.getByLabelText('Database name'), { target: { value: 'agent' } });
    fireEvent.change(screen.getByLabelText('Database user'), { target: { value: 'agent' } });
    fireEvent.change(screen.getByLabelText('Database password'), {
      target: { value: 'p@ss word' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Move to this database' }));
    expect(within(screen.getByRole('dialog')).getByText('db2.example:5432')).toBeTruthy();
    confirmDialog('Start move');
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    const request = start.mock.calls[0][0];
    expect(request.kind).toBe('database_migration');
    expect(JSON.stringify(request)).not.toContain('ss word');
    expect(request.sealed_by_agent?.[agent.agent_id].AGENT_DATABASE_URL.v).toBe(1);
  });

  it('keeps a connection change apart from a move, and points to the guide instead of commands', async () => {
    mock([
      {
        ...agent,
        observed: {
          ...agent.observed,
          database: { host: 'db', port: 5432, database: 'cloudagent', user: 'ca', query: '' },
          policy: { allowed: {}, kata: [], has_kata_helper: false, has_database_tools: false },
        },
      },
    ]);
    const start = vi.spyOn(api, 'startWorkflow').mockResolvedValue(workflow({}));
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    openTab('Database');
    const panel = screen.getByRole('tabpanel', { name: 'Database' });
    expect(screen.getByLabelText('Database host').getAttribute('placeholder')).toBe('Current: db');
    fireEvent.change(screen.getByLabelText('Database password'), { target: { value: 'pw' } });
    fireEvent.click(screen.getByRole('button', { name: 'Change connection' }));
    expect(screen.getByRole('dialog', { name: /same database/ })).toBeTruthy();
    confirmDialog('Switch connection');
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    expect(start.mock.calls[0][0].kind).toBe('database_connection');

    fireEvent.click(screen.getByRole('radio', { name: /Move the data/ }));
    expect(screen.getByText(/PostgreSQL client tools/)).toBeTruthy();
    expect(
      (screen.getByRole('button', { name: 'Move to this database' }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(panel.textContent).toContain('the operations guide, under “Moving the database”');
    expectNoPlumbing(panel.textContent);
  });

  it('offers to continue a stopped rotation, and only a rotation', async () => {
    mock([agent], {
      workflows: [
        workflow({ state: 'failed', detail: 'stopped here' }),
        workflow({
          workflow_id: 'c'.repeat(32),
          kind: 'database_connection',
          state: 'failed',
          step: 'probe',
        }),
      ],
    });
    const retry = vi.spyOn(api, 'retryWorkflow').mockResolvedValue(workflow({}));
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    openTab('Database');
    expect(screen.getByText('Stopped before finishing.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Continue from this step' })).toBeNull();
    openTab('Keys and credentials');
    expect(screen.getByText('stopped here')).toBeTruthy();
    const buttons = screen.getAllByRole('button', { name: 'Continue from this step' });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    await waitFor(() => expect(retry).toHaveBeenCalledWith('b'.repeat(32)));
  });

  it('names the outage during a move and what it has heard', async () => {
    mock([agent], {
      maintenance: {
        active: true,
        since: '2026-10-30T10:00:00+00:00',
        progress: { a: { operations: [{ phase: 'copy' }], received_at: '' } },
      },
    });
    render(<DeploymentManagement />);
    expect(await screen.findByText(/Changes are paused while the database moves/)).toBeTruthy();
    expect(screen.getByText(/Last step reported: Data copied/)).toBeTruthy();
    expect(screen.getByText(/may stop\s+answering for a minute or two/)).toBeTruthy();
  });

  it('maps every item to where it changes, and works without a catalog', async () => {
    mock([agent], {}, catalog);
    render(<DeploymentManagement />);
    const map = await screen.findByRole('region', { name: 'Where configuration lives' });
    expect(within(map).getByText('Tunnel keys')).toBeTruthy();
    expect(screen.queryByText('Tunnel key item')).toBeNull();
    expect(within(map).getByText('Under Keys and credentials')).toBeTruthy();
    expect(within(map).getByText('Recovery administrator token')).toBeTruthy();
    // An item without copy yet keeps a readable name and loses a coded description.
    expect(within(map).getByText('Item')).toBeTruthy();
    expectNoPlumbing(screen.getByRole('tabpanel', { name: 'Overview' }).textContent);
    fireEvent.click(screen.getByRole('link', { name: 'Open Keys and credentials' }));
    expect(
      screen.getByRole('tab', { name: 'Keys and credentials' }).getAttribute('aria-selected'),
    ).toBe('true');
    expect(window.location.search).toBe('?tab=keys');
  });

  it('opens the section an earlier link pointed at', async () => {
    window.history.replaceState(null, '', '/admin/deployment#database');
    mock([agent]);
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    expect(screen.getByRole('tab', { name: 'Database' }).getAttribute('aria-selected')).toBe(
      'true',
    );
  });

  it('moves between sections with the arrow keys, like Settings', async () => {
    mock([agent]);
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    const first = screen.getByRole('tab', { name: 'Overview' });
    first.focus();
    fireEvent.keyDown(first, { key: 'ArrowRight' });
    expect(document.activeElement).toBe(screen.getByRole('tab', { name: 'Machines' }));
    fireEvent.keyDown(document.activeElement as Element, { key: 'End' });
    expect(screen.getByRole('tab', { name: 'Activity' }).getAttribute('aria-selected')).toBe(
      'true',
    );
  });

  it('never shows contract names, commands or raw codes, in any section, step or error', async () => {
    const reporting: api.DeploymentAgent = {
      ...agent,
      observed: {
        ...agent.observed,
        unmanaged_keys: ['AGENT_SOMETHING_LOCAL'],
        legacy_names_present: ['AGENT_WORKSPACE_BROKER_URL', 'AGENT_RUNNER_REPLICAS'],
        releases: {
          ['e'.repeat(64)]: { source_commit: 'f'.repeat(40), database_revision: '0112' },
        },
        database: { host: 'db', port: 5432, database: 'cloudagent', user: 'ca', query: '' },
        policy: { allowed: {}, kata: [], has_kata_helper: false, has_database_tools: false },
      },
    };
    const intent = (operation: string, state: string, detail = ''): api.DeploymentIntent => ({
      intent_id: `${operation}-${state}`,
      agent_id: agent.agent_id,
      seq: 1,
      operation,
      params: {},
      state,
      detail,
      evidence: {},
      workflow_id: null,
      created_by: 'admin:a',
      created_at: '2026-10-30T10:00:00+00:00',
    });
    mock(
      [reporting],
      {
        intents: [
          intent('apply_config', 'succeeded'),
          intent('database_migrate', 'failed', 'pg_restore exited 1 on agent_jobs'),
          intent('rotation_step', 'pending'),
          intent('some_new_operation', 'rejected'),
        ],
        workflows: [
          workflow({ kind: 'rollout', state: 'running', step: 'activate_hosts' }),
          workflow({ kind: 'database_migration', state: 'failed', detail: 'AGENT_DATABASE_URL' }),
          workflow({ kind: 'future_kind', state: 'running' }),
        ],
        maintenance: {
          active: true,
          progress: { a: { operations: [{ phase: 'some_phase' }], received_at: '' } },
        },
      },
      catalog,
    );
    vi.spyOn(api, 'listCredentials').mockResolvedValue({
      credentials: [
        {
          credential_id: 'c1',
          name: 'nightly-report',
          scopes: ['admin', 'deployment'],
          created_by: 'admin:a',
          created_at: '',
          expires_at: null,
          revoked_at: null,
          last_used_at: null,
        },
      ],
    });
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    await screen.findByText('nightly-report');
    expect(screen.getByText('Apply configuration')).toBeTruthy();
    expect(screen.getByText('Move database')).toBeTruthy();
    expect(screen.getByText('Some new operation')).toBeTruthy();
    expect(screen.getByText('Refused by the machine')).toBeTruthy();
    expect(screen.getByText('2 entries')).toBeTruthy();
    expect(screen.getByText('Deployments')).toBeTruthy();
    // Every section is mounted, so the whole page is checked at once.
    expectNoPlumbing(document.body.textContent);
    for (const control of document.querySelectorAll('input, button, [role="combobox"]')) {
      expectNoPlumbing(control.getAttribute('aria-label'));
      expectNoPlumbing(control.getAttribute('placeholder'));
    }
  });

  it('says the control plane is not answering, without its raw error', async () => {
    vi.spyOn(api, 'getDeployments').mockRejectedValue(
      new Error('TypeError: fetch failed ECONNREFUSED /admin/agent/deployments'),
    );
    vi.spyOn(api, 'getManagementStatus').mockResolvedValue(status);
    vi.spyOn(api, 'listCredentials').mockResolvedValue({ credentials: [] });
    vi.spyOn(api, 'getManagementCatalog').mockRejectedValue(new Error('not there'));
    vi.spyOn(api, 'getHostCredentials').mockResolvedValue({ signing_key_id: 'k', hosts: [] });
    render(<DeploymentManagement />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toMatch(/not answering right now/);
    expect(alert.textContent).not.toMatch(/ECONNREFUSED|admin\/agent|TypeError/);
    expect(within(alert).getByRole('button', { name: 'Retry' })).toBeTruthy();
  });
});

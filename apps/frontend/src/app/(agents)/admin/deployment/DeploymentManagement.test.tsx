// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as api from '@/lib/api/adminManagement';
import { DeploymentManagement } from './DeploymentManagement';

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

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
          item: 'web.listener',
          services: ['web'],
          choices: [],
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
      },
      host: {},
    },
    services: {},
  },
});

const status: api.ManagementStatus = {
  settings: { saved_revision: 4, reporting: 2, adopted: 1, restart_required: {} },
  instances: [],
  unidentified_sessions: [
    { application_name: 'psql', user: 'u', client_addr: '', backend_start: null },
  ],
  hosts: [],
};

function mock(agents: api.DeploymentAgent[], extra: Partial<api.DeploymentOverview> = {}) {
  vi.spyOn(api, 'getDeployments').mockResolvedValue({ ...overview(agents), ...extra });
  vi.spyOn(api, 'getManagementStatus').mockResolvedValue(status);
  vi.spyOn(api, 'listCredentials').mockResolvedValue({ credentials: [] });
  vi.spyOn(api, 'getManagementCatalog').mockRejectedValue(new Error('not there'));
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

describe('deployment management', () => {
  it('reports adoption truthfully and offers enrollment with no machines', async () => {
    mock([]);
    render(<DeploymentManagement />);
    expect(await screen.findByText(/adopted by 1 of 2 reporting/)).toBeTruthy();
    expect(screen.getByText(/were not opened by a reporting process/)).toBeTruthy();
    expect(screen.getByText(/No machine has a deployment agent/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Issue enrollment token' })).toBeTruthy();
  });

  it('configures a machine by item, plain values in the clear and a secret only sealed', async () => {
    mock([agent]);
    const intent = vi.spyOn(api, 'createIntent').mockResolvedValue({} as api.DeploymentIntent);
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    fireEvent.change(screen.getByLabelText('Configure'), { target: { value: 'web.listener' } });
    fireEvent.change(screen.getByLabelText('Port (AGENT_WEB_PORT)'), {
      target: { value: '3010' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(intent).toHaveBeenCalledTimes(1));
    expect(intent.mock.calls[0][1]).toEqual({
      operation: 'apply_config',
      params: { changes: { AGENT_WEB_PORT: '3010' } },
    });
    fireEvent.change(screen.getByLabelText('Configure'), { target: { value: 'web.oauth' } });
    fireEvent.change(screen.getByLabelText('Value (AGENT_WEB_SECRET)'), {
      target: { value: 'a-secret-value' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(intent).toHaveBeenCalledTimes(2));
    const sealedRequest = intent.mock.calls[1][1];
    expect(JSON.stringify(sealedRequest)).not.toContain('a-secret-value');
    expect(sealedRequest.sealed?.AGENT_WEB_SECRET?.v).toBe(1);
  });

  it('keeps keys with a guided operation out of direct configuration', async () => {
    mock([agent]);
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    const options = Array.from(
      (screen.getByLabelText('Configure') as HTMLSelectElement).options,
    ).map((option) => option.value);
    expect(options).toEqual(['web.listener', 'web.oauth']);
    expect(screen.queryByLabelText(/\(ADMIN_TOKEN\)/)).toBeNull();
  });

  it('starts each key rotation in its own words, with nothing typed', async () => {
    mock([agent]);
    const start = vi.spyOn(api, 'startWorkflow').mockResolvedValue(workflow({}));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    await screen.findByText('gpu-1');
    fireEvent.click(screen.getByRole('button', { name: 'Rotate User credential key' }));
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    expect(start.mock.calls[0][0]).toMatchObject({
      kind: 'credential_key',
      params: { key: 'user' },
    });
    expect(start.mock.calls[0][0].secret?.length).toBeGreaterThan(30);
    fireEvent.click(screen.getByRole('button', { name: 'Rotate Tunnel SSH keys' }));
    await waitFor(() => expect(start).toHaveBeenCalledTimes(2));
    expect(start.mock.calls[1][0]).toEqual({ kind: 'tunnel_keys' });
  });

  it('shows where a rotation stands and refuses a second one', async () => {
    mock([agent], {
      workflows: [
        workflow({
          progress: { totals: { rewritten: 12, unreadable: 1 } },
          detail: 're-encrypting mcp_access',
        }),
      ],
    });
    render(<DeploymentManagement />);
    expect(await screen.findByText('Credential encryption key')).toBeTruthy();
    const current = screen.getByText('Re-encrypt stored credentials').closest('li');
    expect(current?.getAttribute('aria-current')).toBe('step');
    expect(screen.getByText(/12 values re-encrypted, 1 unreadable/)).toBeTruthy();
    expect(
      (screen.getByRole('button', { name: 'Rotate User credential key' }) as HTMLButtonElement)
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
    expect(await screen.findByText(key)).toBeTruthy();
    expect(screen.getByText(/gpu-1: Accepted on the far end/)).toBeTruthy();
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
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const renew = vi.spyOn(api, 'renewHostCredential').mockResolvedValue({ requested: true });
    const start = vi.spyOn(api, 'startWorkflow').mockResolvedValue(workflow({}));
    render(<DeploymentManagement />);
    fireEvent.click(await screen.findByRole('button', { name: 'Renew' }));
    await waitFor(() => expect(renew).toHaveBeenCalledWith('gpu-1'));

    expect(screen.getByText('cloudagent')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Move the data to an empty database'));
    fireEvent.change(screen.getByLabelText('Database host'), { target: { value: 'db2.example' } });
    fireEvent.change(screen.getByLabelText('Database name'), { target: { value: 'agent' } });
    fireEvent.change(screen.getByLabelText('Database user'), { target: { value: 'agent' } });
    fireEvent.change(screen.getByLabelText('Database password'), {
      target: { value: 'p@ss word' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Move to this database' }));
    await waitFor(() => expect(start).toHaveBeenCalledTimes(1));
    const request = start.mock.calls[0][0];
    expect(request.kind).toBe('database_migration');
    expect(JSON.stringify(request)).not.toContain('ss word');
    expect(request.sealed_by_agent?.[agent.agent_id].AGENT_DATABASE_URL.v).toBe(1);
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
    const buttons = await screen.findAllByRole('button', { name: 'Continue from this step' });
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
    expect(await screen.findByText(/Changes are refused while the database moves/)).toBeTruthy();
    expect(screen.getByText(/Last step reported: Data copied/)).toBeTruthy();
    expect(screen.getByText(/may\s+stop answering for a minute or two/)).toBeTruthy();
  });

  it('maps every item to where it changes, and works without a catalog', async () => {
    mock([agent]);
    vi.spyOn(api, 'getManagementCatalog').mockResolvedValue({
      methods: ['workflow'],
      apply_modes: [],
      items: [
        {
          id: 'hosts.tunnel_keys',
          label: 'Tunnel key item',
          group: 'Security',
          method: 'workflow',
          sources: [{ kind: 'deployment_store', reader: 'x', meaning: 'key files' }],
          owner: 'workflow:tunnel_keys',
          description: 'd',
          apply_mode: 'unverified',
          restart_scope: [],
          role: 'host',
          env_names: [],
          setting_keys: [],
          bounded: false,
        },
      ],
    });
    render(<DeploymentManagement />);
    expect(await screen.findByText('Tunnel key item')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Keys and credentials' })).toBeTruthy();
    expect(screen.getByText(/Guided operation/)).toBeTruthy();
  });
});

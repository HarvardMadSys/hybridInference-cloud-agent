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
        AGENT_WEB_PORT: { kind: 'port', item: 'x', services: ['web'], choices: [], boundary: '' },
        ADMIN_TOKEN: {
          kind: 'secret',
          item: 'y',
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

function mock(agents: api.DeploymentAgent[]) {
  vi.spyOn(api, 'getDeployments').mockResolvedValue(overview(agents));
  vi.spyOn(api, 'getManagementStatus').mockResolvedValue(status);
  vi.spyOn(api, 'listCredentials').mockResolvedValue({ credentials: [] });
}

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

  it('applies a plain value in the clear and a secret only sealed', async () => {
    mock([agent]);
    const intent = vi.spyOn(api, 'createIntent').mockResolvedValue({} as api.DeploymentIntent);
    render(<DeploymentManagement />);
    await screen.findByText('cp-1');
    fireEvent.change(screen.getByLabelText('Setting'), { target: { value: 'AGENT_WEB_PORT' } });
    fireEvent.change(screen.getByLabelText('Value'), { target: { value: '3010' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(intent).toHaveBeenCalledTimes(1));
    expect(intent.mock.calls[0][1]).toEqual({
      operation: 'apply_config',
      params: { changes: { AGENT_WEB_PORT: '3010' } },
    });
    fireEvent.change(screen.getByLabelText('Setting'), { target: { value: 'ADMIN_TOKEN' } });
    fireEvent.change(screen.getByLabelText('Value'), { target: { value: 'a-secret-value' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    await waitFor(() => expect(intent).toHaveBeenCalledTimes(2));
    const sealedRequest = intent.mock.calls[1][1];
    expect(JSON.stringify(sealedRequest)).not.toContain('a-secret-value');
    expect(sealedRequest.sealed?.ADMIN_TOKEN?.v).toBe(1);
  });
});

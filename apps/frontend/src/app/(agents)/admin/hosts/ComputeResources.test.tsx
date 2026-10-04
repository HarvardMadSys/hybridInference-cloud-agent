// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '@/lib/api/compute';
import type {
  ComputeAllocation,
  ComputeCatalog,
  ComputeConnection,
  ComputeOperation,
} from '@/lib/api/compute';
import { APIError } from '@/lib/utils/errors';

import AddComputeServer from './AddComputeServer';
import ComputeConnectionForm from './ComputeConnectionForm';
import ComputeResourcePanel from './ComputeResourcePanel';
import ComputeResources from './ComputeResources';

vi.mock('@/lib/api/compute', () => ({
  getComputeProviders: vi.fn(),
  getComputeConnections: vi.fn(),
  getComputeServers: vi.fn(),
  getComputeOperations: vi.fn(),
  createComputeServer: vi.fn(),
  preflightComputeServer: vi.fn(),
  importComputeResource: vi.fn(),
  saveComputeConnection: vi.fn(),
  validateComputeConnection: vi.fn(),
  setComputePolicy: vi.fn(),
  getRetirementImpact: vi.fn(),
  extendComputeAllocation: vi.fn(),
  retireComputeAllocation: vi.fn(),
  actOnComputeOperation: vi.fn(),
  resolveComputeOperation: vi.fn(),
}));

const catalog: ComputeCatalog = {
  secret_store_configured: true,
  deployment_profiles: [{ id: 'secure-kata', label: 'Kata isolation' }],
  providers: [
    {
      id: 'existing_ssh',
      label: 'Existing SSH server',
      modes: ['attach'],
      capabilities: {},
      fields: [{ key: 'address', label: 'Host address', type: 'text', required: true }],
      connection_fields: [{ key: 'known_hosts', label: 'Verified host keys', type: 'textarea' }],
      secret_label: 'SSH private key',
    },
    {
      id: 'cloudlab',
      label: 'CloudLab',
      modes: ['provision', 'attach'],
      capabilities: { extend: true, import: true },
      requires_ssh_connection: true,
      fields: [
        { key: 'project', label: 'Project', type: 'text', modes: ['provision'], required: true },
        { key: 'profile', label: 'Profile', type: 'text', modes: ['provision'], required: true },
        {
          key: 'experiment_id',
          label: 'Experiment ID',
          type: 'text',
          modes: ['attach'],
          required: true,
        },
      ],
      connection_fields: [],
      secret_label: 'Portal API token',
    },
  ],
};
const connections: ComputeConnection[] = [
  {
    id: 'ssh-account',
    name: 'Lab key',
    provider_id: 'existing_ssh',
    configuration: { known_hosts: 'verified-public-key' },
    status: 'verified',
    masked_summary: 'SSH credential configured',
    revision: 1,
    blocked_reasons: [],
  },
  {
    id: 'cloudlab-account',
    name: 'FAST25',
    provider_id: 'cloudlab',
    configuration: {},
    status: 'verified',
    masked_summary: 'CloudLab credential configured',
    revision: 1,
    blocked_reasons: [],
  },
];
function operation(over: Partial<ComputeOperation> = {}): ComputeOperation {
  return {
    id: 'op-a',
    request_id: 'req-a',
    kind: 'acquire',
    allocation_id: 'resource-a',
    state: 'running',
    step: 'waiting_nodes',
    message: 'Waiting for nodes',
    created_at: '2026-10-04T08:00:00Z',
    updated_at: '2026-10-04T08:01:00Z',
    allowed_actions: [],
    blocked_reasons: [],
    ...over,
  };
}
function allocation(over: Partial<ComputeAllocation> = {}): ComputeAllocation {
  return {
    id: 'resource-a',
    name: 'CloudLab experiment',
    provider_id: 'cloudlab',
    state: 'ready',
    external_id: 'experiment-a',
    expires_at: '2026-10-05T08:00:00Z',
    observed_at: '2026-10-04T08:00:00Z',
    revision: 2,
    allowed_actions: ['policy', 'extend', 'retire'],
    blocked_reasons: [],
    nodes: [{ id: 'node-a', name: 'node 1', hosts: ['host-a'] }],
    policy: {
      enabled: false,
      renew_before_seconds: 3600,
      extend_to_horizon_seconds: 14400,
      stop_new_groups_before_seconds: 1800,
      stop_claims_before_seconds: 300,
      reason: '',
    },
    ...over,
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.getComputeProviders).mockResolvedValue(catalog);
  vi.mocked(api.getComputeConnections).mockResolvedValue({ connections });
  vi.mocked(api.getComputeServers).mockResolvedValue({
    allocations: [allocation()],
    hosts: [
      {
        host: 'old-host',
        retained_groups: 3,
        status: 'draining',
        allocation_id: null,
        compute_node_id: null,
      },
    ],
    active_host: 'host-a',
    observed_at: '2026-10-04T08:00:00Z',
  });
  vi.mocked(api.getComputeOperations).mockResolvedValue({ operations: [] });
});
afterEach(cleanup);

function renderAdd(onOperation = vi.fn()) {
  render(
    <AddComputeServer
      catalog={catalog}
      connections={connections}
      operations={[]}
      onOperation={onOperation}
      onConnectionSaved={vi.fn()}
    />,
  );
  return onOperation;
}
function fillSshForm() {
  fireEvent.change(screen.getByLabelText('Server name'), { target: { value: 'Test server' } });
  fireEvent.change(screen.getByLabelText('Host address'), { target: { value: 'host.example' } });
  fireEvent.change(screen.getByLabelText('Account connection'), {
    target: { value: 'ssh-account' },
  });
}

describe('server acquisition UI', () => {
  it('submits an SSH resource with credential references and a configured deployment', async () => {
    const next = operation();
    vi.mocked(api.createComputeServer).mockResolvedValue({ operation: next });
    const receive = renderAdd();
    fillSshForm();
    fireEvent.click(screen.getByRole('button', { name: 'Connect and deploy' }));
    await waitFor(() => expect(receive).toHaveBeenCalledWith(next, true));
    expect(api.createComputeServer).toHaveBeenCalledWith(
      expect.objectContaining({
        provider_id: 'existing_ssh',
        mode: 'attach',
        connection_id: 'ssh-account',
        configuration: { address: 'host.example' },
        deployment_profile: 'secure-kata',
      }),
    );
    expect(JSON.stringify(vi.mocked(api.createComputeServer).mock.calls)).not.toContain(
      'verified-public-key',
    );
  });

  it('requests CloudLab using separate cloud and SSH connections, and only enabled providers', async () => {
    vi.mocked(api.createComputeServer).mockResolvedValue({ operation: operation() });
    renderAdd();
    expect(screen.queryByRole('radio', { name: /AWS/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('radio', { name: /CloudLab/ }));
    fireEvent.change(screen.getByLabelText('Server name'), { target: { value: 'Cloud machine' } });
    fireEvent.change(screen.getByLabelText('Account connection'), {
      target: { value: 'cloudlab-account' },
    });
    fireEvent.change(screen.getByLabelText('SSH deployment connection'), {
      target: { value: 'ssh-account' },
    });
    fireEvent.change(screen.getByLabelText('Project'), { target: { value: 'FAST25-AE' } });
    fireEvent.change(screen.getByLabelText('Profile'), { target: { value: 'small-lan' } });
    fireEvent.click(screen.getByRole('button', { name: 'Provision and deploy' }));
    await waitFor(() =>
      expect(api.createComputeServer).toHaveBeenCalledWith(
        expect.objectContaining({
          provider_id: 'cloudlab',
          mode: 'provision',
          connection_id: 'cloudlab-account',
          ssh_connection_id: 'ssh-account',
          configuration: { project: 'FAST25-AE', profile: 'small-lan' },
        }),
      ),
    );
  });

  it('recovers an acquisition whose response was lost instead of issuing another create', async () => {
    const next = operation();
    vi.mocked(api.createComputeServer).mockRejectedValue(new Error('Connection interrupted'));
    vi.mocked(api.getComputeOperations).mockImplementation(async (requestId) => ({
      operations: [{ ...next, request_id: requestId! }],
    }));
    const receive = renderAdd();
    fillSshForm();
    fireEvent.click(screen.getByRole('button', { name: 'Connect and deploy' }));
    await waitFor(() =>
      expect(receive).toHaveBeenCalledWith(expect.objectContaining({ id: next.id }), true),
    );
    expect(api.createComputeServer).toHaveBeenCalledTimes(1);
    expect(api.getComputeOperations).toHaveBeenCalledWith(
      vi.mocked(api.createComputeServer).mock.calls[0][0].request_id,
    );
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('preserves the request ID after an unknown result, but changes it when inputs change', async () => {
    vi.mocked(api.createComputeServer).mockRejectedValue(new Error('Connection interrupted'));
    renderAdd();
    fillSshForm();
    const submit = screen.getByRole('button', { name: 'Connect and deploy' });
    fireEvent.click(submit);
    await screen.findByRole('alert');
    const first = vi.mocked(api.createComputeServer).mock.calls[0][0];
    fireEvent.click(submit);
    await waitFor(() => expect(api.createComputeServer).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.createComputeServer).mock.calls[1][0].request_id).toBe(first.request_id);
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.change(screen.getByLabelText('Server name'), {
      target: { value: 'Different machine' },
    });
    fireEvent.click(submit);
    await waitFor(() => expect(api.createComputeServer).toHaveBeenCalledTimes(3));
    expect(vi.mocked(api.createComputeServer).mock.calls[2][0].request_id).not.toBe(
      first.request_id,
    );
  });

  it('validates without provisioning', async () => {
    vi.mocked(api.preflightComputeServer).mockResolvedValue({
      operation: operation({ kind: 'preflight' }),
    });
    renderAdd();
    fillSshForm();
    fireEvent.click(screen.getByRole('button', { name: 'Validate configuration' }));
    await waitFor(() => expect(api.preflightComputeServer).toHaveBeenCalledOnce());
    expect(api.createComputeServer).not.toHaveBeenCalled();
  });
});

describe('connection credentials', () => {
  it('writes a credential only in the connection request and clears it after saving', async () => {
    const saved = vi.fn();
    vi.mocked(api.saveComputeConnection).mockResolvedValue({ connection: connections[0] });
    const local = vi.spyOn(Storage.prototype, 'setItem');
    render(<ComputeConnectionForm catalog={catalog} onSaved={saved} />);
    fireEvent.change(screen.getByLabelText('Connection name'), { target: { value: 'Lab key' } });
    fireEvent.change(screen.getByLabelText('SSH private key'), {
      target: { value: 'sensitive-private-key' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save connection' }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith(connections[0]));
    expect(api.saveComputeConnection).toHaveBeenCalledWith(
      expect.objectContaining({ secret: 'sensitive-private-key' }),
      undefined,
    );
    expect(screen.getByLabelText('SSH private key')).toHaveValue('');
    expect(local).not.toHaveBeenCalled();
    local.mockRestore();
  });

  it('does not populate a previous secret when rotating, and sends the observed revision', async () => {
    vi.mocked(api.saveComputeConnection).mockResolvedValue({ connection: connections[0] });
    render(
      <ComputeConnectionForm catalog={catalog} connection={connections[0]} onSaved={vi.fn()} />,
    );
    expect(screen.getByLabelText('SSH private key')).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Save connection changes' }));
    await waitFor(() =>
      expect(api.saveComputeConnection).toHaveBeenCalledWith(
        expect.objectContaining({ expected_revision: 1 }),
        'ssh-account',
      ),
    );
    expect(vi.mocked(api.saveComputeConnection).mock.calls[0][0]).not.toHaveProperty('secret');
  });
});

describe('resource management', () => {
  it('shows the configured check interval without promising the next execution time', () => {
    const { rerender } = render(
      <ComputeResourcePanel
        allocation={allocation({ check_interval_seconds: 75 })}
        onSaved={vi.fn()}
        onOperation={vi.fn()}
      />,
    );
    expect(screen.getByText(/Periodic provider checks: about every 75 seconds/)).toHaveTextContent(
      'Pending operations can delay a check.',
    );
    expect(screen.queryByText(/Next check:/)).not.toBeInTheDocument();
    rerender(
      <ComputeResourcePanel
        allocation={allocation({ state: 'terminated', check_interval_seconds: null })}
        onSaved={vi.fn()}
        onOperation={vi.fn()}
      />,
    );
    expect(screen.queryByText(/Periodic provider checks:/)).not.toBeInTheDocument();
  });

  it('keeps renewal policy save separate from confirmed extension and preserves conflict edits', async () => {
    vi.mocked(api.setComputePolicy).mockRejectedValue(
      new Error('The policy was updated by another administrator.'),
    );
    render(
      <ComputeResourcePanel allocation={allocation()} onSaved={vi.fn()} onOperation={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText('Renewal reason'), {
      target: { value: 'Preserve evaluation' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save renewal policy' }));
    await screen.findByRole('alert');
    expect(screen.getByLabelText('Renewal reason')).toHaveValue('Preserve evaluation');
    expect(api.extendComputeAllocation).not.toHaveBeenCalled();
    expect(api.setComputePolicy).toHaveBeenCalledWith(
      'resource-a',
      expect.objectContaining({ reason: 'Preserve evaluation' }),
      2,
      undefined,
    );
  });

  it('previews whole-resource dependencies and honors blocked retirement actions', async () => {
    vi.mocked(api.getRetirementImpact).mockResolvedValue({
      allocation_id: 'resource-a',
      revision: 2,
      nodes: allocation().nodes,
      hosts: ['host-a'],
      active_attempts: 1,
      retained_groups: 2,
      global_active_host: 'host-a',
      allowed_actions: [],
      blocked_reasons: [
        { code: 'retained_groups', message: 'Retained sandboxes prevent release.' },
      ],
    });
    render(
      <ComputeResourcePanel allocation={allocation()} onSaved={vi.fn()} onOperation={vi.fn()} />,
    );
    fireEvent.click(screen.getByText('Release cloud resource'));
    fireEvent.click(screen.getByRole('button', { name: 'Preview retirement impact' }));
    expect(
      await screen.findByText('1 running attempts · 2 retained sandbox groups'),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Start retirement for entire resource' }),
    ).toBeDisabled();
    expect(api.retireComputeAllocation).not.toHaveBeenCalled();
  });

  it('requires explicit complete-scope adoption before asking to manage an observed resource', async () => {
    const observed = allocation({ management_mode: 'observe' });
    vi.mocked(api.setComputePolicy).mockResolvedValue({
      allocation: { ...observed, management_mode: 'manage', revision: 3 },
    });
    render(<ComputeResourcePanel allocation={observed} onSaved={vi.fn()} onOperation={vi.fn()} />);
    fireEvent.click(screen.getByRole('checkbox', { name: /I authorize lifecycle management/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Save renewal policy' }));
    await waitFor(() =>
      expect(api.setComputePolicy).toHaveBeenCalledWith('resource-a', observed.policy, 2, 'manage'),
    );
    expect(api.retireComputeAllocation).not.toHaveBeenCalled();
  });

  it('does not show unsupported lease actions for an SSH resource', () => {
    render(
      <ComputeResourcePanel
        allocation={allocation({
          provider_id: 'existing_ssh',
          expires_at: null,
          allowed_actions: [],
        })}
        onSaved={vi.fn()}
        onOperation={vi.fn()}
      />,
    );
    expect(screen.queryByRole('button', { name: 'Request extension' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Request renewal automatically')).not.toBeInTheDocument();
  });
});

describe('resource dashboard', () => {
  it('shows actual retained locations separately and recovers ongoing operations on page open', async () => {
    vi.mocked(api.getComputeOperations).mockResolvedValue({
      operations: [
        operation({
          state: 'unknown',
          allowed_actions: [],
          message: 'External result is being reconciled',
        }),
      ],
    });
    render(<ComputeResources onHostsChanged={vi.fn()} />);
    const oldServer = (await screen.findByRole('rowheader', { name: /old-host/ })).closest('tr')!;
    expect(within(oldServer).getByText('3')).toBeInTheDocument();
    expect(within(oldServer).getByText('draining')).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: /Activity.*1 active/ }));
    fireEvent.click(
      await screen.findByRole('button', { name: /acquire · CloudLab experiment · unknown/ }),
    );
    const dialog = await screen.findByRole('dialog', { name: 'Resource operation' });
    expect(within(dialog).getByText('External result is being reconciled')).toBeInTheDocument();
    expect(
      within(dialog).queryByRole('button', { name: 'Retry operation' }),
    ).not.toBeInTheDocument();
    expect(
      within(dialog).queryByRole('button', { name: 'Cancel operation' }),
    ).not.toBeInTheDocument();
  });

  it('offers explicit reconciliation for an unknown acquisition without creating a resource', async () => {
    vi.mocked(api.getComputeOperations).mockResolvedValue({
      operations: [operation({ state: 'unknown', allowed_actions: ['resolve'] })],
    });
    vi.mocked(api.resolveComputeOperation).mockResolvedValue({
      operation: operation({ state: 'pending' }),
    });
    render(<ComputeResources onHostsChanged={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /Activity.*1 active/ }));
    fireEvent.click(
      await screen.findByRole('button', { name: /acquire · CloudLab experiment · unknown/ }),
    );
    fireEvent.change(await screen.findByLabelText('Existing experiment UUID'), {
      target: { value: 'confirmed-experiment-id' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Adopt this experiment and continue' }));
    await waitFor(() =>
      expect(api.resolveComputeOperation).toHaveBeenCalledWith('op-a', 'confirmed-experiment-id'),
    );
    expect(api.createComputeServer).not.toHaveBeenCalled();
  });

  it('keeps one resource row and its retained count when the global target changes', async () => {
    vi.mocked(api.getComputeServers).mockResolvedValue({
      allocations: [allocation()],
      hosts: [
        {
          host: 'host-a',
          status: 'online',
          retained_groups: 5,
          allocation_id: 'resource-a',
          compute_node_id: 'node-a',
        },
        {
          host: 'old-host',
          status: 'online',
          retained_groups: 3,
          allocation_id: null,
          compute_node_id: null,
        },
      ],
      active_host: 'host-a',
      observed_at: '2026-10-04T08:00:00Z',
    });
    const { rerender } = render(<ComputeResources onHostsChanged={vi.fn()} activeHost="host-a" />);
    const table = await screen.findByRole('table', { name: 'Server inventory' });
    expect(within(table).getAllByRole('row')).toHaveLength(3);
    const cloudRow = screen.getByRole('rowheader', { name: /CloudLab experiment/ }).closest('tr')!;
    const oldRow = screen.getByRole('rowheader', { name: /old-host/ }).closest('tr')!;
    expect(within(cloudRow).getByText('5')).toBeInTheDocument();
    expect(within(cloudRow).getByText('Selected')).toBeInTheDocument();
    expect(within(oldRow).getByText('3')).toBeInTheDocument();
    rerender(<ComputeResources onHostsChanged={vi.fn()} activeHost="old-host" />);
    expect(within(oldRow).getByText('Selected')).toBeInTheDocument();
    expect(within(cloudRow).queryByText('Selected')).not.toBeInTheDocument();
    expect(within(cloudRow).getByText('5')).toBeInTheDocument();
    expect(within(oldRow).getByText('3')).toBeInTheDocument();
  });

  it('opens one management panel for all enrollments of the selected machine', async () => {
    const controls = vi.fn((hosts: string[]) => <div>Controls: {hosts.join(', ')}</div>);
    vi.mocked(api.getComputeServers).mockResolvedValue({
      allocations: [],
      hosts: [
        {
          host: 'old-host',
          status: 'draining',
          retained_groups: 3,
          allocation_id: null,
          compute_node_id: null,
        },
        {
          host: 'kata-host',
          status: 'online',
          retained_groups: 4,
          allocation_id: null,
          compute_node_id: null,
        },
      ],
      active_host: 'kata-host',
      observed_at: '2026-10-04T08:00:00Z',
    });
    render(
      <ComputeResources
        onHostsChanged={vi.fn()}
        renderHostControls={controls}
        serverGroups={[
          {
            name: 'Team server',
            host: { host: 'kata-host' },
            members: [{ host: 'old-host' }, { host: 'kata-host' }],
          },
        ]}
      />,
    );
    const table = await screen.findByRole('table', { name: 'Server inventory' });
    expect(within(table).getAllByRole('row')).toHaveLength(2);
    const row = screen.getByRole('rowheader', { name: /Team server/ }).closest('tr')!;
    expect(within(row).getByText('7')).toBeInTheDocument();
    expect(within(row).getByText('Selected')).toBeInTheDocument();
    expect(within(row).getByText('Online')).toBeInTheDocument();
    expect(controls).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Manage Team server' }));
    expect(controls).toHaveBeenCalledWith(['old-host', 'kata-host']);
    const dialog = screen.getByRole('dialog', { name: 'Team server' });
    expect(within(dialog).getByText('Controls: old-host, kata-host')).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close panel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('keeps resource actions, renewal drafts and activity under the selected server', async () => {
    vi.mocked(api.getComputeOperations).mockResolvedValue({
      operations: [
        operation(),
        operation({
          id: 'other-op',
          allocation_id: 'other-resource',
          message: 'Other server activity',
        }),
      ],
    });
    const controls = vi.fn((hosts: string[]) => <button>Pause {hosts.join(', ')}</button>);
    const useHost = vi.fn();
    render(
      <ComputeResources
        onHostsChanged={vi.fn()}
        renderHostControls={controls}
        activeHost="old-host"
        eligibleHosts={['host-a']}
        onUseHost={useHost}
        hostNotice="Host command saved."
      />,
    );
    expect(controls).not.toHaveBeenCalled();
    fireEvent.click(await screen.findByRole('button', { name: 'Manage CloudLab experiment' }));
    let dialog = screen.getByRole('dialog', { name: 'CloudLab experiment' });
    expect(controls).toHaveBeenCalledWith(['host-a']);
    expect(within(dialog).getByRole('status')).toHaveTextContent('Host command saved.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Use for new sandboxes' }));
    expect(useHost).toHaveBeenCalledWith('host-a');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Lease' }));
    expect(within(dialog).queryByRole('button', { name: 'Pause host-a' })).not.toBeInTheDocument();
    fireEvent.change(within(dialog).getByLabelText('Renewal reason'), {
      target: { value: 'Keep this draft' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Overview' }));
    expect(within(dialog).getByRole('button', { name: 'Pause host-a' })).toBeVisible();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Lease' }));
    expect(within(dialog).getByLabelText('Renewal reason')).toHaveValue('Keep this draft');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Activity' }));
    expect(within(dialog).queryByText('Other server activity')).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: /acquire · running/ }));
    const operationDialog = screen.getByRole('dialog', { name: 'Resource operation' });
    fireEvent.click(within(operationDialog).getByRole('button', { name: '← Back' }));
    dialog = screen.getByRole('dialog', { name: 'CloudLab experiment' });
    expect(within(dialog).getByRole('button', { name: 'Activity' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    fireEvent.click(within(dialog).getByRole('button', { name: /acquire · running/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Close panel' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('retains a working Host management entry when the compute query fails', async () => {
    vi.mocked(api.getComputeServers).mockRejectedValue(new Error('Resource service unavailable'));
    const controls = vi.fn(() => <button>Pause old-host</button>);
    render(
      <ComputeResources
        onHostsChanged={vi.fn()}
        renderHostControls={controls}
        serverGroups={[
          {
            name: 'Original server',
            host: { host: 'old-host' },
            members: [{ host: 'old-host', status: 'online' }],
          },
        ]}
      />,
    );
    await screen.findByText('Resource service unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Manage Original server' }));
    const dialog = screen.getByRole('dialog', { name: 'Original server' });
    expect(within(dialog).getByRole('button', { name: 'Pause old-host' })).toBeInTheDocument();
    expect(controls).toHaveBeenCalledWith(['old-host']);
  });

  it('does not offer placement or lease controls for an ineligible SSH server', async () => {
    vi.mocked(api.getComputeServers).mockResolvedValue({
      allocations: [
        allocation({ provider_id: 'existing_ssh', allowed_actions: ['retire'], expires_at: null }),
      ],
      hosts: [],
      active_host: null,
      observed_at: '2026-10-04T08:00:00Z',
    });
    render(<ComputeResources onHostsChanged={vi.fn()} eligibleHosts={[]} onUseHost={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Manage CloudLab experiment' }));
    const dialog = screen.getByRole('dialog', { name: 'CloudLab experiment' });
    expect(within(dialog).queryByRole('button', { name: 'Lease' })).not.toBeInTheDocument();
    expect(
      within(dialog).queryByRole('button', { name: 'Use for new sandboxes' }),
    ).not.toBeInTheDocument();
    expect(within(dialog).getByText('Disconnect server')).toBeInTheDocument();
  });

  it('does not call a ready cloud resource online before its agent has enrolled', async () => {
    render(<ComputeResources onHostsChanged={vi.fn()} />);
    const row = (await screen.findByRole('rowheader', { name: /CloudLab experiment/ })).closest(
      'tr',
    )!;
    expect(within(row).getByText('Awaiting agent')).toBeInTheDocument();
    expect(within(row).queryByText('Online')).not.toBeInTheDocument();
  });

  it('keeps an unfinished server form when adding a connection', async () => {
    vi.mocked(api.saveComputeConnection).mockResolvedValue({ connection: connections[0] });
    render(<ComputeResources onHostsChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add server' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
    fireEvent.change(await screen.findByLabelText('Server name'), {
      target: { value: 'Pending new server' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add SSH connection' }));
    const connectionDialog = screen.getByRole('dialog', { name: 'Add server' });
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(within(connectionDialog).getByLabelText('Connection provider')).toBeDisabled();
    fireEvent.change(within(connectionDialog).getByLabelText('Connection name'), {
      target: { value: 'New key' },
    });
    fireEvent.change(within(connectionDialog).getByLabelText('SSH private key'), {
      target: { value: 'private-key-input' },
    });
    fireEvent.click(within(connectionDialog).getByRole('button', { name: 'Save connection' }));
    await waitFor(() =>
      expect(screen.getByLabelText('Account connection')).toHaveValue('ssh-account'),
    );
    expect(screen.getByLabelText('Server name')).toHaveValue('Pending new server');
    expect(screen.queryByLabelText('SSH private key')).not.toBeInTheDocument();
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
  });

  it('keeps old Host management independent when resource management is not configured', async () => {
    vi.mocked(api.getComputeProviders).mockResolvedValue({
      ...catalog,
      secret_store_configured: false,
    });
    render(<ComputeResources onHostsChanged={vi.fn()} />);
    await screen.findByText(/Configure the control plane secret store/);
    expect(screen.getByRole('button', { name: 'Add server' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Manage CloudLab experiment' })).toBeEnabled();
  });

  it('removes sensitive management forms when authorization is revoked', async () => {
    vi.mocked(api.getComputeConnections).mockRejectedValue(
      new APIError('FORBIDDEN', 'Forbidden', 403),
    );
    render(<ComputeResources onHostsChanged={vi.fn()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('access was revoked');
    expect(screen.queryByRole('button', { name: 'Add server' })).not.toBeInTheDocument();
  });
});

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
    connection_id: 'cloudlab-account',
    ssh_connection_id: 'ssh-account',
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

function renderAdd(onOperation = vi.fn(), testCatalog = catalog, saved = connections) {
  render(
    <AddComputeServer
      catalog={testCatalog}
      connections={saved}
      onOperation={onOperation}
      onConnectionSaved={vi.fn()}
    />,
  );
  return onOperation;
}
function fillSshForm() {
  fireEvent.change(screen.getByLabelText('Server name'), { target: { value: 'Test server' } });
  fireEvent.change(screen.getByLabelText('Host address'), { target: { value: 'host.example' } });
}

describe('server acquisition UI', () => {
  it('submits SSH credential references and leaves deployment selection to the server', async () => {
    const next = operation();
    vi.mocked(api.createComputeServer).mockResolvedValue({ operation: next });
    const receive = renderAdd();
    expect(screen.queryByLabelText('Agent deployment')).not.toBeInTheDocument();
    expect(screen.queryByText('Deployment settings')).not.toBeInTheDocument();
    fillSshForm();
    fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
    await waitFor(() => expect(receive).toHaveBeenCalledWith(next, true));
    expect(api.createComputeServer).toHaveBeenCalledWith(
      expect.objectContaining({
        provider_id: 'existing_ssh',
        mode: 'attach',
        connection_id: 'ssh-account',
        configuration: { address: 'host.example' },
      }),
    );
    expect(vi.mocked(api.createComputeServer).mock.calls[0][0]).not.toHaveProperty(
      'deployment_profile',
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
    fireEvent.change(screen.getByLabelText('Project'), { target: { value: 'FAST25-AE' } });
    fireEvent.change(screen.getByLabelText('Profile'), { target: { value: 'small-lan' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create server' }));
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

  it('keeps optional CloudLab values and defaults when advanced options are collapsed', async () => {
    vi.mocked(api.createComputeServer).mockResolvedValue({ operation: operation() });
    const withAdvancedFields: ComputeCatalog = {
      ...catalog,
      providers: catalog.providers.map((provider) => {
        if (provider.id !== 'cloudlab') return provider;
        return {
          ...provider,
          fields: [
            ...provider.fields,
            { key: 'group', label: 'Project group', type: 'text', advanced: true },
            {
              key: 'bindings',
              label: 'Profile parameters',
              type: 'json',
              default: {},
              advanced: true,
            },
          ],
        };
      }),
    };
    render(
      <AddComputeServer
        catalog={withAdvancedFields}
        connections={connections}
        onOperation={vi.fn()}
        onConnectionSaved={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('radio', { name: /CloudLab/ }));
    fireEvent.change(screen.getByLabelText('Server name'), { target: { value: 'Cloud machine' } });
    fireEvent.change(screen.getByLabelText('Project'), { target: { value: 'FAST25-AE' } });
    fireEvent.change(screen.getByLabelText('Profile'), { target: { value: 'small-lan' } });
    const advanced = screen.getByText('Advanced options');
    const details = advanced.closest('details')!;
    expect(details).not.toHaveAttribute('open');
    fireEvent.click(advanced);
    fireEvent.change(screen.getByLabelText('Project group'), { target: { value: 'research' } });
    fireEvent.click(advanced);
    expect(details).not.toHaveAttribute('open');
    fireEvent.click(screen.getByRole('button', { name: 'Create server' }));
    await waitFor(() =>
      expect(api.createComputeServer).toHaveBeenCalledWith(
        expect.objectContaining({
          configuration: {
            project: 'FAST25-AE',
            profile: 'small-lan',
            group: 'research',
            bindings: {},
          },
        }),
      ),
    );
  });

  it('requires a choice when several keys are saved, then reuses the chosen key', async () => {
    vi.mocked(api.createComputeServer).mockResolvedValue({ operation: operation() });
    renderAdd(vi.fn(), catalog, [
      ...connections,
      { ...connections[0], id: 'other-key', name: 'Other key' },
    ]);
    fillSshForm();
    const picker = screen.getByRole('combobox', { name: 'Use SSH key' });
    expect(picker).toHaveValue('');
    fireEvent.change(picker, { target: { value: 'other-key' } });
    expect(screen.queryByRole('combobox', { name: 'Use SSH key' })).not.toBeInTheDocument();
    expect(screen.getByText('Other key')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Change SSH key' })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'Change SSH key' }));
    expect(screen.getByRole('combobox', { name: 'Use SSH key' })).toHaveFocus();
    fireEvent.change(screen.getByRole('combobox', { name: 'Use SSH key' }), {
      target: { value: '__new__' },
    });
    fireEvent.change(screen.getByLabelText('SSH private key'), {
      target: { value: 'discard-this-key' },
    });
    fireEvent.change(screen.getByRole('combobox', { name: 'Use SSH key' }), {
      target: { value: 'other-key' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
    await waitFor(() =>
      expect(api.createComputeServer).toHaveBeenCalledWith(
        expect.objectContaining({ connection_id: 'other-key' }),
      ),
    );
    expect(api.saveComputeConnection).not.toHaveBeenCalled();
    expect(JSON.stringify(vi.mocked(api.createComputeServer).mock.calls)).not.toContain(
      'discard-this-key',
    );
  });

  it('opens advanced options and focuses invalid JSON before saving credentials or creating a server', async () => {
    const withJson: ComputeCatalog = {
      ...catalog,
      providers: catalog.providers.map((provider) => ({
        ...provider,
        fields: [
          ...provider.fields,
          {
            key: 'bindings',
            label: 'Profile parameters',
            type: 'json',
            advanced: true,
            default: {},
          },
        ],
      })),
    };
    vi.mocked(api.createComputeServer).mockResolvedValue({ operation: operation() });
    renderAdd(vi.fn(), withJson);
    fillSshForm();
    const summary = screen.getByText('Advanced options');
    fireEvent.click(summary);
    fireEvent.change(screen.getByLabelText('Profile parameters'), { target: { value: '{broken' } });
    fireEvent.click(summary);
    fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Profile parameters must contain valid JSON.',
    );
    expect(summary.closest('details')).toHaveAttribute('open');
    expect(screen.getByLabelText('Profile parameters')).toHaveFocus();
    expect(api.createComputeServer).not.toHaveBeenCalled();
    expect(api.saveComputeConnection).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText('Profile parameters'), {
      target: { value: '{"nodes":1}' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
    await waitFor(() =>
      expect(api.createComputeServer).toHaveBeenCalledWith(
        expect.objectContaining({
          configuration: { address: 'host.example', bindings: { nodes: 1 } },
        }),
      ),
    );
  });

  it('imports observation only without a deployment or SSH credential, and resets that choice for creation', async () => {
    const observedCatalog: ComputeCatalog = {
      ...catalog,
      deployment_profiles: [],
      providers: catalog.providers.map((provider) => ({
        ...provider,
        fields: [
          ...provider.fields,
          {
            key: 'ssh_username',
            label: 'CloudLab SSH user',
            type: 'text',
            required: true,
            deployment_only: true,
            section: 'access',
          },
        ],
      })),
    };
    vi.mocked(api.importComputeResource).mockResolvedValue({
      operation: operation({ kind: 'import' }),
    });
    renderAdd(vi.fn(), observedCatalog, [connections[1]]);
    fireEvent.click(screen.getByRole('radio', { name: /CloudLab/ }));
    fireEvent.click(screen.getByRole('radio', { name: 'Connect existing' }));
    fireEvent.change(screen.getByLabelText('Server name'), {
      target: { value: 'Observed experiment' },
    });
    fireEvent.change(screen.getByLabelText('Experiment ID'), { target: { value: 'experiment-a' } });
    fireEvent.change(screen.getByLabelText('CloudLab SSH user'), { target: { value: 'unused' } });
    fireEvent.click(screen.getByText('Advanced options'));
    fireEvent.click(
      screen.getByRole('checkbox', { name: 'Only track this resource (skip server setup)' }),
    );
    expect(screen.queryByLabelText('CloudLab SSH user')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('SSH private key')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Import resource' }));
    await waitFor(() =>
      expect(api.importComputeResource).toHaveBeenCalledWith(
        expect.objectContaining({
          configuration: { experiment_id: 'experiment-a' },
          connection_id: 'cloudlab-account',
        }),
      ),
    );
    expect(vi.mocked(api.importComputeResource).mock.calls[0][0]).not.toHaveProperty(
      'ssh_connection_id',
    );
    expect(api.createComputeServer).not.toHaveBeenCalled();
    expect(api.saveComputeConnection).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole('radio', { name: 'Create new' })).toBeEnabled());
    fireEvent.click(screen.getByRole('radio', { name: 'Create new' }));
    expect(screen.getByRole('button', { name: 'Create server' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Import resource' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('SSH private key')).toBeInTheDocument();
  });

  it('recovers an acquisition whose response was lost instead of issuing another create', async () => {
    const next = operation();
    vi.mocked(api.createComputeServer).mockRejectedValue(new Error('Connection interrupted'));
    vi.mocked(api.getComputeOperations).mockImplementation(async (requestId) => ({
      operations: [{ ...next, request_id: requestId! }],
    }));
    const receive = renderAdd();
    fillSshForm();
    fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
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
    const submit = screen.getByRole('button', { name: 'Add server' });
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

  it('keeps a saved cloud account when saving the SSH key fails, then submits only their IDs', async () => {
    vi.mocked(api.saveComputeConnection)
      .mockResolvedValueOnce({ connection: connections[1] })
      .mockRejectedValueOnce(new Error('SSH key rejected'))
      .mockResolvedValueOnce({ connection: connections[0] });
    vi.mocked(api.createComputeServer).mockResolvedValue({ operation: operation() });
    render(
      <AddComputeServer
        catalog={catalog}
        connections={[]}
        onOperation={vi.fn()}
        onConnectionSaved={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('radio', { name: /CloudLab/ }));
    fireEvent.change(screen.getByLabelText('Server name'), { target: { value: 'Research' } });
    fireEvent.change(screen.getByLabelText('Project'), { target: { value: 'FAST25' } });
    fireEvent.change(screen.getByLabelText('Profile'), { target: { value: 'small' } });
    fireEvent.change(screen.getByLabelText('Portal API token'), {
      target: { value: 'cloud-token' },
    });
    fireEvent.change(screen.getByLabelText('SSH private key'), { target: { value: 'ssh-key' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create server' }));
    await screen.findByText('SSH key rejected');
    expect(api.createComputeServer).not.toHaveBeenCalled();
    expect(api.getComputeOperations).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Change CloudLab account' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Portal API token')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Create server' }));
    await waitFor(() => expect(api.createComputeServer).toHaveBeenCalledOnce());
    expect(api.saveComputeConnection).toHaveBeenCalledTimes(3);
    expect(api.createComputeServer).toHaveBeenCalledWith(
      expect.objectContaining({
        connection_id: 'cloudlab-account',
        ssh_connection_id: 'ssh-account',
      }),
    );
    expect(JSON.stringify(vi.mocked(api.createComputeServer).mock.calls)).not.toMatch(
      /cloud-token|ssh-key/,
    );
  });

  it('submits through one primary action and opens the setup operation', async () => {
    const next = operation();
    vi.mocked(api.createComputeServer).mockResolvedValue({ operation: next });
    const received = renderAdd();
    fillSshForm();
    expect(
      screen.queryByRole('button', { name: 'Validate configuration' }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Use SSH key' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
    await waitFor(() => expect(received).toHaveBeenCalledWith(next, true));
    expect(api.createComputeServer).toHaveBeenCalledOnce();
    expect(api.preflightComputeServer).not.toHaveBeenCalled();
  });
});

describe('connection credentials', () => {
  it('writes a credential only in the connection request and clears it after saving', async () => {
    const saved = vi.fn();
    vi.mocked(api.saveComputeConnection).mockResolvedValue({ connection: connections[0] });
    const local = vi.spyOn(Storage.prototype, 'setItem');
    render(<ComputeConnectionForm catalog={catalog} connection={connections[0]} onSaved={saved} />);
    fireEvent.change(screen.getByLabelText('Key name'), { target: { value: 'Lab key' } });
    fireEvent.change(screen.getByLabelText('SSH private key'), {
      target: { value: 'sensitive-private-key' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
    await waitFor(() => expect(saved).toHaveBeenCalledWith(connections[0]));
    expect(api.saveComputeConnection).toHaveBeenCalledWith(
      expect.objectContaining({ secret: 'sensitive-private-key' }),
      'ssh-account',
    );
    expect(screen.getByLabelText('SSH private key')).toHaveValue('');
    expect(local).not.toHaveBeenCalled();
    local.mockRestore();
  });

  it('does not populate a previous secret when rotating, and sends the observed revision', async () => {
    vi.mocked(api.saveComputeConnection).mockResolvedValue({ connection: connections[0] });
    const { rerender } = render(
      <ComputeConnectionForm catalog={catalog} connection={connections[0]} onSaved={vi.fn()} />,
    );
    expect(screen.getByLabelText('SSH private key')).toHaveValue('');
    rerender(
      <ComputeConnectionForm
        catalog={catalog}
        connection={{ ...connections[0], revision: 2, name: 'Changed elsewhere' }}
        onSaved={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('Key name')).toHaveValue('Lab key');
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
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
    const oldServer = await screen.findByRole('listitem', { name: /old-host/ });
    expect(within(oldServer).getByText('3 sandboxes')).toBeInTheDocument();
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
    const table = await screen.findByRole('list', { name: 'Server inventory' });
    expect(within(table).getAllByRole('listitem')).toHaveLength(2);
    const cloudRow = screen.getByRole('listitem', { name: /CloudLab experiment/ });
    const oldRow = screen.getByRole('listitem', { name: /old-host/ });
    expect(within(cloudRow).getByText('5 sandboxes')).toBeInTheDocument();
    expect(within(cloudRow).getByText('Current target')).toBeInTheDocument();
    expect(within(oldRow).getByText('3 sandboxes')).toBeInTheDocument();
    rerender(<ComputeResources onHostsChanged={vi.fn()} activeHost="old-host" />);
    expect(within(oldRow).getByText('Current target')).toBeInTheDocument();
    expect(within(cloudRow).queryByText('Current target')).not.toBeInTheDocument();
    expect(within(cloudRow).getByText('5 sandboxes')).toBeInTheDocument();
    expect(within(oldRow).getByText('3 sandboxes')).toBeInTheDocument();
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
    const table = await screen.findByRole('list', { name: 'Server inventory' });
    expect(within(table).getAllByRole('listitem')).toHaveLength(1);
    const row = screen.getByRole('listitem', { name: /Team server/ });
    expect(within(row).getByText('7 sandboxes')).toBeInTheDocument();
    expect(within(row).getByText('Current target')).toBeInTheDocument();
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
    fireEvent.click(within(dialog).getByLabelText('Server actions'));
    fireEvent.click(within(dialog).getByRole('menuitem', { name: 'Disconnect server' }));
    expect(
      within(dialog).getByRole('button', { name: 'Preview retirement impact' }),
    ).toBeInTheDocument();
  });

  it('does not call a ready cloud resource online before its agent has enrolled', async () => {
    render(<ComputeResources onHostsChanged={vi.fn()} />);
    const row = await screen.findByRole('listitem', { name: /CloudLab experiment/ });
    expect(within(row).getByText('Awaiting agent')).toBeInTheDocument();
    expect(within(row).queryByText('Online')).not.toBeInTheDocument();
  });

  it('adds a key inline and reuses it after a failed server request without losing the draft', async () => {
    vi.mocked(api.saveComputeConnection).mockResolvedValue({
      connection: { ...connections[0], id: 'new-key' },
    });
    vi.mocked(api.createComputeServer)
      .mockRejectedValueOnce(new Error('Try again'))
      .mockResolvedValueOnce({ operation: operation() });
    const local = vi.spyOn(Storage.prototype, 'setItem');
    render(<ComputeResources onHostsChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add server' })).toBeEnabled());
    expect(screen.queryByRole('button', { name: 'Connections' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
    const dialog = screen.getByRole('dialog', { name: 'Add server' });
    fireEvent.change(within(dialog).getByLabelText('Server name'), {
      target: { value: 'Pending new server' },
    });
    fireEvent.change(within(dialog).getByLabelText('Host address'), {
      target: { value: 'host.example' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Change SSH key' }));
    fireEvent.change(within(dialog).getByLabelText('Use SSH key'), {
      target: { value: '__new__' },
    });
    fireEvent.change(within(dialog).getByLabelText('SSH private key'), {
      target: { value: 'private-key-input' },
    });
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(within(dialog).queryByRole('button', { name: /Save/ })).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add server' }));
    await within(dialog).findByText('Try again');
    expect(within(dialog).getByRole('button', { name: 'Change SSH key' })).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Server name')).toHaveValue('Pending new server');
    expect(within(dialog).queryByLabelText('SSH private key')).not.toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add server' }));
    await waitFor(() => expect(api.createComputeServer).toHaveBeenCalledTimes(2));
    expect(api.saveComputeConnection).toHaveBeenCalledTimes(1);
    expect(api.saveComputeConnection).toHaveBeenCalledWith(
      expect.objectContaining({ name: 'Pending new server key', secret: 'private-key-input' }),
    );
    expect(vi.mocked(api.createComputeServer).mock.calls[1][0]).toEqual(
      vi.mocked(api.createComputeServer).mock.calls[0][0],
    );
    expect(vi.mocked(api.createComputeServer).mock.calls[0][0].connection_id).toBe('new-key');
    expect(JSON.stringify(vi.mocked(api.createComputeServer).mock.calls)).not.toContain(
      'private-key-input',
    );
    expect(local).not.toHaveBeenCalled();
    local.mockRestore();
  });

  it('edits shared credentials from the selected server and stays in that server', async () => {
    vi.mocked(api.getComputeServers).mockResolvedValue({
      allocations: [allocation(), allocation({ id: 'second', name: 'Another server', nodes: [] })],
      hosts: [],
      active_host: null,
      observed_at: '2026-10-04T08:00:00Z',
    });
    vi.mocked(api.saveComputeConnection).mockResolvedValue({ connection: connections[1] });
    render(<ComputeResources onHostsChanged={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Manage CloudLab experiment' }));
    const dialog = screen.getByRole('dialog', { name: 'CloudLab experiment' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Access settings' }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Edit account' }));
    expect(within(dialog).getByText(/Used by 2 servers; changes apply to all/)).toBeInTheDocument();
    expect(within(dialog).getByLabelText('Portal API token')).toHaveValue('');
    fireEvent.change(within(dialog).getByLabelText('Portal API token'), {
      target: { value: 'rotated-token' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
    await within(dialog).findByText('Access settings saved.');
    expect(api.saveComputeConnection).toHaveBeenCalledWith(
      expect.objectContaining({ expected_revision: 1, secret: 'rotated-token' }),
      'cloudlab-account',
    );
    expect(within(dialog).queryByLabelText('Portal API token')).not.toBeInTheDocument();
    expect(screen.getAllByRole('dialog')).toHaveLength(1);
    expect(api.createComputeServer).not.toHaveBeenCalled();
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

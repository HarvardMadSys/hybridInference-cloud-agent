// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '@/lib/api/compute';
import { forgetAgentRunnerHost } from '@/lib/api/admin';
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
  removeComputeServer: vi.fn(),
  getComputeOperations: vi.fn(),
  createComputeServer: vi.fn(),
  preflightComputeServer: vi.fn(),
  importComputeResource: vi.fn(),
  saveComputeDeploymentProfile: vi.fn(),
  getComputeAutomaticSetup: vi.fn(),
  createAutomaticComputeSetup: vi.fn(),
  saveComputeConnection: vi.fn(),
  validateComputeConnection: vi.fn(),
  setComputePolicy: vi.fn(),
  getRetirementImpact: vi.fn(),
  extendComputeAllocation: vi.fn(),
  retireComputeAllocation: vi.fn(),
  rotateComputeAllocation: vi.fn(),
  actOnComputeOperation: vi.fn(),
  resolveComputeOperation: vi.fn(),
}));

vi.mock('@/lib/api/admin', () => ({ forgetAgentRunnerHost: vi.fn() }));

vi.mock('./CloudLabAutoscaling', () => ({
  default: () => <p>CloudLab auto-scaling controls</p>,
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
        {
          key: 'host_key_source',
          label: 'SSH host-key checking',
          type: 'select',
          default: 'none',
          advanced: true,
          deployment_only: true,
          modes: ['provision'],
          options: [
            { value: 'none', label: 'Do not verify the CloudLab host' },
            { value: 'registration', label: 'Registered by the node at first boot' },
          ],
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
const cloudLabSshKey: ComputeConnection = {
  ...connections[0],
  configuration: {},
  status: 'unverified',
  blocked_reasons: [
    { code: 'connection_unverified', message: 'Validate this connection before deployment.' },
  ],
};

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
      rotation_enabled: false,
      max_node_age_seconds: 604800,
      node_types: [],
      node_type_binding: 'phystype',
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
  vi.mocked(api.getComputeAutomaticSetup).mockResolvedValue({
    available: true,
    deployment_sha: 'c'.repeat(40),
    blocked_reasons: [],
  });
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
  fireEvent.change(screen.getByLabelText('Sandbox storage path'), {
    target: { value: '/mnt/user-data' },
  });
}

describe('server acquisition UI', () => {
  it('submits SSH credential references and leaves deployment selection to the server', async () => {
    const next = operation();
    vi.mocked(api.createComputeServer).mockResolvedValue({ operation: next });
    const receive = renderAdd();
    expect(screen.queryByLabelText('Agent deployment')).not.toBeInTheDocument();
    expect(screen.queryByText('Deployment settings')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Sandbox storage path')).toHaveValue('');
    expect(screen.getByLabelText('Sandbox storage path')).toBeRequired();
    expect(screen.getByRole('region', { name: 'Sandbox storage' })).toBeVisible();
    fillSshForm();
    fireEvent.change(screen.getByLabelText('Sandbox storage path'), {
      target: { value: ' /mnt/custom-data/ ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
    await waitFor(() => expect(receive).toHaveBeenCalledWith(next, true));
    expect(api.createComputeServer).toHaveBeenCalledWith(
      expect.objectContaining({
        provider_id: 'existing_ssh',
        mode: 'attach',
        connection_id: 'ssh-account',
        configuration: { address: 'host.example' },
        sandbox_storage_path: '/mnt/custom-data',
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
    fireEvent.change(screen.getByLabelText('Sandbox storage path'), {
      target: { value: '/mnt/cloudlab-data' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create server' }));
    await waitFor(() =>
      expect(api.createComputeServer).toHaveBeenCalledWith(
        expect.objectContaining({
          provider_id: 'cloudlab',
          mode: 'provision',
          connection_id: 'cloudlab-account',
          ssh_connection_id: 'ssh-account',
          configuration: { project: 'FAST25-AE', profile: 'small-lan', host_key_source: 'none' },
          sandbox_storage_path: '/mnt/cloudlab-data',
        }),
      ),
    );
  });

  it('keeps verified host keys when an older catalog does not advertise unchecked CloudLab SSH', () => {
    const providers = catalog.providers.map((provider) => {
      if (provider.id !== 'cloudlab') return provider;
      return {
        ...provider,
        fields: provider.fields.map((field) => {
          if (field.key !== 'host_key_source') return field;
          return {
            ...field,
            default: 'known_hosts',
            options: [
              { value: 'known_hosts', label: 'Verified list on the SSH connection' },
              {
                value: 'registration',
                label: 'Registered by the node at first boot',
              },
            ],
          };
        }),
      };
    });
    const oldCatalog: ComputeCatalog = { ...catalog, providers };
    renderAdd(vi.fn(), oldCatalog, []);
    fireEvent.click(screen.getByRole('radio', { name: /CloudLab/ }));
    expect(screen.getByLabelText('Verified host keys')).toBeInTheDocument();
    expect(
      screen.queryByText('CloudLab SSH host identity will not be verified.'),
    ).not.toBeInTheDocument();
  });

  it('warns when connect-existing ignores a previously selected registration policy', () => {
    renderAdd();
    fireEvent.click(screen.getByRole('radio', { name: /CloudLab/ }));
    fireEvent.change(screen.getByLabelText('SSH host-key checking'), {
      target: { value: 'registration' },
    });
    expect(
      screen.queryByText('CloudLab SSH host identity will not be verified.'),
    ).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('radio', { name: 'Connect existing' }));

    expect(
      screen.getByText('CloudLab SSH host identity will not be verified.'),
    ).toBeInTheDocument();
  });

  it('prefills CloudLab fields from the provider catalog defaults', async () => {
    const withDefaults: ComputeCatalog = {
      ...catalog,
      providers: catalog.providers.map((provider) => ({
        ...provider,
        fields: provider.fields.map((field) =>
          field.key === 'project'
            ? { ...field, default: 'FAST25-AE' }
            : field.key === 'profile'
              ? { ...field, default: 'small-lan' }
              : field,
        ),
      })),
    };
    vi.mocked(api.createComputeServer).mockResolvedValue({ operation: operation() });
    renderAdd(vi.fn(), withDefaults);
    fireEvent.click(screen.getByRole('radio', { name: /CloudLab/ }));

    expect(screen.getByLabelText('Project')).toHaveValue('FAST25-AE');
    expect(screen.getByLabelText('Profile')).toHaveValue('small-lan');
    fireEvent.change(screen.getByLabelText('Server name'), { target: { value: 'Cloud machine' } });
    fireEvent.change(screen.getByLabelText('Sandbox storage path'), {
      target: { value: '/mnt/cloudlab-data' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Create server' }));

    await waitFor(() =>
      expect(api.createComputeServer).toHaveBeenCalledWith(
        expect.objectContaining({
          configuration: { project: 'FAST25-AE', profile: 'small-lan', host_key_source: 'none' },
        }),
      ),
    );
  });

  it.each([
    '',
    'data/sandboxes',
    '////',
    '/mnt/data//sandboxes',
    '/mnt/./data',
    '/mnt/../data',
    '/mnt/data disk',
    '/mnt/data;touch',
    '/mnt',
    '/etc',
    '/etc/sandboxes',
    '/var/lib',
    '/var/lib/docker',
    '/var/lib/docker/sandboxes',
    '/var/lib/containerd/sandboxes',
    '/var/lib/cloud-agent/sandboxes',
    '/var/lib/hybridinference/sandboxes',
    `/mnt/${'a'.repeat(4092)}`,
  ])('rejects invalid sandbox storage path %j before saving credentials', async (path) => {
    renderAdd(vi.fn(), catalog, []);
    fillSshForm();
    const storage = screen.getByLabelText('Sandbox storage path');
    fireEvent.change(storage, { target: { value: path } });
    fireEvent.change(screen.getByLabelText('SSH private key'), {
      target: { value: 'unsaved-private-key' },
    });
    fireEvent.submit(storage.closest('form')!);
    expect(await screen.findByRole('alert')).toHaveTextContent('Sandbox storage path');
    expect(storage).toHaveAttribute('aria-invalid', 'true');
    expect(storage).toHaveFocus();
    expect(api.saveComputeConnection).not.toHaveBeenCalled();
    expect(api.createComputeServer).not.toHaveBeenCalled();
    expect(api.getComputeOperations).not.toHaveBeenCalled();
  });

  it.each(['/', ' / '])(
    'rejects root storage path %j without falling back to defaults',
    async (path) => {
      renderAdd(vi.fn(), catalog, []);
      fillSshForm();
      const storage = screen.getByLabelText('Sandbox storage path');
      fireEvent.change(storage, { target: { value: path } });
      fireEvent.change(screen.getByLabelText('SSH private key'), {
        target: { value: 'unsaved-private-key' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
      expect(await screen.findByRole('alert')).toHaveTextContent(
        'Sandbox storage path must point to a dedicated mounted data disk outside system and service directories.',
      );
      expect(storage).toHaveAttribute('aria-invalid', 'true');
      expect(storage).toHaveFocus();
      expect(api.saveComputeConnection).not.toHaveBeenCalled();
      expect(api.createComputeServer).not.toHaveBeenCalled();
      expect(api.getComputeOperations).not.toHaveBeenCalled();
    },
  );

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
    fireEvent.change(screen.getByLabelText('Sandbox storage path'), {
      target: { value: '/mnt/cloudlab-data' },
    });
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
            host_key_source: 'none',
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

  it('rejects an overlong experiment name before saving credentials or submitting', async () => {
    const limited: ComputeCatalog = {
      ...catalog,
      providers: catalog.providers.map((provider) => ({
        ...provider,
        fields: [
          ...provider.fields,
          {
            key: 'name',
            label: 'CloudLab experiment name',
            type: 'text' as const,
            max_length: 16,
            advanced: true,
          },
        ],
      })),
    };
    renderAdd(vi.fn(), limited);
    fillSshForm();
    const input = screen.getByLabelText('CloudLab experiment name');
    expect(input).toHaveAttribute('maxlength', '16');
    fireEvent.change(input, { target: { value: 'cloud-agent-staging' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'CloudLab experiment name must be 16 characters or fewer.',
    );
    expect(input).toHaveFocus();
    expect(input.closest('details')).toHaveAttribute('open');
    expect(api.createComputeServer).not.toHaveBeenCalled();
    expect(api.saveComputeConnection).not.toHaveBeenCalled();
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
    expect(screen.getByRole('alert')).toHaveTextContent(
      'No server setup is configured. Configure it before this server can run sandboxes.',
    );
    expect(screen.getByRole('button', { name: 'Create server' })).toBeDisabled();
    fireEvent.click(screen.getByRole('radio', { name: 'Connect existing' }));
    fireEvent.change(screen.getByLabelText('Server name'), {
      target: { value: 'Observed experiment' },
    });
    fireEvent.change(screen.getByLabelText('Experiment ID'), { target: { value: 'experiment-a' } });
    fireEvent.change(screen.getByLabelText('CloudLab SSH user'), { target: { value: 'unused' } });
    fireEvent.change(screen.getByLabelText('Sandbox storage path'), {
      target: { value: '/mnt/unused-data' },
    });
    fireEvent.click(screen.getByText('Advanced options'));
    fireEvent.click(
      screen.getByRole('checkbox', { name: 'Only track this resource (skip server setup)' }),
    );
    expect(screen.queryByText(/No server setup is configured/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText('CloudLab SSH user')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('SSH private key')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Import resource' })).toBeEnabled();
    expect(screen.queryByLabelText('Sandbox storage path')).not.toBeInTheDocument();
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
    expect(vi.mocked(api.importComputeResource).mock.calls[0][0]).not.toHaveProperty(
      'sandbox_storage_path',
    );
    expect(api.createComputeServer).not.toHaveBeenCalled();
    expect(api.saveComputeConnection).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole('radio', { name: 'Create new' })).toBeEnabled());
    fireEvent.click(screen.getByRole('radio', { name: 'Create new' }));
    expect(screen.getByRole('button', { name: 'Create server' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('No server setup is configured.');
    expect(screen.queryByRole('button', { name: 'Import resource' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('SSH private key')).toBeInTheDocument();
    expect(screen.getByLabelText('Sandbox storage path')).toHaveValue('/mnt/unused-data');
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
      .mockResolvedValueOnce({ connection: cloudLabSshKey });
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
    expect(screen.queryByLabelText('Verified host keys')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Server name'), { target: { value: 'Research' } });
    fireEvent.change(screen.getByLabelText('Project'), { target: { value: 'FAST25' } });
    fireEvent.change(screen.getByLabelText('Profile'), { target: { value: 'small' } });
    fireEvent.change(screen.getByLabelText('Sandbox storage path'), {
      target: { value: '/mnt/cloudlab-data' },
    });
    fireEvent.change(screen.getByLabelText('Portal API token'), {
      target: { value: 'cloud-token' },
    });
    fireEvent.change(screen.getByLabelText('SSH private key'), { target: { value: 'ssh-key' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create server' }));
    await screen.findByText('SSH key rejected');
    expect(
      screen.getByText('CloudLab SSH host identity will not be verified.'),
    ).toBeInTheDocument();
    expect(
      screen.queryByText('Validate this connection before deployment.'),
    ).not.toBeInTheDocument();
    expect(api.saveComputeConnection).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({
        provider_id: 'existing_ssh',
        configuration: {},
        secret: 'ssh-key',
      }),
    );
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
    expect(screen.getByLabelText('Verified host keys')).toHaveValue('verified-public-key');
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
  it('shows the saved sandbox storage path in server details when configured', () => {
    const { rerender } = render(
      <ComputeResourcePanel
        allocation={allocation({ sandbox_storage_path: '/mnt/user-data' })}
        section="details"
        onSaved={vi.fn()}
        onOperation={vi.fn()}
      />,
    );
    expect(screen.getByText('Sandbox storage path').closest('div')).toHaveTextContent(
      '/mnt/user-data',
    );
    rerender(
      <ComputeResourcePanel
        allocation={allocation({ sandbox_storage_path: null })}
        section="details"
        onSaved={vi.fn()}
        onOperation={vi.fn()}
      />,
    );
    expect(screen.queryByText('Sandbox storage path')).not.toBeInTheDocument();
  });

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

  it('saves replacement timing and ordered node types without moving a node', async () => {
    const resource = allocation();
    vi.mocked(api.setComputePolicy).mockImplementation(async (_id, policy) => ({
      allocation: { ...resource, policy, revision: 3 },
    }));
    render(<ComputeResourcePanel allocation={resource} onSaved={vi.fn()} onOperation={vi.fn()} />);
    fireEvent.click(screen.getByLabelText('Request renewal automatically'));
    fireEvent.click(screen.getByLabelText(/Move to a new node when renewal/));
    fireEvent.change(screen.getByLabelText('Node types to use, newest first'), {
      target: { value: 'r6615, c6620  d6515' },
    });
    fireEvent.change(screen.getByLabelText('Renewal reason'), {
      target: { value: 'Keep evaluation sessions' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save renewal policy' }));
    await waitFor(() =>
      expect(api.setComputePolicy).toHaveBeenCalledWith(
        resource.id,
        expect.objectContaining({
          enabled: true,
          rotation_enabled: true,
          renew_before_seconds: 10800,
          max_node_age_seconds: 604800,
          node_types: ['r6615', 'c6620', 'd6515'],
        }),
        2,
        undefined,
      ),
    );
    expect(api.rotateComputeAllocation).not.toHaveBeenCalled();
  });

  it('disables replacement together with automatic renewal', async () => {
    const resource = allocation();
    resource.policy = {
      ...resource.policy,
      enabled: true,
      rotation_enabled: true,
      node_types: ['r6615'],
      reason: 'Keep evaluation sessions',
    };
    vi.mocked(api.setComputePolicy).mockImplementation(async (_id, policy) => ({
      allocation: { ...resource, policy, revision: 3 },
    }));
    render(<ComputeResourcePanel allocation={resource} onSaved={vi.fn()} onOperation={vi.fn()} />);
    fireEvent.click(screen.getByLabelText('Request renewal automatically'));
    expect(screen.getByLabelText(/Move to a new node when renewal/)).not.toBeChecked();
    expect(screen.getByLabelText(/Move to a new node when renewal/)).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Save renewal policy' }));
    await waitFor(() =>
      expect(api.setComputePolicy).toHaveBeenCalledWith(
        resource.id,
        expect.objectContaining({ enabled: false, rotation_enabled: false }),
        2,
        undefined,
      ),
    );
  });

  it('recovers a submitted replacement after the response is lost', async () => {
    vi.mocked(api.rotateComputeAllocation).mockRejectedValue(new Error('Connection lost'));
    const moved = operation({ kind: 'rotate', state: 'waiting' });
    vi.mocked(api.getComputeOperations).mockImplementation(async (requestId) => ({
      operations: [{ ...moved, request_id: requestId! }],
    }));
    const onOperation = vi.fn();
    render(
      <ComputeResourcePanel
        allocation={allocation({ allowed_actions: ['rotate'] })}
        onSaved={vi.fn()}
        onOperation={onOperation}
      />,
    );
    fireEvent.click(screen.getByText('Replace node now'));
    fireEvent.click(screen.getByRole('button', { name: 'Start replacement' }));
    await waitFor(() => expect(onOperation).toHaveBeenCalled());
    const request = vi.mocked(api.rotateComputeAllocation).mock.calls[0][1];
    expect(request.expected_revision).toBe(2);
    expect(api.getComputeOperations).toHaveBeenCalledWith(request.request_id);
    expect(onOperation).toHaveBeenCalledWith({ ...moved, request_id: request.request_id });
    expect(api.rotateComputeAllocation).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows the replacement status and suppresses unsupported actions', () => {
    render(
      <ComputeResourcePanel
        allocation={allocation({
          allowed_actions: [],
          rotation: {
            enabled: true,
            decide_at: '2026-10-10T10:00:00Z',
            replace_after: '2026-10-15T10:00:00Z',
            last: {
              id: 'move-a',
              state: 'blocked',
              step: 'selecting_candidate',
              message: 'No replacement node could be prepared.',
              updated_at: '2026-10-10T11:00:00Z',
              completed_at: null,
            },
          },
        })}
        onSaved={vi.fn()}
        onOperation={vi.fn()}
      />,
    );
    expect(screen.getByText(/Last move needs attention/)).toHaveTextContent(
      'No replacement node could be prepared.',
    );
    expect(screen.queryByRole('button', { name: 'Start replacement' })).not.toBeInTheDocument();
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
  it('opens auto-scaling without replacing the manual allocation flow', async () => {
    render(<ComputeResources onHostsChanged={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole('button', { name: 'Add server' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Auto-scaling' }));

    const dialog = screen.getByRole('dialog', { name: 'CloudLab auto-scaling' });
    expect(within(dialog).getByText('CloudLab auto-scaling controls')).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close panel' }));
    expect(screen.getByRole('button', { name: 'Add server' })).toBeEnabled();
  });

  it('shows only the newest periodic inspection for each server', async () => {
    vi.mocked(api.getComputeOperations).mockResolvedValue({
      operations: [
        operation({
          id: 'inspect-newest',
          kind: 'inspect',
          state: 'blocked',
          message: "CloudLab cannot confirm this experiment's existence or visibility",
          updated_at: '2026-10-04T08:05:00Z',
        }),
        operation({
          id: 'inspect-older',
          kind: 'inspect',
          state: 'blocked',
          message: "CloudLab cannot confirm this experiment's existence or visibility",
          updated_at: '2026-10-04T08:03:00Z',
        }),
        operation({ id: 'acquire-original', state: 'succeeded' }),
      ],
    });

    render(<ComputeResources onHostsChanged={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Activity' }));
    expect(screen.getByText('No active operations.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Show recent operations' }));
    expect(
      screen.getAllByRole('button', { name: /inspect · CloudLab experiment · blocked/ }),
    ).toHaveLength(1);
    expect(
      screen.getByRole('button', { name: /acquire · CloudLab experiment · succeeded/ }),
    ).toBeInTheDocument();
  });

  it('keeps failed operations with recovery actions in the default Activity view', async () => {
    vi.mocked(api.getComputeOperations).mockResolvedValue({
      operations: [
        operation({
          state: 'failed',
          message: 'CloudLab had no matching capacity',
          allowed_actions: ['retry', 'cancel'],
        }),
      ],
    });

    render(<ComputeResources onHostsChanged={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: /Activity.*1 active/ }));

    expect(
      screen.getByRole('button', { name: /acquire · CloudLab experiment · failed/ }),
    ).toBeInTheDocument();
  });

  it('keeps settled non-actionable operations in recent Activity only', async () => {
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
    // A drain nobody owns on this page reads as paused, never as `draining`.
    expect(within(oldServer).getByText('paused')).toBeInTheDocument();
    fireEvent.click(await screen.findByRole('button', { name: 'Activity' }));
    expect(screen.getByText('No active operations.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Show recent operations' }));
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

  it('hides released resources without letting their old host bindings conceal live inventory', async () => {
    const staleObservation = {
      code: 'observation_stale',
      message: 'Resource observation needs refresh.',
    };
    vi.mocked(api.getComputeServers).mockResolvedValue({
      allocations: [
        allocation({
          id: 'current-resource',
          name: 'Current CloudLab',
          external_id: 'current-experiment',
          expires_at: '2099-10-05T08:00:00Z',
        }),
        allocation({
          id: 'released-resource',
          name: 'Released CloudLab',
          state: 'terminated',
          external_id: 'released-experiment',
          expires_at: '2000-10-05T08:00:00Z',
          blocked_reasons: [staleObservation],
          nodes: [{ id: 'released-node', name: 'old node', hosts: ['old-host'] }],
        }),
        allocation({
          id: 'absent-resource',
          name: 'Absent CloudLab',
          state: 'absent',
          external_id: 'absent-experiment',
          expires_at: '2000-10-05T08:00:00Z',
          blocked_reasons: [staleObservation],
          nodes: [],
        }),
      ],
      hosts: [
        {
          host: 'old-host',
          status: 'online',
          retained_groups: 3,
          allocation_id: null,
          compute_node_id: null,
        },
      ],
      active_host: null,
      observed_at: '2026-10-04T08:00:00Z',
    });

    render(<ComputeResources onHostsChanged={vi.fn()} />);

    const table = await screen.findByRole('list', { name: 'Server inventory' });
    expect(within(table).getAllByRole('listitem')).toHaveLength(2);
    expect(screen.getByRole('listitem', { name: 'Current CloudLab' })).toBeInTheDocument();
    expect(screen.getByRole('listitem', { name: 'old-host' })).toBeInTheDocument();
    expect(screen.queryByRole('listitem', { name: 'Released CloudLab' })).not.toBeInTheDocument();
    expect(screen.queryByRole('listitem', { name: 'Absent CloudLab' })).not.toBeInTheDocument();

    const showReleased = screen.getByRole('button', { name: 'Show released (2)' });
    expect(showReleased).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(showReleased);

    const released = screen.getByRole('listitem', { name: 'Released CloudLab' });
    const absent = screen.getByRole('listitem', { name: 'Absent CloudLab' });
    expect(within(released).getByText('Released')).toBeInTheDocument();
    expect(within(absent).getByText('Disconnected')).toBeInTheDocument();
    expect(screen.queryByText(staleObservation.message)).not.toBeInTheDocument();
    expect(screen.queryByText('Deadline passed')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Hide released' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Hide released' }));
    expect(screen.queryByRole('listitem', { name: 'Released CloudLab' })).not.toBeInTheDocument();
    expect(screen.queryByRole('listitem', { name: 'Absent CloudLab' })).not.toBeInTheDocument();
  });

  it('describes an empty active inventory when every resource is released', async () => {
    vi.mocked(api.getComputeServers).mockResolvedValue({
      allocations: [
        allocation({
          id: 'released-resource',
          name: 'Released CloudLab',
          state: 'terminated',
        }),
      ],
      hosts: [],
      active_host: null,
      observed_at: '2026-10-04T08:00:00Z',
    });

    render(<ComputeResources onHostsChanged={vi.fn()} />);

    expect(
      await screen.findByText('No active servers. Released servers are hidden.'),
    ).toBeInTheDocument();
    expect(screen.queryByText('Add your first server to run sandboxes.')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Show released (1)' }));
    expect(screen.getByRole('listitem', { name: 'Released CloudLab' })).toBeInTheDocument();
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

  it('confirms permanent cleanup of a pending server and refreshes both inventories', async () => {
    const pending = allocation({
      name: 'Pending server',
      state: 'unknown',
      external_id: null,
      nodes: [],
      allowed_actions: [],
    });
    const onHostsChanged = vi.fn();
    const onInventory = vi.fn();
    vi.mocked(api.getComputeServers).mockResolvedValue({
      allocations: [pending],
      hosts: [],
      active_host: null,
      observed_at: '2026-10-04T08:00:00Z',
    });
    vi.mocked(api.removeComputeServer).mockImplementation(async () => {
      vi.mocked(api.getComputeServers).mockResolvedValue({
        allocations: [],
        hosts: [],
        active_host: null,
        observed_at: '2026-10-04T08:01:00Z',
      });
      return { removed: true };
    });

    render(<ComputeResources onHostsChanged={onHostsChanged} onInventory={onInventory} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Manage Pending server' }));
    const dialog = screen.getByRole('dialog', { name: 'Pending server' });
    expect(within(dialog).getByText('Pending acquisition')).toBeInTheDocument();
    fireEvent.click(within(dialog).getByLabelText('Server actions'));
    fireEvent.click(within(dialog).getByRole('menuitem', { name: 'Remove server' }));

    expect(within(dialog).getByText(/resource, node and Host records/)).toBeInTheDocument();
    expect(
      within(dialog).getByText(
        /Shared credentials, server setup, user conversations and run history remain/,
      ),
    ).toBeInTheDocument();
    const remove = within(dialog).getByRole('button', { name: 'Permanently remove server' });
    expect(remove).toBeDisabled();
    expect(api.removeComputeServer).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('checkbox', { name: /I confirm permanent removal/ }));
    fireEvent.click(remove);

    await waitFor(() => expect(api.removeComputeServer).toHaveBeenCalledWith('resource-a', 2));
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Pending server was removed from the server inventory.',
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByRole('listitem', { name: 'Pending server' })).not.toBeInTheDocument(),
    );
    expect(onHostsChanged).toHaveBeenCalledTimes(1);
    expect(onInventory).toHaveBeenLastCalledWith(expect.objectContaining({ allocations: [] }));
    expect(api.retireComputeAllocation).not.toHaveBeenCalled();
  });

  it('keeps a blocked server and displays its removal prerequisite inside confirmation', async () => {
    vi.mocked(api.removeComputeServer).mockRejectedValue(
      new APIError('resource_bound', 'Release this server before removing its records.', 409),
    );
    const onHostsChanged = vi.fn();
    render(<ComputeResources onHostsChanged={onHostsChanged} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Manage CloudLab experiment' }));
    const dialog = screen.getByRole('dialog', { name: 'CloudLab experiment' });
    fireEvent.click(within(dialog).getByLabelText('Server actions'));
    fireEvent.click(within(dialog).getByRole('menuitem', { name: 'Remove server' }));
    expect(within(dialog).getByText(/Release this cloud resource first/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('checkbox', { name: /I confirm permanent removal/ }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Permanently remove server' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      'Release this server before removing its records.',
    );
    expect(screen.getByRole('listitem', { name: 'CloudLab experiment' })).toBeInTheDocument();
    expect(onHostsChanged).not.toHaveBeenCalled();
    expect(within(dialog).getByRole('button', { name: 'Permanently remove server' })).toBeEnabled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(within(dialog).getByRole('button', { name: 'Overview' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(api.retireComputeAllocation).not.toHaveBeenCalled();
  });

  it('removes grouped unmanaged Host records and retries only records that remain', async () => {
    const hosts = ['old-host', 'kata-host'];
    vi.mocked(api.getComputeServers).mockResolvedValue({
      allocations: [],
      hosts: hosts.map((host) => ({
        host,
        status: 'disabled',
        retained_groups: 0,
        allocation_id: null,
        compute_node_id: null,
      })),
      active_host: null,
      observed_at: '2026-10-04T08:00:00Z',
    });
    vi.mocked(forgetAgentRunnerHost)
      .mockResolvedValueOnce({} as Awaited<ReturnType<typeof forgetAgentRunnerHost>>)
      .mockRejectedValueOnce(
        new APIError('resource_bound', 'Retained sandboxes block removal.', 409),
      )
      .mockResolvedValueOnce({} as Awaited<ReturnType<typeof forgetAgentRunnerHost>>);
    const onHostsChanged = vi.fn();
    render(
      <ComputeResources
        onHostsChanged={onHostsChanged}
        serverGroups={[{ name: 'Team server', members: hosts.map((host) => ({ host })) }]}
      />,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Manage Team server' }));
    const dialog = screen.getByRole('dialog', { name: 'Team server' });
    fireEvent.click(within(dialog).getByLabelText('Server actions'));
    fireEvent.click(within(dialog).getByRole('menuitem', { name: 'Remove server' }));
    expect(within(dialog).getByText(/Stop the host agent first/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole('checkbox', { name: /I confirm permanent removal/ }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Permanently remove server' }));

    expect(await within(dialog).findByRole('alert')).toHaveTextContent(
      '1 Host record(s) removed. Remaining records: Retained sandboxes block removal.',
    );
    expect(onHostsChanged).toHaveBeenCalledTimes(1);
    fireEvent.click(within(dialog).getByRole('button', { name: 'Permanently remove server' }));
    await waitFor(() => expect(forgetAgentRunnerHost).toHaveBeenCalledTimes(3));
    expect(vi.mocked(forgetAgentRunnerHost).mock.calls.map(([host]) => host)).toEqual([
      'old-host',
      'kata-host',
      'kata-host',
    ]);
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Team server was removed from the server inventory.',
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(onHostsChanged).toHaveBeenCalledTimes(2);
    expect(api.removeComputeServer).not.toHaveBeenCalled();
  });

  it('does not call a ready cloud resource online before its agent has enrolled', async () => {
    render(<ComputeResources onHostsChanged={vi.fn()} />);
    const row = await screen.findByRole('listitem', { name: /CloudLab experiment/ });
    expect(within(row).getByText('Awaiting agent')).toBeInTheDocument();
    expect(within(row).queryByText('Online')).not.toBeInTheDocument();
  });

  it('opens server setup from the missing-profile CloudLab callout', async () => {
    vi.mocked(api.getComputeProviders).mockResolvedValue({
      ...catalog,
      deployment_profiles: [],
    });
    render(<ComputeResources onHostsChanged={vi.fn()} />);
    const addServer = screen.getByRole('button', { name: 'Add server' });
    await waitFor(() => expect(addServer).toBeEnabled());
    fireEvent.click(addServer);
    const addDialog = screen.getByRole('dialog', { name: 'Add server' });
    fireEvent.click(within(addDialog).getByRole('radio', { name: /CloudLab/ }));
    fireEvent.change(within(addDialog).getByLabelText('Server name'), {
      target: { value: 'Preserved CloudLab server' },
    });
    expect(within(addDialog).getByRole('button', { name: 'Create server' })).toBeDisabled();
    expect(within(addDialog).getByRole('alert')).toHaveTextContent(
      'No server setup is configured. Configure it before this server can run sandboxes.',
    );
    const configureSetup = within(addDialog).getByRole('button', {
      name: 'Configure server setup',
    });
    fireEvent.click(configureSetup);
    expect(screen.queryByRole('dialog', { name: 'Add server' })).not.toBeInTheDocument();
    const setupDialog = screen.getByRole('dialog', { name: 'Server setup' });
    await waitFor(() =>
      expect(within(setupDialog).getByRole('heading', { name: 'Server setup' })).toHaveFocus(),
    );
    fireEvent.click(within(setupDialog).getByRole('button', { name: '← Back' }));
    const restoredDialog = screen.getByRole('dialog', { name: 'Add server' });
    const restoredConfigureSetup = within(restoredDialog).getByRole('button', {
      name: 'Configure server setup',
    });
    expect(restoredConfigureSetup).toBe(configureSetup);
    await waitFor(() => expect(restoredConfigureSetup).toHaveFocus());
    expect(within(restoredDialog).getByRole('radio', { name: /CloudLab/ })).toBeChecked();
    expect(within(restoredDialog).getByLabelText('Server name')).toHaveValue(
      'Preserved CloudLab server',
    );
    expect(api.createComputeServer).not.toHaveBeenCalled();
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
    fireEvent.change(within(dialog).getByLabelText('Sandbox storage path'), {
      target: { value: '/mnt/user-data' },
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

  it('treats a key-only SSH credential as valid only in its CloudLab server context', async () => {
    vi.mocked(api.getComputeConnections).mockResolvedValue({
      connections: [cloudLabSshKey, connections[1]],
    });
    vi.mocked(api.getComputeServers).mockResolvedValue({
      allocations: [allocation({ ssh_host_key_policy: 'unchecked' })],
      hosts: [],
      active_host: null,
      observed_at: '2026-10-04T08:00:00Z',
    });
    vi.mocked(api.saveComputeConnection).mockResolvedValue({
      connection: { ...cloudLabSshKey, name: 'Rotated key', revision: 2 },
    });
    render(<ComputeResources onHostsChanged={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Manage CloudLab experiment' }));
    const dialog = screen.getByRole('dialog', { name: 'CloudLab experiment' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Access settings' }));

    expect(
      within(dialog).getByText('SSH host-key checking is disabled for this CloudLab server.'),
    ).toBeInTheDocument();
    expect(
      within(dialog).queryByText('Validate this connection before deployment.'),
    ).not.toBeInTheDocument();
    expect(within(dialog).getAllByRole('button', { name: 'Check' })).toHaveLength(1);

    fireEvent.click(within(dialog).getByRole('button', { name: 'Edit SSH key' }));
    expect(within(dialog).getByLabelText('SSH private key')).toHaveValue('');
    expect(within(dialog).queryByLabelText('Verified host keys')).not.toBeInTheDocument();
    fireEvent.change(within(dialog).getByLabelText('Key name'), {
      target: { value: 'Rotated key' },
    });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save changes' }));
    await within(dialog).findByText('Access settings saved.');

    expect(api.saveComputeConnection).toHaveBeenCalledWith(
      expect.objectContaining({
        name: 'Rotated key',
        expected_revision: 1,
      }),
      'ssh-account',
    );
    expect(vi.mocked(api.saveComputeConnection).mock.calls[0][0]).not.toHaveProperty(
      'configuration',
    );
    expect(api.validateComputeConnection).not.toHaveBeenCalled();
  });

  it('keeps a key-only SSH credential strict when the allocation policy is unavailable', async () => {
    vi.mocked(api.getComputeConnections).mockResolvedValue({
      connections: [cloudLabSshKey, connections[1]],
    });

    render(<ComputeResources onHostsChanged={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Manage CloudLab experiment' }));
    const dialog = screen.getByRole('dialog', { name: 'CloudLab experiment' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Access settings' }));

    expect(
      within(dialog).getByText('Validate this connection before deployment.'),
    ).toBeInTheDocument();
    expect(within(dialog).getAllByRole('button', { name: 'Check' })).toHaveLength(2);

    fireEvent.click(within(dialog).getByRole('button', { name: 'Edit SSH key' }));

    expect(within(dialog).getByLabelText('Verified host keys')).toHaveValue('');
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

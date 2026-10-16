// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '@/lib/api/compute';
import type {
  CloudLabAutoscalingSnapshot,
  CloudLabPreferredNodesSnapshot,
  ComputeCatalog,
  ComputeConnection,
} from '@/lib/api/compute';
import { APIError } from '@/lib/utils/errors';

import CloudLabAutoscaling from './CloudLabAutoscaling';

vi.mock('@/lib/api/compute', () => ({
  getCloudLabAutoscaling: vi.fn(),
  getCloudLabPreferredNodes: vi.fn(),
  updateCloudLabAutoscaling: vi.fn(),
}));

const catalog: ComputeCatalog = {
  secret_store_configured: true,
  providers: [
    {
      id: 'cloudlab',
      label: 'CloudLab',
      modes: ['provision'],
      capabilities: {},
      connection_fields: [],
      fields: [
        {
          key: 'node_types',
          label: 'Preferred node types (top to bottom)',
          type: 'string_list',
          default: ['r6615', 'c6620', 'r650', 'r6525', 'c6525-100g', 'c6525-25g'],
        },
        {
          key: 'node_type_binding',
          label: 'Node type profile parameter',
          type: 'text',
          default: 'phystype',
        },
      ],
    },
  ],
  deployment_profiles: [
    { id: 'profile-a', label: 'Host A' },
    { id: 'profile-b', label: 'Host B' },
    { id: 'profile-spare', label: 'Rotation spare' },
  ],
};

const connections: ComputeConnection[] = [
  {
    id: 'cloudlab-account',
    name: 'FAST CloudLab',
    provider_id: 'cloudlab',
    configuration: {},
    status: 'verified',
    masked_summary: 'CloudLab account configured',
    revision: 1,
    blocked_reasons: [],
  },
  {
    id: 'ssh-key',
    name: 'CloudLab SSH key',
    provider_id: 'existing_ssh',
    configuration: {},
    status: 'verified',
    masked_summary: 'SSH key configured',
    revision: 1,
    blocked_reasons: [],
  },
];

const preferredNodes: CloudLabPreferredNodesSnapshot = {
  nodes: [
    {
      node_type: 'c6620',
      site: 'Utah',
      available: 54,
      total: 132,
      introduced_year: 2025,
      physical_cores: 28,
      logical_threads: 56,
      memory_gib: 128,
    },
    {
      node_type: 'c6525-25g',
      site: 'Utah',
      available: 0,
      total: 144,
      introduced_year: 2021,
      physical_cores: 16,
      logical_threads: 32,
      memory_gib: 128,
    },
  ],
  observed_at: '2026-10-11T12:00:00Z',
  source: 'CloudLab public hardware inventory',
  filters: {
    total_greater_than: 20,
    minimum_introduced_year: 2021,
    minimum_physical_cores: 16,
    minimum_memory_gib: 128,
    general_purpose_only: true,
  },
};

function autoscaling(over: Partial<CloudLabAutoscalingSnapshot> = {}): CloudLabAutoscalingSnapshot {
  return {
    policy: {
      revision: 3,
      enabled: false,
      min_hosts: 1,
      max_hosts: 2,
      connection_id: 'cloudlab-account',
      ssh_connection_id: 'ssh-key',
      configuration: {
        project: 'FAST25',
        group: 'research',
        profile_project: 'CloudLab',
        profile_name: 'cloud-agent',
        duration: 8,
        bindings: { phystype: 'm510' },
        node_types: ['r6615', 'c6620', 'r650', 'r6525', 'c6525-100g', 'c6525-25g'],
        node_type_binding: 'phystype',
        ssh_username: 'operator',
        host_key_source: 'registration',
      },
      deployment_profiles: ['profile-a', 'profile-b', 'profile-spare'],
      sandbox_storage_path: '/mnt/cloud-agent',
      renewal_policy: {
        enabled: true,
        renew_before_seconds: 3600,
        extend_to_horizon_seconds: 14400,
        stop_new_groups_before_seconds: 1800,
        stop_claims_before_seconds: 300,
        reason: 'Keep the staging pool available',
        rotation_enabled: false,
        max_node_age_seconds: 604800,
        node_types: ['m510'],
        node_type_binding: 'phystype',
      },
      slot_policy: {
        formula_version: 1,
        cpu_reservation_fraction: 0.75,
        cpu_per_slot: 2,
        memory_reserve_gib: 8,
        memory_per_slot_gib: 4,
        min_slots: 2,
        max_slots: 128,
      },
      scale_up_sustain_seconds: 120,
      scale_down_idle_seconds: 3600,
      cooldown_seconds: 600,
      scale_down_enabled: false,
    },
    runtime: {
      generation: 8,
      pressure_since: '2026-10-11T12:00:00Z',
      idle_since: null,
      cooldown_until: null,
      last_decision: { action: 'hold', reason: 'cooldown' },
      last_decision_at: '2026-10-11T12:01:00Z',
      blocker_code: null,
      blocker_message: null,
      updated_at: '2026-10-11T12:01:00Z',
      observed_at: '2026-10-11T12:01:00Z',
      queue_pressure: 2,
      slot_status: [
        {
          allocation_id: 'allocation-a',
          host: 'host-a',
          desired_slots: 6,
          applied_slots: 6,
          blocker_code: null,
        },
      ],
    },
    members: [
      {
        allocation_id: 'allocation-a',
        deployment_profile: 'profile-a',
        state: 'active',
        created_at: '2026-10-11T11:00:00Z',
        updated_at: '2026-10-11T12:01:00Z',
        host: 'host-a',
        slots_total: 6,
        slots_desired: 6,
        latest_operation_id: 'operation-a',
        latest_operation_kind: 'acquire',
        latest_operation_state: 'succeeded',
        latest_operation_message: 'Host is ready.',
        latest_acquire_operation_id: 'operation-a',
        latest_acquire_state: 'succeeded',
        latest_acquire_message: 'Host is ready.',
      },
    ],
    profile_reservations: [
      {
        deployment_profile: 'profile-a',
        allocation_id: 'allocation-a',
        created_at: '2026-10-11T11:00:00Z',
        updated_at: '2026-10-11T12:01:00Z',
      },
    ],
    ...over,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.getCloudLabAutoscaling).mockResolvedValue(autoscaling());
  vi.mocked(api.getCloudLabPreferredNodes).mockResolvedValue(preferredNodes);
  vi.mocked(api.updateCloudLabAutoscaling).mockImplementation(async (request) => {
    const current = autoscaling();
    return {
      ...current,
      policy: { ...request.policy, revision: request.expected_revision + 1 },
    };
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('CloudLab auto-scaling panel', () => {
  it('shows durable pool status, manual-allocation boundaries, and the fixed slot result', async () => {
    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    expect(await screen.findByText(/Manual servers stay independent/)).toBeInTheDocument();
    expect(screen.getByText(/2 waiting/)).toBeInTheDocument();
    expect(screen.getByText(/hold · cooldown/)).toBeInTheDocument();
    expect(screen.getByText(/6 applied \/ 6 desired/)).toBeInTheDocument();
    expect(screen.getByText(/Registered by each node at first boot/)).toBeInTheDocument();
    expect(screen.getByText(/Formula v1/)).toHaveTextContent('0.75 × logical cores');
    expect(screen.getByText(/Turning auto-scaling off only pauses/)).toBeInTheDocument();
    expect(screen.getByText(/Lease rotation is not available/)).toBeInTheDocument();
  });

  it('lists modern nodes with more than 20 total, including no free nodes, and pins one without dropping other bindings', async () => {
    const current = autoscaling();
    current.policy.configuration.bindings = {
      phystype: 'm510',
      dataMount: '/mydata',
    };
    vi.mocked(api.getCloudLabAutoscaling).mockResolvedValue(current);

    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    expect(await screen.findByText('c6620 · 54 free of 132')).toBeInTheDocument();
    expect(screen.getByText('c6525-25g · 0 free of 144')).toBeInTheDocument();
    expect(screen.getByText(/more than 20 total/)).toBeInTheDocument();
    expect(screen.getByLabelText('Preferred node types (top to bottom)')).toHaveValue(
      'r6615\nc6620\nr650\nr6525\nc6525-100g\nc6525-25g',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Use c6620' }));

    const bindings = screen.getByLabelText('Profile parameters (JSON object)');
    expect(JSON.parse(String((bindings as HTMLTextAreaElement).value))).toEqual({
      phystype: 'c6620',
      dataMount: '/mydata',
    });
    expect(screen.getByRole('button', { name: 'Use c6620' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );

    fireEvent.click(screen.getByRole('button', { name: 'Save auto-scaling' }));
    await waitFor(() => expect(api.updateCloudLabAutoscaling).toHaveBeenCalledTimes(1));
    expect(
      vi.mocked(api.updateCloudLabAutoscaling).mock.calls[0][0].policy.configuration.bindings,
    ).toEqual({ phystype: 'c6620', dataMount: '/mydata' });
  });

  it('saves the preferred list in order independently of current free inventory', async () => {
    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    const preferences = await screen.findByLabelText('Preferred node types (top to bottom)');
    fireEvent.change(preferences, {
      target: { value: 'c6525-25g, r650\nr6615\nc6525-25g' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save auto-scaling' }));

    await waitFor(() => expect(api.updateCloudLabAutoscaling).toHaveBeenCalledTimes(1));
    expect(
      vi.mocked(api.updateCloudLabAutoscaling).mock.calls[0][0].policy.configuration,
    ).toMatchObject({
      node_types: ['c6525-25g', 'r650', 'r6615'],
      node_type_binding: 'phystype',
    });
  });

  it('pins and restores ordered preferences using the configured profile parameter', async () => {
    const current = autoscaling();
    current.policy.configuration.node_type_binding = 'hardware';
    current.policy.configuration.bindings = {
      hardware: 'r650',
      phystype: 'legacy',
      dataMount: '/mydata',
    };
    vi.mocked(api.getCloudLabAutoscaling).mockResolvedValue(current);

    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    fireEvent.click(await screen.findByRole('button', { name: 'Use c6620' }));
    const bindings = screen.getByLabelText(
      'Profile parameters (JSON object)',
    ) as HTMLTextAreaElement;
    expect(JSON.parse(bindings.value)).toEqual({
      hardware: 'c6620',
      phystype: 'legacy',
      dataMount: '/mydata',
    });

    fireEvent.click(screen.getByRole('button', { name: 'Use ordered preferences' }));
    expect(JSON.parse(bindings.value)).toEqual({ phystype: 'legacy', dataMount: '/mydata' });
    expect(screen.getByRole('button', { name: 'Use c6620' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Save auto-scaling' }));

    await waitFor(() => expect(api.updateCloudLabAutoscaling).toHaveBeenCalledTimes(1));
    expect(
      vi.mocked(api.updateCloudLabAutoscaling).mock.calls[0][0].policy.configuration,
    ).toMatchObject({
      node_types: ['r6615', 'c6620', 'r650', 'r6525', 'c6525-100g', 'c6525-25g'],
      node_type_binding: 'hardware',
      bindings: { phystype: 'legacy', dataMount: '/mydata' },
    });
  });

  it('uses catalog preferences for older policies without marking the form edited', async () => {
    const current = autoscaling();
    delete current.policy.configuration.node_types;
    delete current.policy.configuration.node_type_binding;
    vi.mocked(api.getCloudLabAutoscaling).mockResolvedValue(current);
    const customized: ComputeCatalog = {
      ...catalog,
      providers: catalog.providers.map((provider) => ({
        ...provider,
        fields: provider.fields.map((field) => ({
          ...field,
          default: field.key === 'node_types' ? ['r650', 'c6620'] : 'hardware',
        })),
      })),
    };

    render(
      <CloudLabAutoscaling catalog={customized} connections={connections} activeHost={null} />,
    );

    expect(await screen.findByLabelText('Preferred node types (top to bottom)')).toHaveValue(
      'r650\nc6620',
    );
    expect(screen.getByLabelText('Node type profile parameter')).toHaveValue('hardware');
    expect(screen.getByRole('button', { name: 'Save auto-scaling' })).toBeDisabled();

    fireEvent.change(screen.getByLabelText('CloudLab project'), {
      target: { value: 'UPDATED-PROJECT' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save auto-scaling' }));

    await waitFor(() => expect(api.updateCloudLabAutoscaling).toHaveBeenCalledTimes(1));
    expect(
      vi.mocked(api.updateCloudLabAutoscaling).mock.calls[0][0].policy.configuration,
    ).toMatchObject({ node_types: ['r650', 'c6620'], node_type_binding: 'hardware' });
  });

  it('preserves an explicitly empty preferred list instead of replacing it with catalog defaults', async () => {
    const current = autoscaling();
    current.policy.configuration.node_types = [];
    vi.mocked(api.getCloudLabAutoscaling).mockResolvedValue(current);

    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    expect(await screen.findByLabelText('Preferred node types (top to bottom)')).toHaveValue('');
    fireEvent.change(screen.getByLabelText('CloudLab project'), {
      target: { value: 'UPDATED-PROJECT' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save auto-scaling' }));

    await waitFor(() => expect(api.updateCloudLabAutoscaling).toHaveBeenCalledTimes(1));
    expect(
      vi.mocked(api.updateCloudLabAutoscaling).mock.calls[0][0].policy.configuration.node_types,
    ).toEqual([]);
  });

  it('keeps policy editing available when live node availability fails', async () => {
    vi.mocked(api.getCloudLabPreferredNodes).mockRejectedValue(
      new APIError(
        'cloudlab_availability_unavailable',
        'CloudLab node availability is temporarily unavailable.',
        503,
      ),
    );

    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    expect(
      await screen.findByText(/Preferred-node availability is temporarily unavailable/),
    ).toBeInTheDocument();
    const project = screen.getByLabelText('CloudLab project');
    fireEvent.change(project, { target: { value: 'EDITED-WITHOUT-AVAILABILITY' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save auto-scaling' }));

    await waitFor(() => expect(api.updateCloudLabAutoscaling).toHaveBeenCalledTimes(1));
    expect(api.getCloudLabAutoscaling).toHaveBeenCalledTimes(1);
  });

  it('saves an unrelated disabled-policy edit from revision zero without inventing provider configuration', async () => {
    const empty = autoscaling();
    empty.policy = {
      ...empty.policy,
      revision: 0,
      enabled: false,
      min_hosts: 0,
      max_hosts: 0,
      connection_id: null,
      ssh_connection_id: null,
      configuration: {},
      deployment_profiles: [],
      sandbox_storage_path: null,
      renewal_policy: {
        ...empty.policy.renewal_policy,
        enabled: false,
        reason: '',
        rotation_enabled: false,
        node_types: [],
      },
    };
    empty.members = [];
    empty.profile_reservations = [];
    empty.runtime = {
      ...empty.runtime,
      pressure_since: null,
      queue_pressure: 0,
      slot_status: [],
    };
    vi.mocked(api.getCloudLabAutoscaling).mockResolvedValue(empty);

    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    const sustain = await screen.findByLabelText('Scale-up sustain (seconds)');
    const storagePath = screen.getByLabelText('Sandbox storage path (optional)');
    expect(storagePath).not.toBeRequired();
    fireEvent.change(storagePath, { target: { value: '   ' } });
    fireEvent.change(sustain, { target: { value: '180' } });
    const save = screen.getByRole('button', { name: 'Save auto-scaling' });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() => expect(api.updateCloudLabAutoscaling).toHaveBeenCalledTimes(1));
    const request = vi.mocked(api.updateCloudLabAutoscaling).mock.calls[0][0];
    expect(request).toMatchObject({
      expected_revision: 0,
      policy: {
        enabled: false,
        max_hosts: 0,
        connection_id: null,
        ssh_connection_id: null,
        configuration: {},
        deployment_profiles: [],
        sandbox_storage_path: null,
        scale_up_sustain_seconds: 180,
      },
    });
  });

  it('serializes a cleared optional storage path as null', async () => {
    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    const storagePath = await screen.findByLabelText('Sandbox storage path (optional)');
    fireEvent.change(storagePath, { target: { value: '   ' } });
    const save = screen.getByRole('button', { name: 'Save auto-scaling' });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() => expect(api.updateCloudLabAutoscaling).toHaveBeenCalledTimes(1));
    expect(
      vi.mocked(api.updateCloudLabAutoscaling).mock.calls[0][0].policy.sandbox_storage_path,
    ).toBeNull();
  });

  it('configures automatic renewal in minutes and keeps node rotation disabled', async () => {
    const withoutRenewal = autoscaling();
    withoutRenewal.policy.renewal_policy = {
      ...withoutRenewal.policy.renewal_policy,
      enabled: false,
      reason: '',
      rotation_enabled: false,
    };
    vi.mocked(api.getCloudLabAutoscaling).mockResolvedValue(withoutRenewal);

    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    const enabled = await screen.findByRole('checkbox', {
      name: /Request renewal automatically/,
    });
    expect(enabled).not.toBeChecked();
    expect(screen.queryByLabelText(/Move to a new node/)).not.toBeInTheDocument();
    fireEvent.click(enabled);
    expect(screen.getByRole('button', { name: 'Save auto-scaling' })).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Enter a reason before enabling automatic lease renewal.',
    );

    fireEvent.change(screen.getByLabelText('Renewal reason'), {
      target: { value: 'Keep the staging pool ready' },
    });
    fireEvent.change(screen.getByLabelText('Request before expiry (minutes)'), {
      target: { value: '120' },
    });
    fireEvent.change(screen.getByLabelText('Extend ahead of now (minutes)'), {
      target: { value: '360' },
    });
    fireEvent.change(screen.getByLabelText('Stop new sandboxes before expiry (minutes)'), {
      target: { value: '60' },
    });
    fireEvent.change(screen.getByLabelText('Stop all new turns before expiry (minutes)'), {
      target: { value: '30' },
    });
    const moveOut = screen.getByLabelText(
      'Move sandboxes to other servers before expiry (minutes, 0 = never)',
    );
    // A stored policy that predates the setting shows the server's default.
    expect(moveOut).toHaveValue(120);
    fireEvent.change(moveOut, { target: { value: '90' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save auto-scaling' }));

    await waitFor(() => expect(api.updateCloudLabAutoscaling).toHaveBeenCalledTimes(1));
    expect(
      vi.mocked(api.updateCloudLabAutoscaling).mock.calls[0][0].policy.renewal_policy,
    ).toMatchObject({
      enabled: true,
      reason: 'Keep the staging pool ready',
      renew_before_seconds: 7200,
      extend_to_horizon_seconds: 21600,
      stop_new_groups_before_seconds: 3600,
      stop_claims_before_seconds: 1800,
      move_out_before_seconds: 5400,
      rotation_enabled: false,
    });
  });

  it('blocks unordered lease-renewal timing', async () => {
    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    const horizon = await screen.findByLabelText('Extend ahead of now (minutes)');
    fireEvent.change(horizon, { target: { value: '60' } });

    expect(screen.getByRole('alert')).toHaveTextContent(
      'horizon > renewal lead > stop new sandboxes ≥ stop new turns',
    );
    expect(screen.getByRole('button', { name: 'Save auto-scaling' })).toBeDisabled();
    expect(api.updateCloudLabAutoscaling).not.toHaveBeenCalled();
  });

  it('prevents enablement while placement is pinned or profile capacity is insufficient', async () => {
    const withoutCapacity = autoscaling();
    withoutCapacity.policy.deployment_profiles = ['profile-a'];
    vi.mocked(api.getCloudLabAutoscaling).mockResolvedValue(withoutCapacity);

    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost="host-a" />);

    const enabled = await screen.findByRole('checkbox', {
      name: /Enable CloudLab auto-scaling/,
    });
    expect(enabled).toBeDisabled();
    expect(screen.getByText(/Placement is pinned/)).toBeInTheDocument();
    expect(screen.getByText(/Select at least 2 available deployment profiles/)).toBeInTheDocument();
  });

  it('pauses a legacy enabled pool with degraded credentials without adding inherited defaults', async () => {
    const active = autoscaling();
    active.policy.enabled = true;
    delete active.policy.configuration.node_types;
    delete active.policy.configuration.node_type_binding;
    vi.mocked(api.getCloudLabAutoscaling).mockResolvedValue(active);
    const degradedConnections = connections.map((connection) =>
      connection.id === 'cloudlab-account'
        ? { ...connection, status: 'unverified' as const }
        : connection,
    );

    render(
      <CloudLabAutoscaling catalog={catalog} connections={degradedConnections} activeHost={null} />,
    );

    const enabled = await screen.findByRole('checkbox', {
      name: /Enable CloudLab auto-scaling/,
    });
    expect(enabled).toBeChecked();
    expect(screen.getByLabelText('Preferred node types (top to bottom)')).toHaveValue(
      'r6615\nc6620\nr650\nr6525\nc6525-100g\nc6525-25g',
    );
    fireEvent.click(enabled);
    const save = screen.getByRole('button', { name: 'Save auto-scaling' });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() => expect(api.updateCloudLabAutoscaling).toHaveBeenCalledTimes(1));
    const { revision, ...policy } = active.policy;
    expect(vi.mocked(api.updateCloudLabAutoscaling).mock.calls[0][0]).toEqual({
      expected_revision: revision,
      policy: { ...policy, enabled: false },
    });
    expect(screen.getByLabelText('Preferred node types (top to bottom)')).toHaveValue(
      'r6615\nc6620\nr650\nr6525\nc6525-100g\nc6525-25g',
    );
  });

  it('requires verified credentials when preferences are edited while pausing a legacy pool', async () => {
    const active = autoscaling();
    active.policy.enabled = true;
    delete active.policy.configuration.node_types;
    delete active.policy.configuration.node_type_binding;
    vi.mocked(api.getCloudLabAutoscaling).mockResolvedValue(active);
    const degradedConnections = connections.map((connection) =>
      connection.id === 'cloudlab-account'
        ? { ...connection, status: 'unverified' as const }
        : connection,
    );

    render(
      <CloudLabAutoscaling catalog={catalog} connections={degradedConnections} activeHost={null} />,
    );

    fireEvent.change(await screen.findByLabelText('Preferred node types (top to bottom)'), {
      target: { value: 'c6620\nr650' },
    });
    fireEvent.click(screen.getByRole('checkbox', { name: /Enable CloudLab auto-scaling/ }));

    const save = screen.getByRole('button', { name: 'Save auto-scaling' });
    expect(save).toBeDisabled();
    expect(screen.getByText('Select a verified CloudLab account connection.')).toBeInTheDocument();
    fireEvent.click(save);
    expect(api.updateCloudLabAutoscaling).not.toHaveBeenCalled();
  });

  it('saves a revisioned reference-only policy and forces host-key registration', async () => {
    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    fireEvent.change(await screen.findByLabelText('Project group (optional)'), {
      target: { value: 'systems' },
    });
    fireEvent.click(screen.getByRole('checkbox', { name: /Enable CloudLab auto-scaling/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Save auto-scaling' }));

    await waitFor(() => expect(api.updateCloudLabAutoscaling).toHaveBeenCalledTimes(1));
    const request = vi.mocked(api.updateCloudLabAutoscaling).mock.calls[0][0];
    expect(request.expected_revision).toBe(3);
    expect(request.policy).toMatchObject({
      enabled: true,
      connection_id: 'cloudlab-account',
      ssh_connection_id: 'ssh-key',
      deployment_profiles: ['profile-a', 'profile-b', 'profile-spare'],
      configuration: {
        group: 'systems',
        host_key_source: 'registration',
        bindings: { phystype: 'm510' },
      },
    });
    expect(JSON.stringify(request)).not.toContain('private');
    expect(await screen.findByRole('status')).toHaveTextContent('settings saved');
  });

  it('preserves a draft on revision conflict and offers an explicit refresh', async () => {
    vi.mocked(api.updateCloudLabAutoscaling).mockRejectedValue(
      new APIError('revision_conflict', 'Settings changed in another tab.', 409),
    );
    const latest = autoscaling();
    latest.policy = {
      ...latest.policy,
      revision: 4,
      configuration: { ...latest.policy.configuration, project: 'SERVER-VALUE' },
    };
    vi.mocked(api.getCloudLabAutoscaling)
      .mockResolvedValueOnce(autoscaling())
      .mockResolvedValueOnce(latest);

    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);
    const project = await screen.findByLabelText('CloudLab project');
    fireEvent.change(project, { target: { value: 'MY-DRAFT' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save auto-scaling' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('another tab');
    expect(project).toHaveValue('MY-DRAFT');
    expect(screen.getByText(/Your draft is preserved/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Refresh from server' }));
    await waitFor(() => expect(project).toHaveValue('SERVER-VALUE'));
    expect(screen.getByText('Revision 4')).toBeInTheDocument();
  });
  it('treats revision-zero bindings as provider configuration and serializes them', async () => {
    const empty = autoscaling();
    empty.policy = {
      ...empty.policy,
      revision: 0,
      enabled: false,
      min_hosts: 0,
      max_hosts: 0,
      connection_id: null,
      ssh_connection_id: null,
      configuration: {},
      deployment_profiles: [],
      sandbox_storage_path: null,
      renewal_policy: {
        ...empty.policy.renewal_policy,
        enabled: false,
        reason: '',
        rotation_enabled: false,
        node_types: [],
      },
    };
    empty.members = [];
    empty.profile_reservations = [];
    vi.mocked(api.getCloudLabAutoscaling).mockResolvedValue(empty);

    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    fireEvent.change(await screen.findByLabelText('Profile parameters (JSON object)'), {
      target: { value: '{"phystype":"m510"}' },
    });
    const save = screen.getByRole('button', { name: 'Save auto-scaling' });
    expect(save).toBeDisabled();
    expect(screen.getByText('Select a verified CloudLab account connection.')).toBeInTheDocument();
    expect(screen.getByText('Select a verified SSH key connection.')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Verified CloudLab account'), {
      target: { value: 'cloudlab-account' },
    });
    fireEvent.change(screen.getByLabelText('Verified SSH key'), {
      target: { value: 'ssh-key' },
    });
    fireEvent.change(screen.getByLabelText('CloudLab project'), {
      target: { value: 'FAST25' },
    });
    fireEvent.change(screen.getByLabelText('Template project'), {
      target: { value: 'CloudLab' },
    });
    fireEvent.change(screen.getByLabelText('Template name'), {
      target: { value: 'cloud-agent' },
    });
    fireEvent.change(screen.getByLabelText('CloudLab SSH user'), {
      target: { value: 'operator' },
    });
    expect(save).toBeEnabled();
    fireEvent.click(save);

    await waitFor(() => expect(api.updateCloudLabAutoscaling).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.updateCloudLabAutoscaling).mock.calls[0][0]).toMatchObject({
      expected_revision: 0,
      policy: {
        configuration: {
          project: 'FAST25',
          profile_project: 'CloudLab',
          profile_name: 'cloud-agent',
          bindings: { phystype: 'm510' },
          ssh_username: 'operator',
          host_key_source: 'registration',
        },
      },
    });
  });

  it('disables every editable control while a save is pending', async () => {
    const write = deferred<CloudLabAutoscalingSnapshot>();
    vi.mocked(api.updateCloudLabAutoscaling).mockReturnValue(write.promise);

    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    const project = await screen.findByLabelText('CloudLab project');
    const bindings = screen.getByLabelText('Profile parameters (JSON object)');
    fireEvent.change(project, { target: { value: 'EDITED-BEFORE-SAVE' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save auto-scaling' }));

    await waitFor(() => expect(api.updateCloudLabAutoscaling).toHaveBeenCalledTimes(1));
    expect(project).toBeDisabled();
    expect(bindings).toBeDisabled();
    expect(screen.getByLabelText('Verified CloudLab account')).toBeDisabled();
    expect(screen.getByRole('checkbox', { name: /Allow guarded scale-down/ })).toBeDisabled();

    const saved = autoscaling();
    saved.policy = {
      ...saved.policy,
      revision: 4,
      configuration: { ...saved.policy.configuration, project: 'EDITED-BEFORE-SAVE' },
    };
    await act(async () => {
      write.resolve(saved);
      await write.promise;
    });

    expect(project).toBeEnabled();
    expect(project).toHaveValue('EDITED-BEFORE-SAVE');
    expect(screen.getByText('Revision 4')).toBeInTheDocument();
  });

  it('ignores a poll started before a successful save', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const initial = autoscaling();
    const stalePoll = deferred<CloudLabAutoscalingSnapshot>();
    vi.mocked(api.getCloudLabAutoscaling)
      .mockResolvedValueOnce(initial)
      .mockReturnValueOnce(stalePoll.promise);

    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    const project = await screen.findByLabelText('CloudLab project');
    fireEvent.change(project, { target: { value: 'SAVED-PROJECT' } });
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(api.getCloudLabAutoscaling).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole('button', { name: 'Save auto-scaling' }));
    expect(await screen.findByRole('status')).toHaveTextContent('settings saved');
    expect(project).toHaveValue('SAVED-PROJECT');
    expect(screen.getByText('Revision 4')).toBeInTheDocument();

    await act(async () => {
      stalePoll.resolve(initial);
      await stalePoll.promise;
    });

    expect(project).toHaveValue('SAVED-PROJECT');
    expect(screen.getByText('Revision 4')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('settings saved');
  });

  it('does not overwrite an edit when an older poll resolves in the same turn', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    const initial = autoscaling();
    const stalePoll = deferred<CloudLabAutoscalingSnapshot>();
    vi.mocked(api.getCloudLabAutoscaling)
      .mockResolvedValueOnce(initial)
      .mockReturnValueOnce(stalePoll.promise);

    render(<CloudLabAutoscaling catalog={catalog} connections={connections} activeHost={null} />);

    const project = await screen.findByLabelText('CloudLab project');
    await act(async () => vi.advanceTimersByTimeAsync(10_000));
    expect(api.getCloudLabAutoscaling).toHaveBeenCalledTimes(2);

    await act(async () => {
      fireEvent.change(project, { target: { value: 'UNSAVED-DRAFT' } });
      stalePoll.resolve(initial);
      await stalePoll.promise;
      await Promise.resolve();
    });

    expect(project).toHaveValue('UNSAVED-DRAFT');
  });
});

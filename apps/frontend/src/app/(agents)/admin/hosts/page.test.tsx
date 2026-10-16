// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// Setup notices have their own tests; this page uses a configured deployment.
vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: async () => ({
    revision: 1,
    definitions: [],
    values: {},
    secret_configured: {},
    bootstrap: [],
    setup: { required: false, groups: [], bootstrap_missing: [] },
  }),
}));

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ReactNode } from 'react';

import type { AgentRunnerHostPool } from '@/lib/api/admin';
import type { AgentAccessApi } from '@/lib/api/session';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({ role: 'admin', access: undefined as AgentAccessApi | undefined }));
const computeServerInventory = vi.hoisted(() => ({
  current: null as Record<string, unknown> | null,
}));
beforeEach(() => {
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  computeServerInventory.current = null;
});

vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: { user: { user_name: 'Ada', role: auth.role, agent_access: auth.access } },
  }),
}));

// Use the real permission check so denied cases cannot pass through a stub.
vi.mock('@/components/providers/AuthProvider', async () => {
  const actual = await vi.importActual<typeof import('@/components/providers/AuthProvider')>(
    '@/components/providers/AuthProvider',
  );
  return { hasAgentPermission: actual.hasAgentPermission };
});

vi.mock('@/lib/api/admin', () => ({
  getAgentRunnerHosts: vi.fn(),
  getAgentRunnerHostCapacityHistory: vi.fn(),
  setActiveAgentRunnerHost: vi.fn(),
  forgetAgentRunnerHost: vi.fn(),
  undrainAgentRunnerHost: vi.fn(),
  removeAgentRunnerHost: vi.fn(),
  setAgentRunnerHostSlots: vi.fn(),
  setAgentRunnerHostSettings: vi.fn(),
  HOST_CONCURRENCY_CAPABILITY: 'host-concurrency-v1',
  HOST_SETTINGS_CAPABILITY: 'host-settings-v1',
  MIN_HOST_SLOTS: 2,
  MAX_HOST_SLOTS: 128,
}));

import {
  forgetAgentRunnerHost,
  getAgentRunnerHostCapacityHistory,
  getAgentRunnerHosts,
  removeAgentRunnerHost,
  setActiveAgentRunnerHost,
  setAgentRunnerHostSettings,
  setAgentRunnerHostSlots,
  undrainAgentRunnerHost,
} from '@/lib/api/admin';

import { reasonLabel } from './capacity';
import { formatAge, runtimeMenu } from './format';
import AdminHostsPage from './page';

// Exercise the parent's Host commands and polling through its scoped render contract.
// ComputeResources.test verifies the real drawer selection and navigation.
vi.mock('./ComputeResources', async () => {
  const { useEffect } = await vi.importActual<typeof import('react')>('react');

  function MockComputeResources({
    renderHostControls,
    serverGroups,
    placementControl,
    onInventory,
  }: {
    placementControl?: ReactNode;
    renderHostControls: (hosts: string[]) => ReactNode;
    serverGroups?: { members: { host: string }[] }[];
    onInventory?: (snapshot: Record<string, unknown>) => void;
  }) {
    const inventorySnapshot = computeServerInventory.current;
    useEffect(() => {
      if (inventorySnapshot !== null) onInventory?.(inventorySnapshot);
    }, [inventorySnapshot, onInventory]);
    return (
      <section aria-label="Servers">
        {placementControl}
        {renderHostControls(
          (serverGroups ?? []).flatMap((server) => server.members.map((host) => host.host)),
        )}
      </section>
    );
  }

  return { default: MockComputeResources };
});

const mocked = {
  list: getAgentRunnerHosts as unknown as ReturnType<typeof vi.fn>,
  history: getAgentRunnerHostCapacityHistory as unknown as ReturnType<typeof vi.fn>,
  pin: setActiveAgentRunnerHost as unknown as ReturnType<typeof vi.fn>,
  undrain: undrainAgentRunnerHost as unknown as ReturnType<typeof vi.fn>,
  forget: forgetAgentRunnerHost as unknown as ReturnType<typeof vi.fn>,
  remove: removeAgentRunnerHost as unknown as ReturnType<typeof vi.fn>,
  slots: setAgentRunnerHostSlots as unknown as ReturnType<typeof vi.fn>,
  serverSettings: setAgentRunnerHostSettings as unknown as ReturnType<typeof vi.fn>,
};

const GIB = 1024 ** 3;
const RUNNER_A_EPOCH = '11111111-1111-4111-8111-111111111111';

function inventory(over: Record<string, unknown> = {}) {
  return {
    namespace: 'example-agent-kata',
    status: 'available',
    running: 3,
    stopped: 7,
    other_states: { created: 0, paused: 0, restarting: 0, removing: 0, dead: 0 },
    reason: null,
    unscoped_legacy_containers: 0,
    duplicate_sandbox_id: false,
    ...over,
  };
}

function capacity(over: Record<string, unknown> = {}) {
  return {
    schema_version: 1,
    source: 'linux_host_proc_and_docker_list',
    sample_id: 'sample-1',
    linux_boot_id: 'boot-a',
    cpu_percent: 37.24,
    cpu_logical_count: 32,
    cpu_window_seconds: 30,
    cpu_missing_reason: null,
    memory_total_bytes: 128 * GIB,
    memory_available_bytes: 86 * GIB,
    memory_used_bytes: 42 * GIB,
    memory_used_percent: 32.8,
    memory_missing_reason: null,
    // A measured storage block, so the Storage column has something to render
    // in the fixtures that do not override it. The byte figures are consistent
    // the way the control plane derives them: used = total - free, and the
    // percentage is used / (used + available).
    storage_total_bytes: 1000 * GIB,
    storage_free_bytes: 665 * GIB,
    storage_available_bytes: 650 * GIB,
    storage_used_bytes: 335 * GIB,
    storage_used_percent: 100 * (335 / (335 + 650)),
    storage_source: 'linux_overlay_root_statvfs',
    storage_target: '/mnt/agent-docker',
    storage_missing_reason: null,
    inventory: inventory(),
    received_at: '2026-09-11T12:34:31Z',
    sampled_at: '2026-09-11T12:34:01Z',
    sample_age_seconds: 12,
    freshness: 'fresh',
    ...over,
  };
}

function host(over: Partial<Record<string, unknown>> = {}) {
  return {
    host: 'runner-a',
    active: false,
    last_worker_id: 'worker-1',
    first_seen_at: '2026-08-01T00:00:00Z',
    last_seen_at: '2026-08-05T00:00:00Z',
    seconds_since_seen: 12,
    status: 'online',
    enrolled: true,
    labels: {},
    capabilities: ['kata', 'host-concurrency-v1'],
    slots_total: 4,
    slots_used: 0,
    slots_desired: null,
    launch_error: '',
    runner_version: '1.2.3',
    sandbox_manager_url: 'http://runner-a:8092',
    last_heartbeat_at: '2026-08-05T00:00:00Z',
    sandbox_runtime: 'io.containerd.kata.v2',
    sandbox_image_digest: 'freeinference-cloud-agent-job@sha256:abcdef0123456789',
    runtime_versions: { 'claude-code': '2.1.220' },
    runtime_versions_available: { 'claude-code': ['2.1.220', '2.1.235'] },
    capacity_epoch: RUNNER_A_EPOCH,
    capacity: null,
    ...over,
  };
}

function pool(
  hosts: unknown[],
  active: string | null = null,
  runtimeVersions: Record<string, string> = {},
): AgentRunnerHostPool {
  return {
    hosts: hosts as AgentRunnerHostPool['hosts'],
    active_host: active,
    runtime_versions: runtimeVersions,
    execution_profile: 'untrusted-kata-v1',
    observed_at: '2026-09-11T12:34:31Z',
  };
}

const at = (minute: number): string =>
  new Date(Date.UTC(2026, 8, 11, 12, minute, 30)).toISOString();

function historyPoint(minute: number, cpu: number | null, over: Record<string, unknown> = {}) {
  return {
    bucket_at: new Date(Date.UTC(2026, 8, 11, 12, minute, 0)).toISOString(),
    sampled_at: cpu === null ? null : at(minute),
    linux_boot_id: 'boot-a',
    cpu_percent: cpu,
    cpu_logical_count: 32,
    cpu_window_seconds: 30,
    cpu_missing_reason: null,
    memory_total_bytes: 128 * GIB,
    memory_available_bytes: 86 * GIB,
    memory_used_bytes: 42 * GIB,
    memory_used_percent: cpu === null ? null : 32.8,
    memory_missing_reason: null,
    ...over,
  };
}

function history(over: Record<string, unknown> = {}) {
  return {
    host: 'runner-a',
    capacity_epoch: RUNNER_A_EPOCH,
    since: new Date(Date.UTC(2026, 8, 11, 12, 0, 0)).toISOString(),
    until: new Date(Date.UTC(2026, 8, 11, 12, 5, 0)).toISOString(),
    observed_at: new Date(Date.UTC(2026, 8, 11, 12, 5, 40)).toISOString(),
    resolution_seconds: 60,
    semantics: 'sampled_observation',
    points: [0, 1, 2, 3, 4, 5].map((minute) => historyPoint(minute, 10 + minute * 10)),
    ...over,
  };
}

/** jsdom's `document.hidden` is read-only; the page reads it every poll. */
function setDocumentHidden(hidden: boolean): void {
  Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
}

/** The expanded panel for a server, by the id its toggle points at. */
function panelFor(name = 'runner-a'): HTMLElement {
  const panel = document.getElementById(`capacity-panel-${name}`);
  if (panel === null) throw new Error(`no open capacity panel for ${name}`);
  return panel;
}

/** Waits for the row, then opens it. `vi.waitFor` also works under fake timers. */
async function openPanel(name = 'runner-a'): Promise<void> {
  const target = (): HTMLElement =>
    screen.getByRole('button', { name: `Show details for ${name}` });
  await vi.waitFor(() => expect(target()).toBeInTheDocument());
  fireEvent.click(target());
}

function renderPage() {
  return render(<AdminHostsPage />);
}

describe('AdminHostsPage', () => {
  beforeEach(() => {
    auth.role = 'admin';
    Object.values(mocked).forEach((m) => m.mockReset());
    mocked.list.mockResolvedValue(pool([host()]));
    mocked.history.mockResolvedValue(history());
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
    setDocumentHidden(false);
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('refuses an admin with only agent.use without asking the control plane', async () => {
    auth.access = { allowed: true, permissions: ['agent.use'] };
    renderPage();

    expect(await screen.findByText('Admin access required')).toBeInTheDocument();
    // Not merely hidden: a page that renders the refusal *and* fetches has told
    // the operator no while doing the thing anyway.
    expect(mocked.list).not.toHaveBeenCalled();
  });

  it('keeps placement inside Servers and puts global configuration in Settings', async () => {
    renderPage();
    const servers = screen.getByRole('region', { name: 'Servers' });
    const location = within(servers).getByLabelText('New sandbox location');
    expect(screen.queryByRole('heading', { name: 'Global settings' })).not.toBeInTheDocument();
    const server = await screen.findByRole('rowheader', { name: 'runner-a' });
    expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute(
      'href',
      '/admin/settings',
    );
    expect(screen.queryByLabelText('Claude Code')).not.toBeInTheDocument();
    expect(screen.queryByText('Harness version')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Turn time limit for all servers')).not.toBeInTheDocument();
    expect(
      location.compareDocumentPosition(server) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('admits a free account granted Agent administration', async () => {
    auth.role = 'free';
    renderPage();
    expect(await screen.findByRole('rowheader', { name: 'runner-a' })).toBeInTheDocument();
    expect(mocked.list).toHaveBeenCalledTimes(1);
  });

  it('refuses an old admin profile without an explicit decision', async () => {
    auth.access = undefined;
    renderPage();
    expect(await screen.findByText('Admin access required')).toBeInTheDocument();
    expect(mocked.list).not.toHaveBeenCalled();
  });

  it('renders the pool a host that joined by polling would produce', async () => {
    // Every dynamic-pool field null/empty, which is exactly what a pre-Phase-G
    // row looks like. This must render rather than 500 the page an operator
    // opens to fix that host.
    mocked.list.mockResolvedValue(
      pool([
        host({
          host: 'legacy-box',
          enrolled: false,
          capabilities: [],
          slots_total: null,
          runner_version: null,
        }),
      ]),
    );
    renderPage();

    expect(await screen.findByText('legacy-box')).toBeInTheDocument();
    // No ceiling declared: the quota says so instead of inventing a free count.
    expect(screen.getByText('Limit not set')).toBeInTheDocument();
    expect(screen.queryByText(/free/)).not.toBeInTheDocument();
  });

  it('shows one server and only its Kata capacity without placement controls', async () => {
    mocked.list.mockResolvedValue(
      pool(
        [
          host({
            host: 'gpu-box',
            labels: { physical_host: 'gpu-box' },
            sandbox_runtime: 'runc',
            slots_total: 3,
          }),
          host({
            host: 'gpu-box-kata',
            labels: { physical_host: 'gpu-box' },
            active: true,
            slots_total: 3,
          }),
          host({ host: 'spark2', status: 'disabled', sandbox_runtime: null }),
        ],
        'gpu-box-kata',
      ),
    );
    renderPage();
    const row = (await screen.findByRole('rowheader', { name: 'gpu-box' })).closest('tr')!;
    expect(within(row).getByText('0 / 3 in use')).toBeInTheDocument();
    expect(within(row).getByText('3 free')).toBeInTheDocument();
    expect(screen.queryByText('Execution environment')).not.toBeInTheDocument();
    expect(screen.queryByText('Standard (runc)')).not.toBeInTheDocument();
    expect(screen.queryByText('pinned')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Any host' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Pin' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('New sandbox location')).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: 'Pause new work' })).not.toBeInTheDocument();
    mocked.remove.mockResolvedValue(pool([]));
    fireEvent.click(within(row).getByRole('button', { name: 'Remove from pool' }));
    await waitFor(() => expect(mocked.remove).toHaveBeenCalledWith('gpu-box-kata', false));
  });

  it('keeps the multi-server selector visible after choosing Automatic', async () => {
    const rows = [host({ active: true }), host({ host: 'runner-b' })];
    mocked.list.mockResolvedValue(pool(rows, 'runner-a'));
    mocked.pin.mockResolvedValue(pool([host(), host({ host: 'runner-b' })]));
    renderPage();
    const select = await screen.findByLabelText('New sandbox location');
    expect(select).toHaveValue('runner-a');
    fireEvent.change(select, { target: { value: '' } });
    await waitFor(() => expect(mocked.pin).toHaveBeenCalledWith(null));
    expect(screen.getByLabelText('New sandbox location')).toHaveValue('');
    expect(await screen.findByRole('status')).toHaveTextContent('assigned automatically');
  });

  it('selects a physical server using its enrolled executor identity', async () => {
    const rows = [host(), host({ host: 'gpu-box-kata', labels: { physical_host: 'gpu-box' } })];
    mocked.list.mockResolvedValue(pool(rows));
    mocked.pin.mockResolvedValue(pool(rows, 'gpu-box-kata'));
    renderPage();
    fireEvent.change(await screen.findByLabelText('New sandbox location'), {
      target: { value: 'gpu-box-kata' },
    });
    await waitFor(() => expect(mocked.pin).toHaveBeenCalledWith('gpu-box-kata'));
    expect(await screen.findByRole('status')).toHaveTextContent('Existing retained chats stay');
  });

  it('selects the exact Host when a physical server has multiple ready environments', async () => {
    const rows = [
      host({ host: 'gpu-kata-a', labels: { physical_host: 'gpu-box' } }),
      host({ host: 'gpu-kata-b', labels: { physical_host: 'gpu-box' } }),
    ];
    mocked.list.mockResolvedValue(pool(rows));
    mocked.pin.mockResolvedValue(pool(rows, 'gpu-kata-b'));
    renderPage();
    const select = await screen.findByLabelText('New sandbox location');
    await waitFor(() => expect(within(select).getAllByRole('option')).toHaveLength(3));
    fireEvent.change(select, { target: { value: 'gpu-kata-b' } });
    await waitFor(() => expect(mocked.pin).toHaveBeenCalledWith('gpu-kata-b'));
    expect(select).toHaveValue('gpu-kata-b');
  });

  it('allows clearing a stale selection even with only one available server', async () => {
    mocked.list.mockResolvedValue(
      pool([host(), host({ host: 'old', status: 'disabled', active: true })], 'old'),
    );
    mocked.pin.mockResolvedValue(pool([host()]));
    renderPage();
    fireEvent.click(await screen.findByRole('button', { name: 'Clear unavailable selection' }));
    await waitFor(() => expect(mocked.pin).toHaveBeenCalledWith(null));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(screen.getByLabelText('New sandbox location')).toBeInTheDocument();
  });

  it('keeps a fixed server visible if it becomes unavailable', async () => {
    mocked.list.mockResolvedValue(
      pool(
        [
          host({ status: 'offline', active: true }),
          host({ host: 'runner-b' }),
          host({ host: 'runner-c' }),
        ],
        'runner-a',
      ),
    );
    renderPage();
    expect(await screen.findByLabelText('New sandbox location')).toHaveValue('__unavailable__');
    expect(screen.getByRole('option', { name: 'Selected server unavailable' })).toBeDisabled();
  });

  it('counts busy servers as placement choices and excludes disabled servers', async () => {
    mocked.list.mockResolvedValue(
      pool([
        host({ slots_used: 4 }),
        host({ host: 'runner-b', slots_used: 4 }),
        host({ host: 'disabled', status: 'disabled' }),
      ]),
    );
    renderPage();
    const select = await screen.findByLabelText('New sandbox location');
    expect(within(select).getAllByRole('option')).toHaveLength(3);
    expect(within(select).queryByRole('option', { name: 'disabled' })).not.toBeInTheDocument();
  });

  it('removes a busy host once its turns finish, without asking or touching the pin', async () => {
    mocked.list.mockResolvedValue(pool([host({ slots_used: 2 })]));
    mocked.remove.mockResolvedValue(
      pool([host({ status: 'draining', removal_pending: true, slots_used: 2 })]),
    );
    const confirm = vi.fn();
    vi.stubGlobal('confirm', confirm);
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Remove from pool' }));

    await waitFor(() => expect(mocked.remove).toHaveBeenCalledWith('runner-a', false));
    expect(confirm).not.toHaveBeenCalled();
    expect(mocked.pin).not.toHaveBeenCalled();
    expect(await screen.findByText('removing')).toBeInTheDocument();
    expect(screen.getByText('2 running turn(s) left')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('is removed when its 2 running turn(s)');
    expect(screen.queryByText('draining')).not.toBeInTheDocument();
  });

  it('cancels a pending removal by putting the host back to work', async () => {
    mocked.list.mockResolvedValue(
      pool([host({ status: 'draining', removal_pending: true, slots_used: 1 })]),
    );
    mocked.undrain.mockResolvedValue(pool([host()]));
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Cancel removal' }));

    await waitFor(() => expect(mocked.undrain).toHaveBeenCalledWith('runner-a'));
    expect(await screen.findByRole('button', { name: 'Remove from pool' })).toBeInTheDocument();
  });

  it('names a deployment drain as an update and offers no way to undo it', async () => {
    mocked.list.mockResolvedValue(
      pool([host({ status: 'draining', drain_fence_active: true, slots_used: 1 })]),
    );
    renderPage();

    expect(await screen.findByText('updating')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Resume new work' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Cancel removal' })).not.toBeInTheDocument();
  });

  it('forgets only a disabled Host record after explaining the scope', async () => {
    mocked.list.mockResolvedValue(pool([host({ host: 'retired', status: 'disabled' })]));
    mocked.forget.mockResolvedValue(pool([]));
    const confirm = vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage();
    fireEvent.click(await screen.findByText('Unavailable servers and removed Host records'));
    fireEvent.click(await screen.findByRole('button', { name: 'Forget retired' }));
    await waitFor(() => expect(mocked.forget).toHaveBeenCalledWith('retired'));
    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining('leaves the machine and its files intact'),
    );
    expect(await screen.findByRole('status')).toHaveTextContent(
      'machine and its files were not deleted',
    );
  });

  it('keeps a record visible when retained dependencies block forgetting', async () => {
    mocked.list.mockResolvedValue(pool([host({ host: 'retired', status: 'disabled' })]));
    mocked.forget.mockRejectedValue(
      new APIError('resource_bound', 'Retained sandboxes block forgetting.', 409),
    );
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderPage();
    fireEvent.click(await screen.findByText('Unavailable servers and removed Host records'));
    fireEvent.click(await screen.findByRole('button', { name: 'Forget retired' }));
    expect(await screen.findByRole('status')).toHaveTextContent(
      'Retained sandboxes block forgetting.',
    );
    expect(screen.getByRole('button', { name: 'Forget retired' })).toBeInTheDocument();
  });

  it('removes a quiet host unforced', async () => {
    mocked.remove.mockResolvedValue(pool([]));
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Remove from pool' }));

    await waitFor(() => expect(mocked.remove).toHaveBeenCalledWith('runner-a', false));
  });

  it('makes the operator confirm before forcing a removing host out, and says why', async () => {
    mocked.list.mockResolvedValue(
      pool([host({ status: 'draining', removal_pending: true, slots_used: 2 })]),
    );
    const confirm = vi.fn().mockReturnValue(false);
    vi.stubGlobal('confirm', confirm);
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Remove now' }));

    expect(confirm).toHaveBeenCalledTimes(1);
    // The consequence has to be in the prompt: force ends the host's running
    // attempts now, requeueing them or marking a chat lost.
    expect(confirm.mock.calls[0][0]).toMatch(/Removing it now ends the attempts/);
    expect(mocked.remove).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    mocked.remove.mockResolvedValue(pool([]));
    fireEvent.click(screen.getByRole('button', { name: 'Remove now' }));
    await waitFor(() => expect(mocked.remove).toHaveBeenCalledWith('runner-a', true));
  });

  it('asks before removing a host whose sandboxes cannot move, and removes it only on yes', async () => {
    mocked.remove.mockRejectedValueOnce(
      new APIError('no_move_target', 'No other server can take the sandboxes', 409),
    );
    const confirm = vi.fn().mockReturnValue(false);
    vi.stubGlobal('confirm', confirm);
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Remove from pool' }));

    await waitFor(() => expect(confirm).toHaveBeenCalledTimes(1));
    expect(confirm.mock.calls[0][0]).toMatch(/cannot continue until it returns/);
    expect(mocked.remove).toHaveBeenCalledTimes(1);

    mocked.remove.mockRejectedValueOnce(
      new APIError('no_move_target', 'No other server can take the sandboxes', 409),
    );
    mocked.remove.mockResolvedValueOnce(pool([]));
    confirm.mockReturnValue(true);
    fireEvent.click(screen.getByRole('button', { name: 'Remove from pool' }));

    await waitFor(() => expect(mocked.remove).toHaveBeenCalledWith('runner-a', false, true));
  });

  it('says why sandboxes that never move would stay, and removes only on yes', async () => {
    mocked.remove.mockRejectedValueOnce(
      new APIError('cannot_move', 'Some sandboxes have no checkpoint', 409),
    );
    mocked.remove.mockResolvedValueOnce(pool([]));
    const confirm = vi.fn().mockReturnValue(true);
    vi.stubGlobal('confirm', confirm);
    renderPage();

    fireEvent.click(await screen.findByRole('button', { name: 'Remove from pool' }));

    await waitFor(() => expect(mocked.remove).toHaveBeenCalledWith('runner-a', false, true));
    expect(confirm.mock.calls[0][0]).toMatch(/a checkpoint failed, so they cannot move/);
  });

  it('shows the sandboxes a moving removal cannot move', async () => {
    mocked.list.mockResolvedValue(
      pool([
        host({
          status: 'draining',
          removal_pending: true,
          removal_moves_out: true,
          sandboxes_left: 0,
          sandboxes_unmovable: 2,
          slots_used: 0,
        }),
      ]),
    );
    renderPage();

    expect(
      await screen.findByText(
        'Moving out: 0 sandbox(es), 0 running turn(s) left; 2 cannot move, remove again to leave them',
      ),
    ).toBeInTheDocument();
  });

  it('shows how much of a removal that moves sandboxes out is left', async () => {
    mocked.list.mockResolvedValue(
      pool([
        host({
          status: 'draining',
          removal_pending: true,
          removal_moves_out: true,
          sandboxes_left: 3,
          slots_used: 1,
        }),
      ]),
    );
    renderPage();

    expect(
      await screen.findByText('Moving out: 3 sandbox(es), 1 running turn(s) left'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Remove now' })).toBeInTheDocument();
  });

  it('warns when the pinned host has gone quiet', async () => {
    mocked.list.mockResolvedValue(
      pool([host({ active: true, seconds_since_seen: 3600 })], 'runner-a'),
    );
    renderPage();

    expect(await screen.findByText(/may be unavailable/)).toBeInTheDocument();
  });

  it('does not warn when an unpinned host is quiet — nothing depends on it', async () => {
    mocked.list.mockResolvedValue(pool([host({ seconds_since_seen: 3600 })], null));
    renderPage();

    await screen.findByRole('rowheader', { name: 'runner-a' });
    expect(screen.queryByText(/the queue is stopped/)).not.toBeInTheDocument();
  });

  it('does not offer to enrol a host', async () => {
    // Removed on purpose, not lost. Enrolment binds a credential to a machine
    // so it cannot claim to be another one, and every runner in this pool is
    // ours — the control answered a question nobody operating this page asks.
    // The endpoint and its client are untouched for the rare new host.
    renderPage();

    await screen.findByRole('rowheader', { name: 'runner-a' });
    expect(screen.queryByRole('button', { name: 'Issue token' })).not.toBeInTheDocument();
    expect(screen.queryByText(/enrol/i)).not.toBeInTheDocument();
  });

  it('clears everything and refuses the page when access is revoked mid-session', async () => {
    mocked.list.mockRejectedValueOnce(new APIError('FORBIDDEN', 'revoked', 403));
    renderPage();

    expect(await screen.findByText('Admin access required')).toBeInTheDocument();
    expect(screen.queryByRole('rowheader', { name: 'runner-a' })).not.toBeInTheDocument();
  });
});

describe('capacity columns', () => {
  beforeEach(() => {
    auth.role = 'admin';
    Object.values(mocked).forEach((m) => m.mockReset());
    mocked.history.mockResolvedValue(history());
  });

  afterEach(cleanup);

  it('reports load, quota and retained sandboxes beside the status', async () => {
    mocked.list.mockResolvedValue(
      pool([
        host({
          slots_used: 2,
          slots_total: 3,
          capacity: capacity(),
        }),
      ]),
    );
    renderPage();

    const row = (await screen.findByRole('rowheader', { name: 'runner-a' })).closest('tr')!;
    expect(within(row).getByText('2 / 3 in use')).toBeInTheDocument();
    expect(within(row).getByText('1 free')).toBeInTheDocument();
    expect(within(row).getByText('37%')).toBeInTheDocument();
    expect(within(row).getByText('32 logical CPUs')).toBeInTheDocument();
    expect(within(row).getByText('33% · 42 / 128 GiB')).toBeInTheDocument();
    expect(within(row).getByText('86 GiB available')).toBeInTheDocument();
    // Storage sits between memory and the sandbox count, on the same
    // observation: 335 / (335 + 650) is 34.01%, and the table rounds it up
    // where the two figures beside it round to nearest.
    expect(within(row).getByText('35% · 335 / 1000 GiB')).toBeInTheDocument();
    expect(within(row).getByText('650 GiB available')).toBeInTheDocument();
    expect(within(row).getByText('3 running · 7 stopped')).toBeInTheDocument();
    // Sampling age and the poll-time column are both implementation details,
    // not fleet facts that need a permanent column.
    expect(within(row).queryByText('12s ago')).not.toBeInTheDocument();
    expect(screen.queryByText('Observed')).not.toBeInTheDocument();
    expect(screen.queryByText('Last polled')).not.toBeInTheDocument();
    // Each load figure carries a bar on the chart's own 0-100 scale, so the
    // fleet is read down the column without reading the numbers. The bar is
    // the unrounded reading, which is why it is not 37.
    expect(
      within(row).getByText('37%').closest('div')!.querySelector('span[aria-hidden] > span'),
    ).toHaveStyle({ width: '37.24%' });
    expect(
      within(row)
        .getByText('35% · 335 / 1000 GiB')
        .closest('div')!
        .querySelector('span[aria-hidden] > span'),
    ).toHaveStyle({ width: `${100 * (335 / 985)}%` });
  });

  it('names Storage in the header and spans the full width when a row opens', async () => {
    mocked.list.mockResolvedValue(pool([host({ capacity: capacity() })]));
    renderPage();
    await openPanel();

    const headers = screen.getAllByRole('columnheader').map((cell) => cell.textContent);
    expect(headers).toEqual([
      'Server',
      'Status',
      'Execution slots',
      'CPU',
      'Memory',
      'Storage',
      'Sandboxes',
      'Actions',
    ]);
    // The expanded band is one cell across all eight columns.
    expect(document.querySelectorAll('td[colspan]')).toHaveLength(1);
    expect(document.querySelector('td[colspan]')).toHaveAttribute('colspan', '8');
  });

  it('reads a control plane older than the storage block as not measured', async () => {
    // The page may ship before the collector does. An older control plane
    // omits the storage keys entirely, and the column has to say so rather
    // than print the 0% an undefined percentage would round to.
    const legacy = capacity();
    for (const key of Object.keys(legacy)) {
      if (key.startsWith('storage_')) delete (legacy as Record<string, unknown>)[key];
    }
    mocked.list.mockResolvedValue(pool([host({ capacity: legacy })]));
    renderPage();

    const row = (await screen.findByRole('rowheader', { name: 'runner-a' })).closest('tr')!;
    // CPU and memory are still measured; only Storage is missing.
    expect(within(row).getByText('33% · 42 / 128 GiB')).toBeInTheDocument();
    expect(within(row).getAllByText('Not measured').length).toBeGreaterThanOrEqual(1);
    expect(within(row).queryByText(/^0%/)).not.toBeInTheDocument();
    expect(within(row).queryByText('0 GiB available')).not.toBeInTheDocument();
  });

  it('asks a host for a number of turns and shows it as applying until the host confirms', async () => {
    mocked.list.mockResolvedValue(pool([host({ slots_total: 4, slots_used: 1 })]));
    mocked.slots.mockResolvedValue(
      pool([host({ slots_total: 4, slots_used: 1, slots_desired: 6 })]),
    );
    renderPage();

    const input = await screen.findByLabelText('Turns at once for runner-a');
    // Pre-filled with what the host runs, so Apply without typing asks for
    // what is already true rather than for a surprise.
    expect(input).toHaveValue(4);
    fireEvent.change(input, { target: { value: '6' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    await waitFor(() => expect(mocked.slots).toHaveBeenCalledWith('runner-a', 6));
    const row = (await screen.findByRole('rowheader', { name: 'runner-a' })).closest('tr')!;
    // The host's own number still bounds the ceiling until it confirms, and
    // the row says the saved one is on its way rather than in force.
    expect(await within(row).findByText('1 / 4 in use')).toBeInTheDocument();
    expect(within(row).getByText('3 free · applying 6')).toBeInTheDocument();
    // The row is the whole confirmation: no banner restates it, and the
    // mechanics (heartbeats, when raising applies) stay on hover, not on
    // the page.
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(within(row).queryByText(/next heartbeat/)).not.toBeInTheDocument();
    expect(within(row).getByText('1 / 4 in use').closest('div')).toHaveAttribute(
      'title',
      expect.stringContaining('next heartbeat'),
    );
  });

  it('keeps autoscaler-managed slot capacity visible without a manual editor', async () => {
    computeServerInventory.current = {
      allocations: [
        {
          id: 'resource-a',
          name: 'Autoscaled CloudLab',
          nodes: [{ hosts: ['runner-a'] }],
          blocked_reasons: [
            {
              code: 'autoscaling_managed',
              message: 'Lifecycle is managed by CloudLab autoscaling.',
            },
          ],
        },
      ],
      hosts: [{ host: 'runner-a', allocation_id: 'resource-a' }],
      active_host: null,
      observed_at: '2026-10-11T12:34:31Z',
    };
    mocked.list.mockResolvedValue(pool([host({ slots_total: 4, slots_used: 1 })]));
    renderPage();

    const explanation = await screen.findByText(
      'Slots are managed from fresh capacity by CloudLab auto-scaling.',
    );
    const row = screen.getByRole('rowheader', { name: 'runner-a' }).closest('tr')!;
    expect(within(row).getByText('1 / 4 in use')).toBeInTheDocument();
    expect(explanation).toBeInTheDocument();
    expect(within(row).queryByLabelText('Turns at once for runner-a')).not.toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: 'Apply' })).not.toBeInTheDocument();
    expect(mocked.slots).not.toHaveBeenCalled();
  });

  it('offers no reset: the saved number is simply the number the host has', async () => {
    mocked.list.mockResolvedValue(pool([host({ slots_total: 4, slots_desired: 3 })]));
    renderPage();

    expect(await screen.findByLabelText('Turns at once for runner-a')).toHaveValue(3);
    expect(screen.queryByRole('button', { name: 'Host default' })).not.toBeInTheDocument();
  });

  it('refuses a number outside the protocol bounds without asking the control plane', async () => {
    mocked.list.mockResolvedValue(pool([host()]));
    renderPage();

    const input = await screen.findByLabelText('Turns at once for runner-a');
    fireEvent.change(input, { target: { value: '1' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    fireEvent.change(input, { target: { value: '129' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));

    expect(mocked.slots).not.toHaveBeenCalled();
  });

  it('says when a saved number will never be applied', async () => {
    mocked.list.mockResolvedValue(
      pool([host({ capabilities: ['kata'], slots_total: 3, slots_desired: 6 })]),
    );
    renderPage();

    const row = (await screen.findByRole('rowheader', { name: 'runner-a' })).closest('tr')!;
    expect(within(row).getByText('0 / 3 in use')).toBeInTheDocument();
    expect(within(row).getByText('3 free · redeploy to change')).toBeInTheDocument();
    // The reason is a hover, not a paragraph under the field.
    expect(within(row).queryByText(/predates remote limits/)).not.toBeInTheDocument();
    expect(within(row).getByText('0 / 3 in use').closest('div')).toHaveAttribute(
      'title',
      expect.stringContaining('predates remote limits'),
    );
  });

  it('counts turns finishing above a limit that came down', async () => {
    mocked.list.mockResolvedValue(
      pool([host({ slots_total: 5, slots_used: 5, slots_desired: 2 })]),
    );
    renderPage();

    const row = (await screen.findByRole('rowheader', { name: 'runner-a' })).closest('tr')!;
    expect(within(row).getByText('5 / 2 in use')).toBeInTheDocument();
    expect(within(row).getByText('0 free · applying 2 · 3 finishing')).toBeInTheDocument();
  });

  it('shows why a host with room is claiming nothing', async () => {
    mocked.list.mockResolvedValue(
      pool([
        host({
          slots_total: 3,
          slots_used: 0,
          launch_error: '3 turns in a row could not be started (image is broken)',
        }),
      ]),
    );
    renderPage();

    const row = (await screen.findByRole('rowheader', { name: 'runner-a' })).closest('tr')!;
    expect(within(row).getByText('3 free · not claiming')).toBeInTheDocument();
    expect(await within(row).findByRole('alert')).toHaveTextContent('image is broken');
  });

  it('warns about a server running untrusted sandboxes without a disk quota', async () => {
    mocked.list.mockResolvedValue(
      pool([
        host({ host: 'unbounded', untrusted_without_disk_quota: true }),
        host({ host: 'bounded', untrusted_without_disk_quota: false }),
        // A control plane older than the warning sends no field at all.
        host({ host: 'older' }),
      ]),
    );
    renderPage();

    const unbounded = (await screen.findByRole('rowheader', { name: 'unbounded' })).closest('tr')!;
    const warning = within(unbounded).getByText('No container disk quota');
    expect(warning).toHaveAttribute('role', 'status');
    expect(warning.getAttribute('title')).toContain('Set Container disk above 0');
    for (const name of ['bounded', 'older']) {
      const row = screen.getByRole('rowheader', { name }).closest('tr')!;
      expect(within(row).queryByText('No container disk quota')).not.toBeInTheDocument();
    }
  });

  it('names the reason a value is missing instead of printing zero', async () => {
    mocked.list.mockResolvedValue(
      pool([
        host({
          host: 'warm',
          capacity: capacity({ cpu_percent: null, cpu_missing_reason: 'warming_up' }),
        }),
        host({ host: 'silent', capacity: null }),
      ]),
    );
    renderPage();

    const warm = (await screen.findByRole('rowheader', { name: 'warm' })).closest('tr')!;
    expect(within(warm).getByText('Warming up')).toBeInTheDocument();
    expect(within(warm).queryByText('0%')).not.toBeInTheDocument();
    // No reading, no bar: an empty track beside "Warming up" would draw the
    // zero this cell exists to refuse. The memory reading beside it is real,
    // so its bar is still there.
    expect(
      within(warm).getByText('Warming up').closest('div')!.querySelector('[aria-hidden]'),
    ).toBeNull();
    expect(
      within(warm).getByText('33% · 42 / 128 GiB').closest('div')!.querySelector('[aria-hidden]'),
    ).not.toBeNull();
    const silent = screen.getByRole('rowheader', { name: 'silent' }).closest('tr')!;
    // Both load cells say so, and the sandbox cell does not claim zero.
    expect(within(silent).getAllByText('Not measured').length).toBeGreaterThanOrEqual(2);
    expect(within(silent).queryByText('0 running · 0 stopped')).not.toBeInTheDocument();
  });

  it('uses the current source and never adds an expired source', async () => {
    // Two environments behind one physical name: one current, one expired.
    // The expired one contributes nothing to this row — its containers have
    // been stopped or started since it reported — so it must not be added.
    mocked.list.mockResolvedValue(
      pool([
        host({
          host: 'gpu-box',
          labels: { physical_host: 'gpu-box' },
          capacity: capacity({ sample_age_seconds: 12, freshness: 'fresh', cpu_percent: 5 }),
        }),
        host({
          host: 'gpu-box-legacy',
          labels: { physical_host: 'gpu-box' },
          sandbox_runtime: 'runc',
          capacity: capacity({
            sample_age_seconds: 400,
            freshness: 'stale',
            cpu_percent: 95,
            inventory: inventory({ namespace: 'example-agent-runc', running: 1, stopped: 2 }),
          }),
        }),
      ]),
    );
    renderPage();

    const row = (await screen.findByRole('rowheader', { name: 'gpu-box' })).closest('tr')!;
    // The freshest observation is what the machine's load cells show.
    expect(within(row).getByText('5%')).toBeInTheDocument();
    // The row still says that something on it has expired, and the count is a
    // lower bound rather than a total that includes an hour-old reading.
    expect(within(row).getByText('Capacity stale')).toBeInTheDocument();
    expect(within(row).getByText('≥10 observed · Partial')).toBeInTheDocument();
    expect(within(row).getByText(reasonLabel('source_stale'))).toBeInTheDocument();
    // The expired environment is still named, with its own last count.
    expect(within(row).queryByText('11 running · 9 stopped')).not.toBeInTheDocument();
  });

  it('renders a control plane that has never sent a capacity block', async () => {
    // The UI half can ship before the collector does. An older control plane
    // omits the keys entirely rather than sending nulls, and the page an
    // operator uses to fix a bad host must not be the page that breaks on it.
    const legacy = host({ host: 'legacy' });
    delete (legacy as Record<string, unknown>).capacity;
    delete (legacy as Record<string, unknown>).capacity_epoch;
    mocked.list.mockResolvedValue({
      hosts: [legacy],
      active_host: null,
      runtime_versions: {},
      execution_profile: 'untrusted-kata-v1',
    });
    renderPage();

    const row = (await screen.findByRole('rowheader', { name: 'legacy' })).closest('tr')!;
    // Both load cells say they have nothing, and neither says zero.
    expect(within(row).getAllByText('Not measured').length).toBeGreaterThanOrEqual(2);
    expect(within(row).queryByText('0%')).not.toBeInTheDocument();
    // The rest of the row is untouched: slots and actions still work.
    expect(within(row).getByText('0 / 4 in use')).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Remove from pool' })).toBeEnabled();
  });

  it('counts a renamed machine once, not as a partial of two records', async () => {
    // The shape staging had: the machine renamed to `-kata`, its previous row
    // left offline and never having reported an inventory. One machine, so the
    // count is a count — not a lower bound produced by a record of the same
    // box under a name it no longer answers to.
    mocked.list.mockResolvedValue(
      pool([
        host({
          host: 'rtx6000b-kata',
          labels: { physical_host: 'rtx6000b' },
          status: 'online',
          capacity: capacity({ inventory: inventory({ running: 0, stopped: 18 }) }),
        }),
        host({
          host: 'rtx6000b',
          labels: { physical_host: 'rtx6000b' },
          status: 'offline',
          capacity: null,
        }),
      ]),
    );
    renderPage();

    const row = (await screen.findByRole('rowheader', { name: 'rtx6000b' })).closest('tr')!;
    expect(within(row).getByText('0 running · 18 stopped')).toBeInTheDocument();
    expect(within(row).queryByText(/Partial/)).not.toBeInTheDocument();
  });

  it('says a partial count is a lower bound', async () => {
    mocked.list.mockResolvedValue(
      pool([
        host({
          capacity: capacity({
            inventory: inventory({ status: 'partial', stopped: null, reason: 'unknown_state' }),
          }),
        }),
      ]),
    );
    renderPage();

    const row = (await screen.findByRole('rowheader', { name: 'runner-a' })).closest('tr')!;
    expect(within(row).getByText('≥3 observed · Partial')).toBeInTheDocument();
    expect(within(row).getByText(/outside the fixed vocabulary/)).toBeInTheDocument();
  });
});

describe('capacity panel', () => {
  beforeEach(() => {
    auth.role = 'admin';
    Object.values(mocked).forEach((m) => m.mockReset());
    mocked.list.mockResolvedValue(pool([host({ capacity: capacity() })]));
    mocked.history.mockResolvedValue(history());
  });

  afterEach(() => {
    cleanup();
    setDocumentHidden(false);
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('opens one server at a time and closes again', async () => {
    mocked.list.mockResolvedValue(
      pool([
        host({ host: 'runner-a', labels: { physical_host: 'box-a' }, capacity: capacity() }),
        host({ host: 'runner-b', labels: { physical_host: 'box-b' }, capacity: capacity() }),
      ]),
    );
    renderPage();

    const toggleA = await screen.findByRole('button', { name: 'Show details for box-a' });
    const toggleB = screen.getByRole('button', { name: 'Show details for box-b' });
    expect(toggleA).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(toggleA);
    expect(screen.getByText('Capacity · box-a')).toBeInTheDocument();
    expect(toggleA).toHaveAttribute('aria-expanded', 'true');

    fireEvent.click(toggleB);
    expect(screen.queryByText('Capacity · box-a')).not.toBeInTheDocument();
    expect(screen.getByText('Capacity · box-b')).toBeInTheDocument();
    expect(toggleA).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(toggleB);
    expect(screen.queryByText('Capacity · box-b')).not.toBeInTheDocument();
  });

  it('does not show sampling implementation details around the charts', async () => {
    renderPage();
    await openPanel();

    await vi.waitFor(() =>
      expect(screen.getByRole('img', { name: 'CPU usage (%)' })).toBeInTheDocument(),
    );
    expect(screen.queryByText(/^Oldest observation:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Bucket:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Each point is one observation/)).not.toBeInTheDocument();
    expect(screen.queryByText(/CPU averages the interval/)).not.toBeInTheDocument();
  });

  it('does not repeat a healthy single-source sandbox count in the panel', async () => {
    renderPage();
    await openPanel();

    const row = (await screen.findByRole('rowheader', { name: 'runner-a' })).closest('tr')!;
    expect(within(row).getByText('3 running · 7 stopped')).toBeInTheDocument();

    const panel = panelFor();
    expect(within(panel).getByText('Retained sandboxes')).not.toBeVisible();
    expect(within(panel).getByText('example-agent-kata')).not.toBeVisible();
  });

  it('reads one hour by default and switches window on request', async () => {
    renderPage();
    await openPanel();

    await waitFor(() =>
      expect(mocked.history).toHaveBeenCalledWith('runner-a', '1h', expect.anything()),
    );
    expect(await screen.findByRole('img', { name: 'CPU usage (%)' })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Memory usage (%)' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Last 1h' })).toHaveAttribute('aria-pressed', 'true');

    mocked.history.mockResolvedValue(
      history({ points: [historyPoint(0, 88), historyPoint(1, 89)] }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Last 6h' }));

    await waitFor(() =>
      expect(mocked.history).toHaveBeenCalledWith('runner-a', '6h', expect.anything()),
    );
    // Only the CPU curve reached 89; the memory fixture stays where it was.
    expect((await screen.findAllByText(/Highest observed in range: 89.0%/)).length).toBe(1);
  });

  it('lets an operator read the other enrollment behind one physical server', async () => {
    // A physical row can be two deployments, and they are never added: each
    // keeps its own curve, and the operator says which one they meant.
    mocked.list.mockResolvedValue(
      pool([
        host({
          host: 'gpu-box-kata',
          labels: { physical_host: 'gpu-box' },
          capacity: capacity({ sample_age_seconds: 5 }),
        }),
        host({
          host: 'gpu-box-runc',
          labels: { physical_host: 'gpu-box' },
          sandbox_runtime: 'runc',
          capacity_epoch: '33333333-3333-4333-8333-333333333333',
          capacity: capacity({
            sample_age_seconds: 400,
            freshness: 'stale',
            cpu_percent: 95,
            inventory: inventory({ namespace: 'example-agent-runc', running: 1, stopped: 2 }),
          }),
        }),
      ]),
    );
    renderPage();
    await openPanel('gpu-box');

    // The default is the enrollment the row's own numbers came from.
    const switcher = await screen.findByLabelText('Curve source');
    expect(switcher).toHaveValue('gpu-box-kata');
    await waitFor(() =>
      expect(mocked.history).toHaveBeenCalledWith('gpu-box-kata', '1h', expect.anything()),
    );

    mocked.history.mockClear();
    mocked.history.mockResolvedValue(
      history({ host: 'gpu-box-runc', capacity_epoch: '33333333-3333-4333-8333-333333333333' }),
    );
    fireEvent.change(switcher, { target: { value: 'gpu-box-runc' } });

    await waitFor(() =>
      expect(mocked.history).toHaveBeenCalledWith('gpu-box-runc', '1h', expect.anything()),
    );
    // The curve is drawn under the window and generation it was asked for.
    expect((await screen.findAllByRole('img', { name: 'CPU usage (%)' })).length).toBeGreaterThan(
      0,
    );
  });

  it('discards a history answer that no longer matches the open panel', async () => {
    // A response for the previous window, carrying the previous generation,
    // lands after the operator has moved on. Drawing it would put one
    // machine's curve under another's title.
    mocked.history.mockImplementation(async (_host: string) =>
      history({ capacity_epoch: '22222222-2222-4222-8222-222222222222' }),
    );
    renderPage();
    await openPanel();

    await waitFor(() => expect(mocked.history).toHaveBeenCalled());
    // Nothing that arrived is shown, because nothing that arrived matches.
    await waitFor(() =>
      expect(screen.queryByText(/Highest observed in range/)).not.toBeInTheDocument(),
    );
    expect(
      await screen.findByText(/No history is available for this server yet/),
    ).toBeInTheDocument();
  });

  it('drops the cached curve and says so when the host re-enrolls', async () => {
    vi.useFakeTimers();
    let epoch = RUNNER_A_EPOCH;
    let cpu = 10;
    mocked.history.mockImplementation(async () =>
      history({
        capacity_epoch: epoch,
        points: [historyPoint(0, cpu), historyPoint(1, cpu)],
      }),
    );
    renderPage();
    await vi.waitFor(() =>
      expect(screen.getByRole('rowheader', { name: 'runner-a' })).toBeInTheDocument(),
    );
    await openPanel();
    await vi.waitFor(() =>
      expect(screen.getAllByText(/Highest observed in range: 10.0%/).length).toBe(1),
    );

    epoch = '33333333-3333-4333-8333-333333333333';
    cpu = 90;
    mocked.list.mockResolvedValue(pool([host({ capacity_epoch: epoch, capacity: capacity() })]));
    await vi.advanceTimersByTimeAsync(30_000);

    await vi.waitFor(() =>
      expect(screen.getByText(/history restarts from the new enrollment/)).toBeInTheDocument(),
    );
    await vi.waitFor(() =>
      expect(screen.getAllByText(/Highest observed in range: 90.0%/).length).toBe(1),
    );
  });

  it('polls the open panel and pauses both channels while the tab is hidden', async () => {
    vi.useFakeTimers();
    renderPage();
    await vi.waitFor(() =>
      expect(screen.getByRole('rowheader', { name: 'runner-a' })).toBeInTheDocument(),
    );
    await openPanel();
    await vi.waitFor(() => expect(mocked.history).toHaveBeenCalledTimes(1));
    const poolCalls = mocked.list.mock.calls.length;

    setDocumentHidden(true);
    await vi.advanceTimersByTimeAsync(90_000);
    expect(mocked.history).toHaveBeenCalledTimes(1);
    expect(mocked.list).toHaveBeenCalledTimes(poolCalls);

    // Becoming visible again refreshes immediately rather than waiting out the
    // rest of an interval that ran while nobody was looking.
    setDocumentHidden(false);
    fireEvent(document, new Event('visibilitychange'));
    await vi.waitFor(() => expect(mocked.history).toHaveBeenCalledTimes(2));
    expect(mocked.list.mock.calls.length).toBeGreaterThan(poolCalls);
  });

  it('keeps the enrollment it fell back to when nothing was measured yet', async () => {
    // Neither enrollment has reported capacity, so the panel opens on the
    // representative one. When the other starts reporting, the curve must not
    // follow it: the operator opened a server, not "whichever machine answers
    // next".
    vi.useFakeTimers();
    const onBox = (name: string, epoch: string, measured: boolean) =>
      host({
        host: name,
        labels: { physical_host: 'box' },
        capacity_epoch: epoch,
        capacity: measured ? capacity() : null,
      });
    mocked.list.mockResolvedValue(
      pool([onBox('box-a', 'epoch-a', false), onBox('box-b', 'epoch-b', false)]),
    );
    mocked.history.mockImplementation(async (name: string) =>
      history({ host: name, capacity_epoch: name === 'box-b' ? 'epoch-b' : 'epoch-a' }),
    );
    renderPage();
    await vi.waitFor(() =>
      expect(screen.getByRole('rowheader', { name: 'box' })).toBeInTheDocument(),
    );
    await openPanel('box');
    await vi.waitFor(() =>
      expect(mocked.history).toHaveBeenCalledWith('box-a', '1h', expect.anything()),
    );

    // box-b is the first to report; the choice made at open still stands.
    mocked.history.mockClear();
    mocked.list.mockResolvedValue(
      pool([onBox('box-a', 'epoch-a', false), onBox('box-b', 'epoch-b', true)]),
    );
    await vi.advanceTimersByTimeAsync(30_000);

    expect(mocked.history).toHaveBeenCalledWith('box-a', '1h', expect.anything());
    expect(mocked.history).not.toHaveBeenCalledWith('box-b', '1h', expect.anything());
  });

  it('adopts the generation of the source the operator switched to', async () => {
    // Switching sources is not a re-enrollment, and saying it is would tell an
    // operator their machine was rebuilt when they merely looked at the other
    // deployment on it.
    const onBox = (name: string, epoch: string, cpu: number) =>
      host({
        host: name,
        labels: { physical_host: 'box' },
        capacity_epoch: epoch,
        capacity: capacity({ cpu_percent: cpu }),
      });
    mocked.list.mockResolvedValue(
      pool([onBox('box-a', 'epoch-a', 11), onBox('box-b', 'epoch-b', 22)]),
    );
    mocked.history.mockImplementation(async (name: string) =>
      history({ host: name, capacity_epoch: name === 'box-b' ? 'epoch-b' : 'epoch-a' }),
    );
    renderPage();

    await openPanel('box');
    await vi.waitFor(() =>
      expect(mocked.history).toHaveBeenCalledWith('box-a', '1h', expect.anything()),
    );
    expect(screen.queryByText(/re-enrolled/)).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Curve source'), { target: { value: 'box-b' } });

    await waitFor(() =>
      expect(mocked.history).toHaveBeenCalledWith('box-b', '1h', expect.anything()),
    );
    expect(screen.queryByText(/re-enrolled/)).not.toBeInTheDocument();
  });

  it('does not offer a removed enrollment as a curve source', async () => {
    // One live environment and one that has been removed, sharing a physical
    // name. The retired row is not an alternative to switch to: showing it is
    // what turned this control into the server's own name with a suffix.
    mocked.list.mockResolvedValue(
      pool([
        host({
          host: 'rtx6000b-kata',
          labels: { physical_host: 'rtx6000b' },
          capacity: capacity(),
        }),
        host({
          host: 'rtx6000b',
          labels: { physical_host: 'rtx6000b' },
          status: 'disabled',
          capacity: null,
        }),
      ]),
    );
    renderPage();

    await openPanel('rtx6000b');

    // One enrollment is not a choice, so neither the control nor the identity
    // is shown: the heading above already names the server, and the enrollment
    // name differs from it only by a suffix.
    await vi.waitFor(() =>
      expect(mocked.history).toHaveBeenCalledWith('rtx6000b-kata', '1h', expect.anything()),
    );
    expect(screen.queryByLabelText('Curve source')).not.toBeInTheDocument();
    expect(screen.queryByText(/^Curve:/)).not.toBeInTheDocument();
  });

  it("shows one name when the other enrollment is the machine's old identity", async () => {
    // The shape staging had: the machine renamed to `-kata`, its previous row
    // left behind and marked offline by the reaper. One machine, one curve,
    // and no control asking which of two names for it to draw.
    mocked.list.mockResolvedValue(
      pool([
        host({
          host: 'rtx6000b-kata',
          labels: { physical_host: 'rtx6000b' },
          status: 'online',
          capacity: capacity(),
        }),
        host({
          host: 'rtx6000b',
          labels: { physical_host: 'rtx6000b' },
          status: 'offline',
          capacity: null,
        }),
      ]),
    );
    renderPage();

    await openPanel('rtx6000b');

    await vi.waitFor(() =>
      expect(mocked.history).toHaveBeenCalledWith('rtx6000b-kata', '1h', expect.anything()),
    );
    expect(screen.queryByLabelText('Curve source')).not.toBeInTheDocument();
    expect(screen.queryByText(/^Curve:/)).not.toBeInTheDocument();
  });

  it('still offers both while both are reporting', async () => {
    // Two stacks genuinely running on one box is the case the control exists
    // for; the rule above must not collapse it.
    mocked.list.mockResolvedValue(
      pool([
        host({ host: 'box-kata', labels: { physical_host: 'box' }, status: 'online' }),
        host({ host: 'box-runc', labels: { physical_host: 'box' }, status: 'draining' }),
      ]),
    );
    renderPage();

    await openPanel('box');

    await vi.waitFor(() => expect(screen.getByLabelText('Curve source')).toBeInTheDocument());
    expect(within(screen.getByLabelText('Curve source')).getAllByRole('option')).toHaveLength(2);
  });

  it('moves off a source that is removed while the panel is open', async () => {
    // The operator had selected the second environment; it is removed a poll
    // later. The panel must fall back to a live one rather than keep asking
    // for the history of an enrollment it no longer offers.
    vi.useFakeTimers();
    const onBox = (name: string, status: string, sampledAt: string) =>
      host({
        host: name,
        labels: { physical_host: 'box' },
        status,
        capacity: capacity({ sampled_at: sampledAt }),
      });
    mocked.list.mockResolvedValue(
      pool([
        onBox('box-a', 'online', '2026-09-11T12:34:30Z'),
        onBox('box-b', 'online', '2026-09-11T12:34:00Z'),
      ]),
    );
    mocked.history.mockImplementation(async (name: string) => history({ host: name }));
    renderPage();

    await openPanel('box');
    await vi.waitFor(() => expect(screen.getByLabelText('Curve source')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Curve source'), { target: { value: 'box-b' } });
    await vi.waitFor(() =>
      expect(mocked.history).toHaveBeenCalledWith('box-b', '1h', expect.anything()),
    );

    mocked.history.mockClear();
    mocked.list.mockResolvedValue(
      pool([
        onBox('box-a', 'online', '2026-09-11T12:34:30Z'),
        host({
          host: 'box-b',
          labels: { physical_host: 'box' },
          status: 'disabled',
          capacity: null,
        }),
      ]),
    );
    await vi.advanceTimersByTimeAsync(30_000);

    await vi.waitFor(() =>
      expect(mocked.history).toHaveBeenCalledWith('box-a', '1h', expect.anything()),
    );
    expect(mocked.history).not.toHaveBeenCalledWith('box-b', '1h', expect.anything());
    expect(screen.queryByLabelText('Curve source')).not.toBeInTheDocument();
  });

  it('stops polling a panel that has been closed', async () => {
    vi.useFakeTimers();
    renderPage();
    await vi.waitFor(() =>
      expect(screen.getByRole('rowheader', { name: 'runner-a' })).toBeInTheDocument(),
    );
    await openPanel();
    await vi.waitFor(() => expect(mocked.history).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: 'Hide details for runner-a' }));
    await vi.advanceTimersByTimeAsync(90_000);
    expect(mocked.history).toHaveBeenCalledTimes(1);
  });

  it('shows accumulating history, per-source counts and the reasons behind them', async () => {
    mocked.history.mockResolvedValue(
      history({
        points: [
          historyPoint(0, null, { sampled_at: null }),
          historyPoint(1, null, { sampled_at: null }),
          historyPoint(2, 20),
        ],
      }),
    );
    mocked.list.mockResolvedValue(
      pool([
        host({
          capacity: capacity({
            inventory: inventory({ status: 'partial', stopped: null, reason: 'unknown_state' }),
          }),
        }),
      ]),
    );
    renderPage();
    await openPanel();

    const panel = panelFor();
    expect((await screen.findAllByText(/History is accumulating/)).length).toBe(2);
    expect(within(panel).getByText('Retained sandboxes')).toBeVisible();
    expect(within(panel).getByText('≥3 observed · Partial')).toBeVisible();
    expect(within(panel).getByText(/outside the fixed vocabulary/)).toBeVisible();
    // The enrollment that contributed the count is named, not just summed.
    expect(within(panel).getByText('example-agent-kata')).toBeVisible();
  });
});

describe('polling and actions', () => {
  beforeEach(() => {
    auth.role = 'admin';
    Object.values(mocked).forEach((m) => m.mockReset());
    mocked.history.mockResolvedValue(history());
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('keeps the last good pool when a background refresh fails', async () => {
    vi.useFakeTimers();
    const clock = { ms: 1_000 };
    const spy = vi.spyOn(performance, 'now').mockImplementation(() => clock.ms);
    try {
      mocked.list.mockResolvedValueOnce(pool([host({ capacity: capacity() })]));
      renderPage();
      await vi.waitFor(() =>
        expect(screen.getByRole('rowheader', { name: 'runner-a' })).toBeInTheDocument(),
      );

      mocked.list.mockRejectedValue(new Error('gateway down'));
      clock.ms += 30_000;
      await vi.advanceTimersByTimeAsync(30_000);

      // Stale numbers are a smaller lie than an empty page — but unlabelled
      // stale numbers are the lie, so the failure is stated.
      expect(screen.getByRole('rowheader', { name: 'runner-a' })).toBeInTheDocument();
      expect(screen.getByText('37%')).toBeInTheDocument();
      expect(screen.queryByText('gateway down')).not.toBeInTheDocument();
      expect(screen.getByText(/Refresh failed/)).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });

  it('expires a reading locally when the pool goes quiet', async () => {
    // The server's `freshness` describes the moment it answered. Two minutes
    // of failing polls later the reading is 132 seconds old, and the page must
    // stop presenting it as current even though it no longer shows a live age.
    vi.useFakeTimers();
    const clock = { ms: 1_000 };
    const spy = vi.spyOn(performance, 'now').mockImplementation(() => clock.ms);
    try {
      mocked.list.mockResolvedValueOnce(
        pool([host({ capacity: capacity({ sample_age_seconds: 12, freshness: 'fresh' }) })]),
      );
      renderPage();
      await vi.waitFor(() =>
        expect(screen.getByRole('rowheader', { name: 'runner-a' })).toBeInTheDocument(),
      );
      expect(screen.queryByText('Capacity stale')).not.toBeInTheDocument();

      mocked.list.mockRejectedValue(new Error('gateway down'));
      clock.ms += 120_000;
      await vi.advanceTimersByTimeAsync(120_000);

      expect(screen.getByText('Capacity stale')).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });

  it('pins the curve source when the panel opens rather than following the newest reporter', async () => {
    // Two environments behind one physical name, both reporting. Left to pick
    // the newest fresh observation on every poll, the curve would swap between
    // them whenever they alternated — under one title, with no operator action
    // — and the epoch check would call the swap a re-enrollment.
    vi.useFakeTimers();
    // The source the row shows is the one sampled most recently, so the two
    // polls below swap which environment that is.
    const onBoxAt = (name: string, sampledAt: string, cpu: number) =>
      host({
        host: name,
        labels: { physical_host: 'box' },
        capacity: capacity({ sample_age_seconds: 5, cpu_percent: cpu, sampled_at: sampledAt }),
      });
    mocked.list.mockResolvedValue(
      pool([
        onBoxAt('box-a', '2026-09-11T12:34:30Z', 11),
        onBoxAt('box-b', '2026-09-11T12:34:00Z', 22),
      ]),
    );
    mocked.history.mockImplementation(async (name: string) => history({ host: name }));
    renderPage();

    await vi.waitFor(() =>
      expect(screen.getByRole('rowheader', { name: 'box' })).toBeInTheDocument(),
    );
    await openPanel('box');
    await vi.waitFor(() =>
      expect(mocked.history).toHaveBeenCalledWith('box-a', '1h', expect.anything()),
    );
    await vi.waitFor(() => expect(screen.getByLabelText('Curve source')).toHaveValue('box-a'));

    // box-b becomes the newest reporter on the next poll.
    mocked.history.mockClear();
    mocked.list.mockResolvedValue(
      pool([
        onBoxAt('box-a', '2026-09-11T12:30:00Z', 11),
        onBoxAt('box-b', '2026-09-11T12:34:30Z', 22),
      ]),
    );
    await vi.advanceTimersByTimeAsync(30_000);

    expect(screen.getByLabelText('Curve source')).toHaveValue('box-a');
    expect(mocked.history).toHaveBeenCalledWith('box-a', '1h', expect.anything());
    expect(mocked.history).not.toHaveBeenCalledWith('box-b', '1h', expect.anything());
  });

  it('does not let a poll issued before an action overwrite the action result', async () => {
    vi.useFakeTimers();
    mocked.list.mockResolvedValueOnce(pool([host()]));
    renderPage();
    await vi.waitFor(() =>
      expect(screen.getByRole('rowheader', { name: 'runner-a' })).toBeInTheDocument(),
    );

    // A poll starts and hangs.
    let landPoll: (value: AgentRunnerHostPool) => void = () => {};
    mocked.list.mockImplementationOnce(
      () =>
        new Promise<AgentRunnerHostPool>((resolve) => {
          landPoll = resolve;
        }),
    );
    await vi.advanceTimersByTimeAsync(30_000);

    // The action answers first.
    mocked.remove.mockResolvedValue(
      pool([host({ status: 'draining', removal_pending: true, slots_used: 1 })]),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Remove from pool' }));
    await vi.waitFor(() =>
      expect(screen.getByRole('button', { name: 'Cancel removal' })).toBeInTheDocument(),
    );

    // The older read lands afterwards and must be discarded.
    landPoll(pool([host()]));
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByRole('button', { name: 'Cancel removal' })).toBeInTheDocument();
  });
});

describe('formatting', () => {
  it('reports poll age in the largest unit that stays readable', () => {
    expect(formatAge(12)).toBe('12s ago');
    expect(formatAge(120)).toBe('2m ago');
    expect(formatAge(7200)).toBe('2h ago');
    expect(formatAge(172800)).toBe('2d ago');
  });
});

describe('version and image cells', () => {
  it('unions menus across hosts without losing the default ordering', () => {
    const menu = runtimeMenu([
      host({ runtime_versions_available: { 'claude-code': ['2.1.220'] } }),
      host({
        runtime_versions_available: { 'claude-code': ['2.1.220', '2.1.235'], pi: ['0.73.1'] },
      }),
    ] as never);
    expect(menu).toEqual([
      ['claude-code', ['2.1.220', '2.1.235']],
      ['pi', ['0.73.1']],
    ]);
  });
});

describe('runtime settings', () => {
  const definitions = [
    {
      key: 'agent_timeout_s',
      env: 'AGENT_TIMEOUT_S',
      label: 'Turn time limit',
      description: 'How long one agent turn may run before the host stops it.',
      unit: 'seconds',
      default: 3600,
      minimum: 60,
      maximum: 86400,
      scope: 'runner',
    },
    {
      key: 'terminal_max_sessions',
      env: 'AGENT_TERMINAL_MAX_SESSIONS',
      label: 'Terminals per server',
      description: 'How many interactive terminals one server may hold open at once.',
      unit: 'count',
      default: 64,
      minimum: 1,
      maximum: 4096,
      scope: 'sandbox_manager',
    },
  ] as AgentRunnerHostPool['host_setting_definitions'];

  function settingsHost(over: Partial<Record<string, unknown>> = {}) {
    return host({
      capabilities: ['kata', 'host-concurrency-v1', 'host-settings-v1'],
      settings_desired: {},
      settings_applied: { agent_timeout_s: 3600, terminal_max_sessions: 64 },
      settings_local: { agent_timeout_s: 3600, terminal_max_sessions: 64 },
      ...over,
    });
  }

  function settingsPool(hosts: unknown[], values: Record<string, number> = {}) {
    return { ...pool(hosts), host_settings: values, host_setting_definitions: definitions };
  }

  beforeEach(() => {
    auth.role = 'admin';
    Object.values(mocked).forEach((m) => m.mockReset());
    mocked.history.mockResolvedValue(history());
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it('is not offered by a control plane that defines no settings', async () => {
    mocked.list.mockResolvedValue(pool([host()]));
    renderPage();

    await screen.findByRole('rowheader', { name: 'runner-a' });
    expect(screen.queryByRole('heading', { name: 'Sandbox' })).not.toBeInTheDocument();
  });

  it('shows what a server uses and where from, and sets a value for it alone', async () => {
    mocked.list.mockResolvedValue(
      settingsPool(
        [settingsHost({ settings_applied: { agent_timeout_s: 7200, terminal_max_sessions: 64 } })],
        { agent_timeout_s: 7200 },
      ),
    );
    mocked.serverSettings.mockResolvedValue(
      settingsPool([settingsHost({ settings_desired: { terminal_max_sessions: 8 } })], {
        agent_timeout_s: 7200,
      }),
    );
    renderPage();
    await openPanel();

    const panel = screen.getByRole('region', { name: 'Sandbox settings for runner-a' });
    expect(within(panel).getByRole('link', { name: 'Settings' })).toHaveAttribute(
      'href',
      '/admin/settings',
    );
    expect(screen.queryByLabelText('Turn time limit for all servers')).not.toBeInTheDocument();
    const timeout = within(panel)
      .getByRole('rowheader', { name: 'Turn time limit' })
      .closest('tr')!;
    expect(within(timeout).getByText('2 h')).toBeInTheDocument();
    expect(within(timeout).getByText('set for all servers')).toBeInTheDocument();
    fireEvent.click(within(panel).getByText('Advanced'));
    const sessions = within(panel)
      .getByRole('rowheader', { name: 'Terminals per server' })
      .closest('tr')!;
    expect(within(sessions).getByText('deployment default')).toBeInTheDocument();

    const input = within(panel).getByLabelText('Terminals per server for runner-a');
    fireEvent.change(input, { target: { value: '8' } });
    fireEvent.submit(input.closest('form')!);

    await waitFor(() =>
      expect(mocked.serverSettings).toHaveBeenCalledWith('runner-a', {
        terminal_max_sessions: 8,
      }),
    );
    expect(
      await within(sessions).findByText('set for this server · applying 8'),
    ).toBeInTheDocument();
  });

  it('sets fractional container CPU for one server and resets it to the global value', async () => {
    const cpuDefinition = {
      key: 'sandbox_cpu_millicores',
      env: 'AGENT_SANDBOX_CPU_MILLICORES',
      label: 'Container CPU',
      description: 'CPU limit for new sandboxes. Existing sandboxes keep their limit.',
      unit: 'millicores',
      default: 2000,
      minimum: 100,
      maximum: 256000,
      scope: 'runner',
    } as const;
    const resourcePool = (settings: Record<string, number>) => ({
      ...settingsPool([
        settingsHost({
          settings_desired: settings,
          settings_applied: { sandbox_cpu_millicores: 2000 },
        }),
      ]),
      host_settings: { sandbox_cpu_millicores: 4000 },
      host_setting_definitions: [cpuDefinition],
    });
    mocked.list.mockResolvedValue(resourcePool({}));
    mocked.serverSettings.mockResolvedValueOnce(resourcePool({ sandbox_cpu_millicores: 1250 }));
    mocked.serverSettings.mockResolvedValueOnce(resourcePool({}));
    renderPage();
    await openPanel();

    const panel = screen.getByRole('region', { name: 'Sandbox settings for runner-a' });
    const input = within(panel).getByLabelText('Container CPU for runner-a');
    expect(input).toHaveAttribute('placeholder', '4');
    expect(within(panel).getByLabelText('Container CPU for runner-a unit')).toHaveValue('vCPU');
    expect(within(panel).getByText('2 vCPU')).toBeInTheDocument();
    fireEvent.change(input, { target: { value: '1.25' } });
    fireEvent.submit(input.closest('form')!);

    await waitFor(() =>
      expect(mocked.serverSettings).toHaveBeenCalledWith('runner-a', {
        sandbox_cpu_millicores: 1250,
      }),
    );
    expect(
      await within(panel).findByText('set for this server · applying 1.25 vCPU'),
    ).toBeInTheDocument();
    fireEvent.click(within(panel).getByRole('button', { name: 'Use default' }));
    await waitFor(() =>
      expect(mocked.serverSettings).toHaveBeenCalledWith('runner-a', {
        sandbox_cpu_millicores: null,
      }),
    );
    expect(
      await within(panel).findByText('set for all servers · applying 4 vCPU'),
    ).toBeInTheDocument();
    expect(within(panel).getByLabelText('Container CPU for runner-a')).toHaveAttribute(
      'placeholder',
      '4',
    );
  });

  it('offers the deployment default after reset even when the host reports an old environment value', async () => {
    mocked.list.mockResolvedValue(
      settingsPool([settingsHost({ settings_local: { agent_timeout_s: 999 } })]),
    );
    renderPage();
    await openPanel();
    const panel = screen.getByRole('region', { name: 'Sandbox settings for runner-a' });
    const input = within(panel).getByLabelText('Turn time limit for runner-a');
    expect(input).toHaveAttribute('placeholder', '1');
    expect(within(panel).getByLabelText('Turn time limit for runner-a unit')).toHaveValue('h');
    const timeout = within(panel)
      .getByRole('rowheader', { name: 'Turn time limit' })
      .closest('tr')!;
    expect(within(timeout).getByText('deployment default')).toBeInTheDocument();
    expect(within(timeout).queryByText(/applying/)).not.toBeInTheDocument();
  });

  it('says a server that predates settings needs a redeploy, and offers it no input', async () => {
    mocked.list.mockResolvedValue(
      settingsPool([
        settingsHost({ capabilities: ['kata'], settings_applied: null, settings_local: null }),
      ]),
    );
    renderPage();
    await openPanel();

    const panel = screen.getByRole('region', { name: 'Sandbox settings for runner-a' });
    expect(within(panel).getByText(/Redeploy it to manage them here/)).toBeInTheDocument();
    expect(within(panel).getByLabelText('Turn time limit for runner-a')).toBeDisabled();
  });
});

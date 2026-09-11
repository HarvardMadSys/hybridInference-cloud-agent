// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentRunnerHostPool } from '@/lib/api/admin';
import type { AgentAccessApi } from '@/lib/api/session';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({ role: 'admin', access: undefined as AgentAccessApi | undefined }));
beforeEach(() => {
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
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
  drainAgentRunnerHost: vi.fn(),
  undrainAgentRunnerHost: vi.fn(),
  removeAgentRunnerHost: vi.fn(),
  setAgentRuntimeVersion: vi.fn(),
}));

import {
  drainAgentRunnerHost,
  getAgentRunnerHostCapacityHistory,
  getAgentRunnerHosts,
  removeAgentRunnerHost,
  setActiveAgentRunnerHost,
  setAgentRuntimeVersion,
} from '@/lib/api/admin';

import { reasonLabel } from './capacity';
import { formatAge, runtimeMenu } from './format';
import AdminHostsPage from './page';

const mocked = {
  list: getAgentRunnerHosts as unknown as ReturnType<typeof vi.fn>,
  history: getAgentRunnerHostCapacityHistory as unknown as ReturnType<typeof vi.fn>,
  pin: setActiveAgentRunnerHost as unknown as ReturnType<typeof vi.fn>,
  drain: drainAgentRunnerHost as unknown as ReturnType<typeof vi.fn>,
  remove: removeAgentRunnerHost as unknown as ReturnType<typeof vi.fn>,
  setVersion: setAgentRuntimeVersion as unknown as ReturnType<typeof vi.fn>,
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
    capabilities: ['kata'],
    slots_total: 4,
    slots_used: 0,
    runner_version: '1.2.3',
    broker_url: 'http://runner-a:8092',
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
    screen.getByRole('button', { name: `Show capacity for ${name}` });
  await vi.waitFor(() => expect(target()).toBeInTheDocument());
  fireEvent.click(target());
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
    render(<AdminHostsPage />);

    expect(await screen.findByText('Admin access required')).toBeInTheDocument();
    // Not merely hidden: a page that renders the refusal *and* fetches has told
    // the operator no while doing the thing anyway.
    expect(mocked.list).not.toHaveBeenCalled();
  });

  it('admits a free account granted Agent administration', async () => {
    auth.role = 'free';
    render(<AdminHostsPage />);
    expect(await screen.findByText('runner-a')).toBeInTheDocument();
    expect(mocked.list).toHaveBeenCalledTimes(1);
  });

  it('refuses an old admin profile without an explicit decision', async () => {
    auth.access = undefined;
    render(<AdminHostsPage />);
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
    render(<AdminHostsPage />);

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
    render(<AdminHostsPage />);
    const row = (await screen.findByRole('rowheader', { name: 'gpu-box' })).closest('tr')!;
    expect(within(row).getByText('0 / 3 in use')).toBeInTheDocument();
    expect(within(row).getByText('3 free')).toBeInTheDocument();
    expect(screen.queryByText('Execution environment')).not.toBeInTheDocument();
    expect(screen.queryByText('Standard (runc)')).not.toBeInTheDocument();
    expect(screen.queryByText('pinned')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Any host' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Pin' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('New chats')).not.toBeInTheDocument();
    mocked.drain.mockResolvedValue(
      pool([
        host({ host: 'gpu-box-kata', labels: { physical_host: 'gpu-box' }, status: 'draining' }),
      ]),
    );
    fireEvent.click(within(row).getByRole('button', { name: 'Drain' }));
    await waitFor(() => expect(mocked.drain).toHaveBeenCalledWith('gpu-box-kata'));
  });

  it('keeps the multi-server selector visible after choosing Automatic', async () => {
    const rows = [host({ active: true }), host({ host: 'runner-b' })];
    mocked.list.mockResolvedValue(pool(rows, 'runner-a'));
    mocked.pin.mockResolvedValue(pool([host(), host({ host: 'runner-b' })]));
    render(<AdminHostsPage />);
    const select = await screen.findByLabelText('New chats');
    expect(select).toHaveValue('runner-a');
    fireEvent.change(select, { target: { value: '' } });
    await waitFor(() => expect(mocked.pin).toHaveBeenCalledWith(null));
    expect(screen.getByLabelText('New chats')).toHaveValue('');
    expect(await screen.findByRole('status')).toHaveTextContent('assigned automatically');
  });

  it('selects a physical server using its enrolled executor identity', async () => {
    const rows = [host(), host({ host: 'gpu-box-kata', labels: { physical_host: 'gpu-box' } })];
    mocked.list.mockResolvedValue(pool(rows));
    mocked.pin.mockResolvedValue(pool(rows, 'gpu-box-kata'));
    render(<AdminHostsPage />);
    fireEvent.change(await screen.findByLabelText('New chats'), { target: { value: 'gpu-box' } });
    await waitFor(() => expect(mocked.pin).toHaveBeenCalledWith('gpu-box-kata'));
    expect(await screen.findByRole('status')).toHaveTextContent('Existing retained chats stay');
  });

  it('allows clearing a stale selection even with only one available server', async () => {
    mocked.list.mockResolvedValue(
      pool([host(), host({ host: 'old', status: 'disabled', active: true })], 'old'),
    );
    mocked.pin.mockResolvedValue(pool([host()]));
    render(<AdminHostsPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Clear unavailable selection' }));
    await waitFor(() => expect(mocked.pin).toHaveBeenCalledWith(null));
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
    expect(screen.queryByLabelText('New chats')).not.toBeInTheDocument();
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
    render(<AdminHostsPage />);
    expect(await screen.findByLabelText('New chats')).toHaveValue('__unavailable__');
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
    render(<AdminHostsPage />);
    const select = await screen.findByLabelText('New chats');
    expect(within(select).getAllByRole('option')).toHaveLength(3);
    expect(within(select).queryByRole('option', { name: 'disabled' })).not.toBeInTheDocument();
  });

  it('drains without touching the pin', async () => {
    mocked.drain.mockResolvedValue(pool([host({ status: 'draining' })]));
    render(<AdminHostsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Drain' }));

    await waitFor(() => expect(mocked.drain).toHaveBeenCalledWith('runner-a'));
    expect(mocked.pin).not.toHaveBeenCalled();
    expect(await screen.findByRole('button', { name: 'Undrain' })).toBeInTheDocument();
  });

  it('removes a quiet host unforced', async () => {
    mocked.remove.mockResolvedValue(pool([]));
    render(<AdminHostsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }));

    await waitFor(() => expect(mocked.remove).toHaveBeenCalledWith('runner-a', false));
  });

  it('makes the operator confirm before forcing a busy host out, and says why', async () => {
    mocked.list.mockResolvedValue(pool([host({ slots_used: 2 })]));
    const confirm = vi.fn().mockReturnValue(false);
    vi.stubGlobal('confirm', confirm);
    render(<AdminHostsPage />);

    fireEvent.click(await screen.findByRole('button', { name: 'Remove' }));

    expect(confirm).toHaveBeenCalledTimes(1);
    // The consequence has to be in the prompt: force does not stop the
    // attempts, it leaves them to their leases.
    expect(confirm.mock.calls[0][0]).toMatch(/does not stop them/);
    expect(mocked.remove).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    mocked.remove.mockResolvedValue(pool([]));
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    await waitFor(() => expect(mocked.remove).toHaveBeenCalledWith('runner-a', true));
  });

  it('warns when the pinned host has gone quiet', async () => {
    mocked.list.mockResolvedValue(
      pool([host({ active: true, seconds_since_seen: 3600 })], 'runner-a'),
    );
    render(<AdminHostsPage />);

    expect(await screen.findByText(/may be unavailable/)).toBeInTheDocument();
  });

  it('does not warn when an unpinned host is quiet — nothing depends on it', async () => {
    mocked.list.mockResolvedValue(pool([host({ seconds_since_seen: 3600 })], null));
    render(<AdminHostsPage />);

    await screen.findByText('runner-a');
    expect(screen.queryByText(/the queue is stopped/)).not.toBeInTheDocument();
  });

  it('does not offer to enrol a host', async () => {
    // Removed on purpose, not lost. Enrolment binds a credential to a machine
    // so it cannot claim to be another one, and every runner in this pool is
    // ours — the control answered a question nobody operating this page asks.
    // The endpoint and its client are untouched for the rare new host.
    render(<AdminHostsPage />);

    await screen.findByText('runner-a');
    expect(screen.queryByRole('button', { name: 'Issue token' })).not.toBeInTheDocument();
    expect(screen.queryByText(/enrol/i)).not.toBeInTheDocument();
  });

  it('clears everything and refuses the page when access is revoked mid-session', async () => {
    mocked.list.mockRejectedValueOnce(new APIError('FORBIDDEN', 'revoked', 403));
    render(<AdminHostsPage />);

    expect(await screen.findByText('Admin access required')).toBeInTheDocument();
    expect(screen.queryByText('runner-a')).not.toBeInTheDocument();
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
    render(<AdminHostsPage />);

    const row = (await screen.findByRole('rowheader', { name: 'runner-a' })).closest('tr')!;
    expect(within(row).getByText('2 / 3 in use')).toBeInTheDocument();
    expect(within(row).getByText('1 free')).toBeInTheDocument();
    expect(within(row).getByText('37%')).toBeInTheDocument();
    expect(within(row).getByText('32 logical CPUs')).toBeInTheDocument();
    expect(within(row).getByText('33% · 42 / 128 GiB')).toBeInTheDocument();
    expect(within(row).getByText('86 GiB available')).toBeInTheDocument();
    expect(within(row).getByText('3 running · 7 stopped')).toBeInTheDocument();
    expect(within(row).getByText('12s ago')).toBeInTheDocument();
    // The poll-time column it replaced is gone.
    expect(screen.queryByText('Last polled')).not.toBeInTheDocument();
    // Each load figure carries a bar on the chart's own 0-100 scale, so the
    // fleet is read down the column without reading the numbers. The bar is
    // the unrounded reading, which is why it is not 37.
    expect(
      within(row).getByText('37%').closest('div')!.querySelector('span[aria-hidden] > span'),
    ).toHaveStyle({ width: '37.24%' });
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
    render(<AdminHostsPage />);

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

  it('ages only what the row shows, and never adds an expired source', async () => {
    // Two environments behind one physical name: one measured twelve seconds
    // ago, one an hour ago. The expired one contributes nothing to this row —
    // its containers have been stopped or started since it reported — so it
    // must be neither added to the count nor allowed to set the row's age.
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
    render(<AdminHostsPage />);

    const row = (await screen.findByRole('rowheader', { name: 'gpu-box' })).closest('tr')!;
    // The freshest observation is what the machine's load cells show, and it
    // is the only thing on the row whose age is being described.
    expect(within(row).getByText('5%')).toBeInTheDocument();
    expect(within(row).getByText('12s ago')).toBeInTheDocument();
    // The row still says that something on it has expired, and the count is a
    // lower bound rather than a total that includes an hour-old reading.
    expect(within(row).getByText('stale')).toBeInTheDocument();
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
    render(<AdminHostsPage />);

    const row = (await screen.findByRole('rowheader', { name: 'legacy' })).closest('tr')!;
    // Both load cells say they have nothing, and neither says zero.
    expect(within(row).getAllByText('Not measured').length).toBeGreaterThanOrEqual(2);
    expect(within(row).queryByText('0%')).not.toBeInTheDocument();
    // The rest of the row is untouched: slots and actions still work.
    expect(within(row).getByText('0 / 4 in use')).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Drain' })).toBeEnabled();
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
    render(<AdminHostsPage />);

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
    render(<AdminHostsPage />);

    const toggleA = await screen.findByRole('button', { name: 'Show capacity for box-a' });
    const toggleB = screen.getByRole('button', { name: 'Show capacity for box-b' });
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

  it('reads one hour by default and switches window on request', async () => {
    render(<AdminHostsPage />);
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
    render(<AdminHostsPage />);
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
    render(<AdminHostsPage />);
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
    render(<AdminHostsPage />);
    await vi.waitFor(() => expect(screen.getByText('runner-a')).toBeInTheDocument());
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
    render(<AdminHostsPage />);
    await vi.waitFor(() => expect(screen.getByText('runner-a')).toBeInTheDocument());
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
    render(<AdminHostsPage />);
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
    render(<AdminHostsPage />);

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
    render(<AdminHostsPage />);

    await openPanel('rtx6000b');

    await vi.waitFor(() => expect(screen.getByText('Curve: rtx6000b-kata')).toBeInTheDocument());
    expect(screen.queryByLabelText('Curve source')).not.toBeInTheDocument();
    await vi.waitFor(() =>
      expect(mocked.history).toHaveBeenCalledWith('rtx6000b-kata', '1h', expect.anything()),
    );
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
    render(<AdminHostsPage />);

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
    render(<AdminHostsPage />);
    await vi.waitFor(() => expect(screen.getByText('runner-a')).toBeInTheDocument());
    await openPanel();
    await vi.waitFor(() => expect(mocked.history).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole('button', { name: 'Hide capacity for runner-a' }));
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
    render(<AdminHostsPage />);
    await openPanel();

    const panel = panelFor();
    expect((await screen.findAllByText(/History is accumulating/)).length).toBe(2);
    expect(within(panel).getByText('Retained sandboxes')).toBeInTheDocument();
    expect(within(panel).getByText('≥3 observed · Partial')).toBeInTheDocument();
    expect(within(panel).getByText(/outside the fixed vocabulary/)).toBeInTheDocument();
    // The enrollment that contributed the count is named, not just summed.
    expect(within(panel).getByText('example-agent-kata')).toBeInTheDocument();
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

  it('keeps the last good pool when a background refresh fails, and its age keeps moving', async () => {
    vi.useFakeTimers();
    const clock = { ms: 1_000 };
    const spy = vi.spyOn(performance, 'now').mockImplementation(() => clock.ms);
    try {
      mocked.list.mockResolvedValueOnce(pool([host({ capacity: capacity() })]));
      render(<AdminHostsPage />);
      await vi.waitFor(() => expect(screen.getByText('runner-a')).toBeInTheDocument());
      expect(screen.getByText('12s ago')).toBeInTheDocument();

      mocked.list.mockRejectedValue(new Error('gateway down'));
      clock.ms += 30_000;
      await vi.advanceTimersByTimeAsync(30_000);

      // Stale numbers are a smaller lie than an empty page — but unlabelled
      // stale numbers are the lie, so the failure is stated.
      expect(screen.getByText('runner-a')).toBeInTheDocument();
      expect(screen.getByText('37%')).toBeInTheDocument();
      expect(screen.queryByText('gateway down')).not.toBeInTheDocument();
      expect(screen.getByText(/Refresh failed/)).toBeInTheDocument();

      // The age advances on local monotonic time; it never freezes at 12s.
      expect(screen.getByText('42s ago')).toBeInTheDocument();
      clock.ms += 5_000;
      await vi.advanceTimersByTimeAsync(5_000);
      expect(screen.getByText('47s ago')).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });

  it('expires a reading locally when the pool goes quiet', async () => {
    // The server's `freshness` describes the moment it answered. Two minutes
    // of failing polls later the reading is 132 seconds old, and a page that
    // kept calling it fresh — while the age beside it climbed — would be the
    // one combination that looks current and is not.
    vi.useFakeTimers();
    const clock = { ms: 1_000 };
    const spy = vi.spyOn(performance, 'now').mockImplementation(() => clock.ms);
    try {
      mocked.list.mockResolvedValueOnce(
        pool([host({ capacity: capacity({ sample_age_seconds: 12, freshness: 'fresh' }) })]),
      );
      render(<AdminHostsPage />);
      await vi.waitFor(() => expect(screen.getByText('runner-a')).toBeInTheDocument());
      expect(screen.queryByText('stale')).not.toBeInTheDocument();

      mocked.list.mockRejectedValue(new Error('gateway down'));
      clock.ms += 120_000;
      await vi.advanceTimersByTimeAsync(120_000);

      expect(screen.getByText('2m ago')).toBeInTheDocument();
      expect(screen.getByText('stale')).toBeInTheDocument();
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
    render(<AdminHostsPage />);

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
    render(<AdminHostsPage />);
    await vi.waitFor(() => expect(screen.getByText('runner-a')).toBeInTheDocument());

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
    mocked.drain.mockResolvedValue(pool([host({ status: 'draining' })]));
    fireEvent.click(screen.getByRole('button', { name: 'Drain' }));
    await vi.waitFor(() =>
      expect(screen.getByRole('button', { name: 'Undrain' })).toBeInTheDocument(),
    );

    // The older read lands afterwards and must be discarded.
    landPoll(pool([host()]));
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByRole('button', { name: 'Undrain' })).toBeInTheDocument();
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

describe('harness version selection', () => {
  beforeEach(() => {
    auth.role = 'admin';
    Object.values(mocked).forEach((m) => m.mockReset());
    mocked.list.mockResolvedValue(pool([host()]));
    mocked.history.mockResolvedValue(history());
  });

  afterEach(cleanup);

  it('offers every version the pool holds, with the image default first', async () => {
    render(<AdminHostsPage />);

    const select = (await screen.findByLabelText('claude-code')) as HTMLSelectElement;
    expect([...select.options].map((o) => o.textContent)).toEqual([
      'Image default',
      '2.1.220',
      '2.1.235',
    ]);
    // Nothing selected means the image default — not "2.1.220 happens to be
    // first". The two differ the moment a host is rebuilt.
    expect(select.value).toBe('');
  });

  it('shows the deployment-wide selection, not a per-host value', async () => {
    mocked.list.mockResolvedValue(pool([host()], null, { 'claude-code': '2.1.235' }));
    render(<AdminHostsPage />);

    const select = (await screen.findByLabelText('claude-code')) as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe('2.1.235'));
  });

  it('sends the choice and takes the whole pool back', async () => {
    mocked.setVersion.mockResolvedValue(pool([host()], null, { 'claude-code': '2.1.235' }));
    render(<AdminHostsPage />);

    const select = await screen.findByLabelText('claude-code');
    fireEvent.change(select, { target: { value: '2.1.235' } });

    await waitFor(() => expect(mocked.setVersion).toHaveBeenCalledWith('claude-code', '2.1.235'));
    // The wording is the contract with the operator: this is not retroactive,
    // and a page that implied otherwise would be the bug.
    expect(
      await screen.findByText(/New claude-code threads will run 2\.1\.235/),
    ).toBeInTheDocument();
  });

  it('clears back to the image default with null, not an empty string', async () => {
    mocked.list.mockResolvedValue(pool([host()], null, { 'claude-code': '2.1.235' }));
    mocked.setVersion.mockResolvedValue(pool([host()]));
    render(<AdminHostsPage />);

    const select = await screen.findByLabelText('claude-code');
    fireEvent.change(select, { target: { value: '' } });

    await waitFor(() => expect(mocked.setVersion).toHaveBeenCalledWith('claude-code', null));
  });

  it('hides the selector when no host reports a menu', async () => {
    // A pool of pre-menu hosts. An empty control that cannot do anything is
    // worse than no control: it reads as broken rather than as absent.
    mocked.list.mockResolvedValue(pool([host({ runtime_versions_available: null })]));
    render(<AdminHostsPage />);

    await screen.findByText('runner-a');
    expect(screen.queryByText('Harness version')).not.toBeInTheDocument();
  });
});

describe('the harness section reads as a choice, not a list', () => {
  beforeEach(() => {
    auth.role = 'admin';
    Object.values(mocked).forEach((m) => m.mockReset());
    mocked.list.mockResolvedValue(pool([host()]));
    mocked.history.mockResolvedValue(history());
  });

  afterEach(cleanup);

  it('is the runtimes and their controls, and no prose', async () => {
    // Everything this section could explain is already said elsewhere: the
    // versions on offer are in the select, what each host holds is in the
    // table below, that a running conversation keeps its version is in the
    // confirmation after the change, and how to add a version is deployment
    // documentation. Written here it was a third telling every time.
    render(<AdminHostsPage />);

    await screen.findByLabelText('claude-code');
    expect(screen.queryByText(/also built in/)).not.toBeInTheDocument();
    expect(screen.queryByText(/The image holds only/)).not.toBeInTheDocument();
    expect(screen.queryByText(/EXTRA_VERSIONS/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Applies to new threads/)).not.toBeInTheDocument();
  });

  it('still offers a fixed runtime, compressed but not removed', async () => {
    // Pinning a single-version runtime is a weak choice, not an absent one:
    // the control stays, it just stops taking three lines to say nothing.
    mocked.list.mockResolvedValue(
      pool([
        host({
          runtime_versions_available: {
            'claude-code': ['2.1.220', '2.1.235'],
            codex: ['0.145.0'],
          },
        }),
      ]),
    );
    render(<AdminHostsPage />);

    const codex = (await screen.findByLabelText('codex')) as HTMLSelectElement;
    expect([...codex.options].map((o) => o.textContent)).toEqual(['Image default', '0.145.0']);
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

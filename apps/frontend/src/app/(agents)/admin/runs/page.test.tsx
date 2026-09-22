// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { useSyncExternalStore } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentAccessApi } from '@/lib/api/session';
import type {
  AgentAttemptTelemetryApi,
  AgentJobUsageApi,
  AttemptResourcesApi,
} from '@/lib/api/agents';
import type {
  AdminRun,
  AdminRunDetail,
  AdminRunsPage,
  ResourceMetricBriefApi,
  RunResourceSummaryApi,
  TurnResourceSummaryApi,
} from '@/lib/api/adminRuns';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({ access: undefined as AgentAccessApi | undefined, id: 'admin-1' }));
const route = vi.hoisted(() => ({ jobId: 'job-1' }));
// The applied window comes from the URL, so a usage deep link can open this
// list on exactly the cohort it was aggregated from. The store notifies its
// subscribers, which is how a real URL change re-renders the mounted page.
const search = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  return {
    value: '',
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    read: () => search.value,
    set(next: string) {
      search.value = next;
      for (const listener of listeners) listener();
    },
  };
});
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: auth.id, role: 'admin', agent_access: auth.access } } }),
}));
vi.mock('@/components/providers/AuthProvider', async () => {
  const actual = await vi.importActual<typeof import('@/components/providers/AuthProvider')>(
    '@/components/providers/AuthProvider',
  );
  return { hasAgentPermission: actual.hasAgentPermission };
});
vi.mock('next/navigation', () => ({
  useParams: () => route,
  useSearchParams: () => new URLSearchParams(useSyncExternalStore(search.subscribe, search.read)),
}));
vi.mock('@/lib/api/adminRuns', () => ({
  getAdminRuns: vi.fn(),
  getAdminRun: vi.fn(),
  getAdminAttemptResources: vi.fn(),
  getAdminLlmRequestSources: vi.fn(),
  getAdminLlmRequests: vi.fn(),
}));
// The startup panel reads the measurement surface, a different client. Mocked
// for the same reason the run client is: jsdom has no control plane to answer,
// and a real fetch would fail these tests for a reason unrelated to what they
// assert. The panel's own honesty rules are proven in its own test file.
const measurements = vi.hoisted(() => ({
  listMeasurements: vi.fn(),
  getMeasurement: vi.fn(),
}));
vi.mock('@/lib/api/adminMeasurements', () => ({
  listMeasurements: measurements.listMeasurements,
  getMeasurement: measurements.getMeasurement,
}));

import {
  getAdminRuns,
  getAdminRun,
  getAdminAttemptResources,
  getAdminLlmRequestSources,
  getAdminLlmRequests,
} from '@/lib/api/adminRuns';
import { listMeasurements } from '@/lib/api/adminMeasurements';
import AdminRunsPageComponent from './AllRuns';
import AdminRunPage from './[jobId]/page';
import {
  collectingSentence,
  formatCpuSeconds,
  formatMemoryBytes,
  UNKNOWN_REASON_SENTENCE,
} from './presentTurnResources';
import { RunState } from './shared';

function run(overrides: Partial<AdminRun> = {}): AdminRun {
  return {
    id: 'job-1',
    title: 'Inspect parser',
    thread_id: 'thread-1',
    turn_no: 2,
    user_id: 'other-user',
    repo: 'org/repo',
    runtime: 'claude-code',
    model: 'model-a',
    reasoning_effort: null,
    execution_profile: 'untrusted-kata-v1',
    state: 'succeeded',
    created_at: '2026-09-10T08:00:00Z',
    updated_at: '2026-09-10T08:02:00Z',
    hosts: ['host-a'],
    tool_calls: 1,
    ...overrides,
  };
}

function page(runs = [run()], next: string | null = null): AdminRunsPage {
  return { runs, next_cursor: next, since: '2026-09-03T08:00:00Z', until: '2026-09-10T08:00:00Z' };
}

function usage(): AgentJobUsageApi {
  return {
    ledger: {
      status: 'partial',
      source: 'gateway_api_logs',
      attribution: 'grant_time_window',
      finalized: false,
      totals: {
        calls: 2,
        tokens_in: 40086,
        tokens_out: 413,
        cache_read: 19712,
        cache_write: null,
        reasoning: null,
        spent_usd: '0.00978569',
      },
      unknown_rows: {
        tokens_in: 0,
        tokens_out: 0,
        cache_read: 0,
        cache_write: 0,
        reasoning: 2,
        spent_usd: 0,
      },
      estimated_calls: 0,
      queried_at: '2026-09-10T08:03:00Z',
      missing_attempts: 0,
      attempts: [
        {
          attempt_id: 10,
          attempt_no: 1,
          status: 'partial',
          reason: null,
          totals: null,
          unknown_rows: null,
          estimated_calls: 0,
          queried_at: '2026-09-10T08:03:00Z',
          windows: [
            {
              since: '2026-09-10T08:00:00Z',
              until: '2026-09-10T08:02:00Z',
              basis: 'request_started_at',
            },
          ],
        },
      ],
    },
    runtime: {
      source: 'runtime',
      input_tokens: 20374,
      normalized_input_tokens: 40086,
      output_tokens: 413,
      cache_read_tokens: 19712,
    },
    conversation: null,
  };
}

function detail(overrides: Partial<AdminRunDetail> = {}): AdminRunDetail {
  return {
    run: run(),
    attempts: [
      {
        attempt_id: 10,
        attempt_no: 1,
        host: 'host-a',
        status: 'succeeded',
        started_at: '2026-09-10T08:00:00Z',
        finished_at: '2026-09-10T08:02:00Z',
      },
    ],
    usage: usage(),
    session_statistics: {
      model_seconds: 1667,
      tool_seconds: 6490,
      ttft_seconds: 2.9,
      output_tokens_per_second: 257,
      timing_source: 'message_events',
    },
    capabilities: { llm_requests: true },
    ...overrides,
  };
}

function attemptsDetail(): AdminRunDetail {
  return detail({
    attempts: [
      { ...detail().attempts[0], status: 'failed' },
      {
        attempt_id: 20,
        attempt_no: 2,
        host: 'host-b',
        status: 'succeeded',
        started_at: '2026-09-10T08:02:00Z',
        finished_at: '2026-09-10T08:03:00Z',
      },
    ],
  });
}

/**
 * The real response shape: the turn summary is one of the attempt's resources,
 * not a sibling of them. Overrides replace entries inside `resources`, so a
 * case cannot accidentally exercise a shape the server never sends.
 */
function resources(override: Partial<AttemptResourcesApi> = {}): AgentAttemptTelemetryApi {
  return {
    attempt_id: 10,
    attempt_no: 1,
    continuity_mode: 'hot',
    context_status: 'live',
    duration_ms: 120000,
    resources: {
      turn_summary: turnSummary(),
      ...override,
      // Deliberately stale numeric values with coverage zero: these must not
      // become whole-sandbox readings merely because the fields are populated.
      cpu_usec: 999000000,
      mem_peak_bytes: 999000000,
      mem_avg_bytes: null,
      io_read_bytes: 0,
      io_write_bytes: 0,
      net_rx_bytes: null,
      net_tx_bytes: null,
      sample_count: 0,
      coverage: 0,
      sampler_version: 'shared-member-v1',
      process_observer: 'ebpf-v1',
      process_observation_complete: false,
      process_events_lost: 2,
      tool_calls: [
        {
          call_seq: 1,
          tool_use_id: 'tool-1',
          tool_name: 'Bash',
          timing_source: 'exact',
          duration_ms: 400,
          sample_count: 0,
          overlap_n: 1,
          measured_span_ms: null,
          cpu_usec_exclusive: null,
          cpu_usec_shared: null,
          mem_peak_exclusive_bytes: null,
          mem_peak_shared_bytes: null,
          process_cpu_usec: 200000,
          process_mem_peak_bytes: 1024,
          process_io_read_bytes: null,
          process_io_write_bytes: 0,
          process_resources_partial: true,
        },
        {
          call_seq: 2,
          tool_use_id: 'tool-2',
          tool_name: 'Read',
          timing_source: 'message',
          duration_ms: 500,
          sample_count: 0,
          overlap_n: 1,
          measured_span_ms: null,
          cpu_usec_exclusive: null,
          cpu_usec_shared: null,
          mem_peak_exclusive_bytes: null,
          mem_peak_shared_bytes: null,
          process_cpu_usec: null,
        },
      ],
      processes: [],
    },
  };
}

/** The compact per-run resource projection the Runs list reads. */
function runSummary(overrides: Partial<RunResourceSummaryApi> = {}): RunResourceSummaryApi {
  return {
    cpu_usec: 80_000_000,
    memory_peak_bytes: 1_288_490_189,
    state: 'final',
    observed_through_ms: 130_000,
    cpu: { status: 'available', reason_codes: [] },
    memory: { status: 'available', reason_codes: [] },
    storage_io: { status: 'unavailable', reason_codes: ['filesystem_attribution_unverified'] },
    attempts_measured: 1,
    attempts_total: 1,
    ...overrides,
  };
}

/** One attempt's whole-turn member execution window. */
function turnSummary(overrides: Partial<TurnResourceSummaryApi> = {}): TurnResourceSummaryApi {
  return {
    measurement_id: '00000000-0000-4000-8000-000000000001',
    state: 'final',
    source: 'kata-guest-member-window-v1',
    received_at: '2026-09-10T08:02:00Z',
    duration_ms: 130_000,
    observed_through_ms: 130_000,
    open_scan_ms: 12,
    close_scan_ms: 15,
    cpu: {
      status: 'available',
      method: 'live_and_exit_task_counters',
      usec: 80_000_000,
      reason_codes: [],
    },
    memory: {
      status: 'available',
      method: 'sampled_member_pss',
      peak_bytes: 1_288_490_189,
      sample_interval_ms: 1000,
      valid_samples: 131,
      expected_samples: 131,
      max_gap_ms: 1000,
      reason_codes: [],
    },
    storage_io: {
      status: 'unavailable',
      method: 'task_io_accounting',
      read_bytes: null,
      write_bytes: null,
      reason_codes: ['filesystem_attribution_unverified'],
    },
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

/**
 * The list row whose accessible name starts with the run's link text.
 *
 * Queried this way because a resource cell must be asserted inside the row it
 * belongs to: "80 CPU-s appears somewhere" would also pass for a stale cell.
 */
function row(runId: string): HTMLElement {
  return screen.getByRole('row', {
    name: (name: string) => name.startsWith(runId),
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  search.value = '';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  auth.id = 'admin-1';
  route.jobId = 'job-1';
  vi.mocked(getAdminRuns).mockResolvedValue(page());
  vi.mocked(getAdminRun).mockResolvedValue(detail());
  vi.mocked(getAdminAttemptResources).mockResolvedValue(resources());
  vi.mocked(getAdminLlmRequestSources).mockResolvedValue({
    job_id: 'job-1',
    observed_at: '2026-09-10T08:03:00Z',
    sources: [],
    next_cursor: null,
    reason: null,
  });
  // The attempt detail also renders the startup panel. Its default answer here
  // is "nothing stored", which is what an attempt that was never selected by an
  // experiment legitimately has; the panel's own tests cover the other states.
  vi.mocked(listMeasurements).mockResolvedValue({
    measurements: [],
    count: 0,
    limit: 5,
    next_cursor: null,
    empty_reason: 'no stored samples match this selection',
  });
});
afterEach(cleanup);

describe('admin runs list', () => {
  it('displays physical hosts and counts machines instead of runner deployments', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(
      page([run({ hosts: ['gpu-box', 'gpu-box-kata'], physical_hosts: ['gpu-box'] })]),
    );
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(screen.getByTitle('gpu-box')).toBeInTheDocument();
    expect(screen.queryByText('gpu-box-kata')).not.toBeInTheDocument();
    expect(screen.queryByText('+1')).not.toBeInTheDocument();
  });

  it('keeps legacy runner names when the API has no physical host field', async () => {
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(screen.getByTitle('host-a')).toBeInTheDocument();
  });

  it('shows the number of additional physical machines', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(
      page([
        run({ hosts: ['runner-a', 'runner-b', 'runner-c'], physical_hosts: ['box-a', 'box-b'] }),
      ]),
    );
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(screen.getByTitle('box-a, box-b')).toBeInTheDocument();
    expect(screen.getByText('+1')).toBeInTheDocument();
  });

  it.each<AgentAccessApi | undefined>([
    undefined,
    { allowed: false, permissions: ['agent.admin'] },
    { allowed: true, permissions: ['agent.use'] },
  ])('does not fetch cross-user data without a current admin permission: %j', (access) => {
    auth.access = access;
    render(<AdminRunsPageComponent />);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(getAdminRuns).not.toHaveBeenCalled();
  });

  it('defaults to an anchored seven-day window and exposes working admin sections', async () => {
    render(<AdminRunsPageComponent />);
    expect(await screen.findByRole('link', { name: 'job-1' })).toHaveAttribute(
      'href',
      '/admin/runs/job-1',
    );
    expect(screen.queryByText('Inspect parser')).not.toBeInTheDocument();
    expect(screen.queryByTitle('Inspect parser')).not.toBeInTheDocument();
    const filters = vi.mocked(getAdminRuns).mock.calls[0][0];
    expect(Date.parse(filters.until!) - Date.parse(filters.since!)).toBe(7 * 86_400_000);
    expect(screen.getByLabelText('Created')).toHaveValue('7');
    expect(screen.getByLabelText('Status')).toHaveValue('');
    expect(
      within(screen.getByLabelText('Status'))
        .getAllByRole('option')
        .map((option) => option.textContent),
    ).toEqual(['All statuses', 'queued', 'running', 'succeeded', 'failed', 'cancelled']);
    const controls = within(screen.getByRole('group', { name: 'Run filters' }));
    expect(controls.getAllByRole('combobox')).toHaveLength(2);
    expect(controls.getByRole('button', { name: 'Refresh' })).toBeInTheDocument();
    expect(controls.queryByRole('textbox')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /More filters|Apply filters|Clear filters/ }),
    ).not.toBeInTheDocument();
    for (const label of ['User ID', 'Repository', 'Agent', 'Model', 'Host']) {
      expect(screen.queryByLabelText(label)).not.toBeInTheDocument();
    }
    const nav = screen.getByRole('navigation', { name: 'Admin sections' });
    expect(within(nav).getByRole('link', { name: 'Conversations' })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(within(nav).getByRole('link', { name: 'Servers' })).toHaveAttribute(
      'href',
      '/admin/hosts',
    );
    expect(within(nav).getByRole('link', { name: 'Usage' })).toHaveAttribute(
      'href',
      '/admin/usage',
    );
  });

  it('honours an absolute window and identities from a usage deep link', async () => {
    search.value =
      'since=2026-09-01T00:00:00.000Z&until=2026-09-08T00:00:00.000Z&model=model-a&user_id=owner-one';
    vi.mocked(getAdminRuns).mockResolvedValue(page());
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    // The linked window replaces the seven-day default, and the identities the
    // usage row was grouped by travel with it.
    expect(vi.mocked(getAdminRuns).mock.calls[0][0]).toMatchObject({
      since: '2026-09-01T00:00:00.000Z',
      until: '2026-09-08T00:00:00.000Z',
      model: 'model-a',
      user_id: 'owner-one',
    });
    // The control names the window that is applied: a week that ended a month
    // ago is Custom, not "Last 7 days".
    expect(screen.getByLabelText('Created')).toHaveValue('custom');
    expect(screen.getByText(/linked usage group/)).toBeInTheDocument();
  });

  it('keeps a linked window on Refresh instead of drifting with the clock', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      vi.setSystemTime(new Date('2026-09-10T08:00:00Z'));
      search.value = 'since=2026-09-01T00:00:00.000Z&until=2026-09-08T00:00:00.000Z';
      vi.mocked(getAdminRuns).mockResolvedValue(page());
      render(<AdminRunsPageComponent />);
      await screen.findByRole('link', { name: 'job-1' });
      vi.setSystemTime(new Date('2026-09-10T09:00:00Z'));
      fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
      await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(2));
      expect(vi.mocked(getAdminRuns).mock.lastCall![0]).toMatchObject({
        since: '2026-09-01T00:00:00.000Z',
        until: '2026-09-08T00:00:00.000Z',
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('refreshes a window that has aged past its preset', async () => {
    // The Created control reads Custom once the window is older than a preset
    // tolerance, so the refresh length has to come from the window on screen.
    // Reading it from the control's value produced an invalid date.
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      vi.setSystemTime(new Date('2026-09-10T08:00:00Z'));
      vi.mocked(getAdminRuns).mockResolvedValue(page());
      render(<AdminRunsPageComponent />);
      await screen.findByRole('link', { name: 'job-1' });

      vi.setSystemTime(new Date('2026-09-10T08:06:00Z'));
      fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'failed' } });
      await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(2));
      expect(screen.getByLabelText('Created')).toHaveValue('custom');

      fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
      await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(3));
      const filters = vi.mocked(getAdminRuns).mock.lastCall![0];
      // The same seven-day window, re-anchored at the current time.
      expect(Date.parse(filters.until!) - Date.parse(filters.since!)).toBe(7 * 86_400_000);
      expect(filters.until).toBe('2026-09-10T08:06:00.000Z');
      expect(filters.state).toBe('failed');
    } finally {
      vi.useRealTimers();
    }
  });

  it('follows a URL that changes while the list stays mounted', async () => {
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(vi.mocked(getAdminRuns).mock.calls[0][0].model).toBeUndefined();

    search.set('since=2026-08-01T00:00:00.000Z&until=2026-08-08T00:00:00.000Z&model=model-a');
    await act(async () => {});
    await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(2));
    expect(vi.mocked(getAdminRuns).mock.calls[1][0]).toMatchObject({
      since: '2026-08-01T00:00:00.000Z',
      until: '2026-08-08T00:00:00.000Z',
      model: 'model-a',
    });
  });

  it('reports an unusable link instead of answering for every run', async () => {
    // An inverted window is a broken link, not an empty selection. Falling back
    // to the default window would answer a question nobody asked.
    search.value = 'since=2026-09-08T00:00:00.000Z&until=2026-09-01T00:00:00.000Z';
    render(<AdminRunsPageComponent />);
    await screen.findByRole('alert');
    expect(screen.getByText(/empty or inverted window/)).toBeInTheDocument();
    expect(getAdminRuns).not.toHaveBeenCalled();
  });

  it('reports a link naming a filter this list cannot express', async () => {
    search.value = 'since=2026-09-01T00:00:00.000Z&until=2026-09-08T00:00:00.000Z&host=runner-one';
    render(<AdminRunsPageComponent />);
    await screen.findByRole('alert');
    expect(screen.getByText(/Unsupported filter: host/)).toBeInTheDocument();
    expect(getAdminRuns).not.toHaveBeenCalled();
  });

  it('keeps a one-day linked window when Status changes', async () => {
    // The Created control names the window that is applied, so changing a
    // non-time filter cannot re-derive seven days from a stale preset.
    search.value = 'since=2026-09-09T00:00:00.000Z&until=2026-09-10T00:00:00.000Z';
    vi.mocked(getAdminRuns).mockResolvedValue(page());
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(screen.getByLabelText('Created')).toHaveValue('custom');
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'failed' } });
    await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(2));
    expect(vi.mocked(getAdminRuns).mock.lastCall![0]).toMatchObject({
      since: '2026-09-09T00:00:00.000Z',
      until: '2026-09-10T00:00:00.000Z',
      state: 'failed',
    });
  });

  it('applies filters and paginates with the same window and filter scope', async () => {
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run()], 'cursor-2'))
      .mockResolvedValueOnce(page([run({ id: 'job-2', title: 'Second run' })]));
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'failed' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Load more runs' }));
    await screen.findByRole('link', { name: 'job-2' });
    expect(getAdminRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({
        state: 'failed',
        cursor: 'cursor-2',
        since: page().since,
        until: page().until,
      }),
      expect.any(AbortSignal),
    );
    expect(screen.getByRole('link', { name: 'job-1' })).toBeInTheDocument();
    expect(screen.queryByText('Second run')).not.toBeInTheDocument();
  });

  it('keeps the queued filter and badge across pages', async () => {
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run({ state: 'queued' })], 'group-cursor'))
      .mockResolvedValueOnce(page([run({ id: 'job-2', state: 'queued' })]));
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'queued' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Load more runs' }));
    await screen.findByRole('link', { name: 'job-2' });

    expect(vi.mocked(getAdminRuns).mock.calls[1][0]).toEqual(
      expect.objectContaining({ state: 'queued' }),
    );
    expect(getAdminRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({
        state: 'queued',
        cursor: 'group-cursor',
        since: page().since,
        until: page().until,
      }),
      expect.any(AbortSignal),
    );
    const table = within(screen.getByRole('table'));
    expect(table.getAllByText('queued')).toHaveLength(2);
  });

  it('renders a legacy publishing badge as running', () => {
    render(<RunState state="publishing" />);
    expect(screen.getByText('running')).toBeInTheDocument();
    expect(screen.queryByText('publishing')).not.toBeInTheDocument();
  });

  it('applies Created and Status immediately and replaces previously paged rows', async () => {
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run()], 'cursor-2'))
      .mockResolvedValueOnce(page([run({ id: 'job-2' })]))
      .mockResolvedValueOnce(page([run({ id: 'job-30-days' })]))
      .mockResolvedValueOnce(page([run({ id: 'job-failed', state: 'failed' })]));
    render(<AdminRunsPageComponent />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load more runs' }));
    await screen.findByRole('link', { name: 'job-2' });

    fireEvent.change(screen.getByLabelText('Created'), { target: { value: '30' } });
    await screen.findByRole('link', { name: 'job-30-days' });
    const timeFilters = vi.mocked(getAdminRuns).mock.lastCall![0];
    expect(Date.parse(timeFilters.until!) - Date.parse(timeFilters.since!)).toBe(30 * 86_400_000);
    expect(timeFilters.cursor).toBeUndefined();
    expect(screen.queryByRole('link', { name: 'job-1' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'job-2' })).not.toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'failed' } });
    await screen.findByRole('link', { name: 'job-failed' });
    const statusFilters = vi.mocked(getAdminRuns).mock.lastCall![0];
    expect(statusFilters.state).toBe('failed');
    expect(Date.parse(statusFilters.until!) - Date.parse(statusFilters.since!)).toBe(
      30 * 86_400_000,
    );
    expect(statusFilters.cursor).toBeUndefined();
    expect(screen.queryByRole('link', { name: 'job-30-days' })).not.toBeInTheDocument();
    expect(getAdminRuns).toHaveBeenCalledTimes(4);
  });

  it('refreshes the time window while preserving Status and replacing previously paged rows', async () => {
    vi.useFakeTimers({ toFake: ['Date'] });
    try {
      vi.setSystemTime(new Date('2026-09-10T08:00:00Z'));
      vi.mocked(getAdminRuns)
        .mockResolvedValueOnce(page())
        .mockResolvedValueOnce(page([run({ state: 'failed' })], 'cursor-2'))
        .mockResolvedValueOnce(page([run({ id: 'job-2', state: 'failed' })]))
        .mockResolvedValueOnce(page([run({ id: 'refreshed-run', state: 'failed' })]));
      render(<AdminRunsPageComponent />);
      await screen.findByRole('link', { name: 'job-1' });
      fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'failed' } });
      fireEvent.click(await screen.findByRole('button', { name: 'Load more runs' }));
      await screen.findByRole('link', { name: 'job-2' });

      vi.setSystemTime(new Date('2026-09-10T09:00:00Z'));
      fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
      await screen.findByRole('link', { name: 'refreshed-run' });
      const filters = vi.mocked(getAdminRuns).mock.lastCall![0];
      expect(filters.since).toBe('2026-09-03T09:00:00.000Z');
      expect(filters.until).toBe('2026-09-10T09:00:00.000Z');
      expect(filters.state).toBe('failed');
      expect(filters.cursor).toBeUndefined();
      expect(screen.getByLabelText('Status')).toHaveValue('failed');
      expect(screen.queryByRole('link', { name: 'job-1' })).not.toBeInTheDocument();
      expect(screen.queryByRole('link', { name: 'job-2' })).not.toBeInTheDocument();
      expect(getAdminRuns).toHaveBeenCalledTimes(4);
    } finally {
      vi.useRealTimers();
    }
  });

  it('ignores an old response when the filters change', async () => {
    const pending = deferred<AdminRunsPage>();
    vi.mocked(getAdminRuns)
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(page([run({ id: 'current-match' })]));
    render(<AdminRunsPageComponent />);
    fireEvent.change(screen.getByLabelText('Created'), { target: { value: '30' } });
    await screen.findByRole('link', { name: 'current-match' });
    await act(async () => pending.resolve(page([run({ id: 'stale-match' })])));
    expect(screen.queryByRole('link', { name: 'stale-match' })).not.toBeInTheDocument();
    expect(vi.mocked(getAdminRuns).mock.calls[0][1]?.aborted).toBe(true);
  });

  it('ignores a late pagination response after Status changes the filter scope', async () => {
    const pending = deferred<AdminRunsPage>();
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run()], 'old-cursor'))
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(page([run({ id: 'current-match', state: 'failed' })]));
    render(<AdminRunsPageComponent />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load more runs' }));
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'failed' } });
    await screen.findByRole('link', { name: 'current-match' });
    expect(vi.mocked(getAdminRuns).mock.calls[1][1]?.aborted).toBe(true);
    expect(vi.mocked(getAdminRuns).mock.lastCall![0].cursor).toBeUndefined();

    await act(async () =>
      pending.resolve(page([run({ id: 'stale-match' })], 'another-old-cursor')),
    );
    expect(screen.queryByRole('link', { name: 'job-1' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'stale-match' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Load more runs' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'current-match' })).toBeInTheDocument();
  });

  it('clears retained cross-user rows when a later page rejects revoked access', async () => {
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run()], 'more'))
      .mockRejectedValueOnce(new APIError('FORBIDDEN', 'Admin permission revoked', 403));
    render(<AdminRunsPageComponent />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load more runs' }));
    await screen.findByText('Admin access required');
    expect(screen.queryByRole('link', { name: 'job-1' })).not.toBeInTheDocument();
  });
});

describe('admin runs resource cells', () => {
  // The list asserts a cell's text belongs to the run on screen, never that
  // some text appeared: a cell left over from the previous selection is the
  // failure this column can actually have.
  it('says a run with no whole-turn summary has none, rather than showing a zero', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(page([run()]));
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(within(row('job-1')).getAllByText('Not collected')).toHaveLength(3);
    expect(within(row('job-1')).queryByText('0 B')).not.toBeInTheDocument();
    expect(within(row('job-1')).queryByText('0 CPU-s')).not.toBeInTheDocument();
  });

  it('shows CPU and the sampled memory peak for a measured run', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(page([run({ resource_summary: runSummary() })]));
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(within(row('job-1')).getByText(`${formatCpuSeconds(80_000_000)}`)).toBeInTheDocument();
    expect(
      within(row('job-1')).getByText(`${formatMemoryBytes(1_288_490_189)} \u00b7 sampled`),
    ).toBeInTheDocument();
    // An unsupported I/O metric does not make the row read as a zero.
    expect(screen.queryByText(/^0 B$/)).not.toBeInTheDocument();
  });

  it('never leaves the previous run\u2019s resource cell beside a new selection', async () => {
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run({ resource_summary: runSummary() })]))
      .mockResolvedValueOnce(page([run({ resource_summary: null })]));
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    expect(within(row('job-1')).getByText('80 CPU-s')).toBeInTheDocument();

    // A filter change replaces the page; the old numbers must go with it.
    fireEvent.change(screen.getByLabelText('Created'), { target: { value: '30' } });
    await waitFor(() => expect(getAdminRuns).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(within(row('job-1')).queryByText('80 CPU-s')).not.toBeInTheDocument(),
    );
    expect(within(row('job-1')).getAllByText('Not collected')).toHaveLength(3);
  });

  it('renders a partial CPU measurement as a lower bound and a partial peak as observed', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(
      page([
        run({
          resource_summary: runSummary({
            state: 'interrupted',
            observed_through_ms: 130_000,
            cpu: { status: 'partial', reason_codes: ['ring_loss'] },
            memory: { status: 'partial', reason_codes: ['pss_unreadable'] },
            attempts_measured: 3,
            attempts_total: 5,
          }),
        }),
      ]),
    );
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    const cpu = within(row('job-1')).getByText(/\u2265 80 CPU-s/);
    expect(cpu).toBeInTheDocument();
    expect(cpu.textContent).toContain('collection was interrupted');
    const memory = within(row('job-1')).getByText(/1.2 GiB/);
    expect(memory.textContent).toContain('observed');
    expect(memory.textContent).not.toContain('\u2265');
    // A total over three of five executions is not the run's total.
    const state = within(row('job-1')).getByText('Interrupted');
    expect(state.getAttribute('title')).toContain('3 of 5 executions measured');
  });

  it('states when collection is still running rather than showing a final total', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(
      page([
        run({
          resource_summary: runSummary({
            state: 'collecting',
            observed_through_ms: 90_000,
            memory: { status: 'partial', reason_codes: ['sampling_over_budget'] },
          }),
        }),
      ]),
    );
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    const state = within(row('job-1')).getByText('Collecting');
    // The string handed to title is checked separately: jsdom renders the
    // attribute, and the sentence must name how far the window actually got.
    expect(state.getAttribute('title')).toBe(collectingSentence(90_000));
    expect(state.getAttribute('title')).toContain('not the turn\u2019s total');
  });

  it('gives an unknown reason code the generic sentence, never the code', async () => {
    vi.mocked(getAdminRuns).mockResolvedValue(
      page([
        run({
          resource_summary: runSummary({
            cpu: {
              status: 'partial',
              reason_codes: [
                'uid=1001 lease_token=deadbeef',
              ] as unknown as ResourceMetricBriefApi['reason_codes'],
            },
          }),
        }),
      ]),
    );
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'job-1' });
    const cpu = within(row('job-1')).getByText(/\u2265 80 CPU-s/);
    expect(cpu.getAttribute('title')).toBe(UNKNOWN_REASON_SENTENCE);
    expect(screen.queryByText(/deadbeef|uid=1001/)).not.toBeInTheDocument();
  });
});

describe('admin run observation', () => {
  it('shows the server and timing without an execution selector for a single attempt', async () => {
    render(<AdminRunPage />);
    await screen.findByText('Server');
    const serverCard = screen.getByText('Server').closest('dl')!;
    expect(within(serverCard).getByText('host-a')).toBeInTheDocument();
    expect(within(serverCard).getByText('succeeded')).toBeInTheDocument();
    expect(within(serverCard).getByText('Started')).toBeInTheDocument();
    expect(within(serverCard).getByText('Finished')).toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Previous attempts/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Execution')).not.toBeInTheDocument();
  });

  it('follows the newest attempt regardless of response order and discovers new attempts on refresh', async () => {
    const initial = attemptsDetail();
    const newest = {
      ...initial.attempts[1],
      attempt_id: 30,
      attempt_no: 3,
      host: 'host-c',
      started_at: '2026-09-10T08:04:00Z',
      finished_at: null,
    };
    vi.mocked(getAdminRun)
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce(
        detail({ attempts: [initial.attempts[0], newest, initial.attempts[1]] }),
      );
    render(<AdminRunPage />);
    await screen.findByText('host-b');
    expect(getAdminAttemptResources).toHaveBeenLastCalledWith('job-1', 20, expect.any(AbortSignal));
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    // Opening history alone must not pin the latest attempt when a new one arrives.
    fireEvent.click(screen.getByRole('button', { name: 'Previous attempts (1)' }));
    expect(screen.getByLabelText('View attempt')).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh run' }));
    await screen.findByText('host-c');
    expect(getAdminAttemptResources).toHaveBeenLastCalledWith('job-1', 30, expect.any(AbortSignal));
    expect(screen.getByLabelText('View attempt')).toHaveValue('');
    expect(screen.queryByText('Finished')).not.toBeInTheDocument();
  });

  it('keeps explicit history across refresh, scopes every panel, and returns to latest when closed', async () => {
    const initial = attemptsDetail();
    vi.mocked(getAdminRun).mockResolvedValue(initial);
    render(<AdminRunPage />);
    await screen.findByText('host-b');
    fireEvent.click(screen.getByRole('button', { name: 'Previous attempts (1)' }));
    fireEvent.change(screen.getByLabelText('View attempt'), { target: { value: '10' } });
    await screen.findByText('host-a');
    expect(screen.getByText('failed')).toBeInTheDocument();
    expect(getAdminAttemptResources).toHaveBeenLastCalledWith('job-1', 10, expect.any(AbortSignal));
    await waitFor(() =>
      expect(getAdminLlmRequestSources).toHaveBeenLastCalledWith(
        'job-1',
        { attempt_id: 10, limit: 100 },
        expect.any(AbortSignal),
      ),
    );

    vi.mocked(getAdminRun).mockResolvedValueOnce(
      detail({
        attempts: [
          ...initial.attempts,
          { ...initial.attempts[1], attempt_id: 30, attempt_no: 3, host: 'host-c' },
        ],
      }),
    );
    vi.mocked(getAdminLlmRequestSources).mockClear();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh run' }));
    await screen.findByRole('button', { name: 'Previous attempts (2)' });
    expect(screen.getByLabelText('View attempt')).toHaveValue('10');
    expect(screen.getByText('host-a')).toBeInTheDocument();
    await waitFor(() =>
      expect(getAdminLlmRequestSources).toHaveBeenCalledWith(
        'job-1',
        { attempt_id: 10, limit: 100 },
        expect.any(AbortSignal),
      ),
    );

    fireEvent.click(screen.getByRole('button', { name: 'Previous attempts (2)' }));
    await screen.findByText('host-c');
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(getAdminAttemptResources).toHaveBeenLastCalledWith('job-1', 30, expect.any(AbortSignal));
    await waitFor(() =>
      expect(getAdminLlmRequestSources).toHaveBeenLastCalledWith(
        'job-1',
        { attempt_id: 30, limit: 100 },
        expect.any(AbortSignal),
      ),
    );
  });

  it('explains queued runs without assuming why execution has not started', async () => {
    vi.mocked(getAdminRun).mockResolvedValue(
      detail({ run: run({ state: 'queued' }), attempts: [] }),
    );
    render(<AdminRunPage />);
    expect(await screen.findByText('Waiting to start')).toBeInTheDocument();
    expect(screen.getByText('queued')).toBeInTheDocument();
    expect(screen.getByText('No execution has started.')).toBeInTheDocument();
  });

  it('rejects an owner without admin permission before any detail, resource or request read', () => {
    auth.access = { allowed: true, permissions: ['agent.use'] };
    render(<AdminRunPage />);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    for (const fn of [
      getAdminRun,
      getAdminAttemptResources,
      getAdminLlmRequestSources,
      getAdminLlmRequests,
    ])
      expect(fn).not.toHaveBeenCalled();
  });

  it('shows session statistics and one token usage summary without usage diagnostics', async () => {
    render(<AdminRunPage />);
    expect(await screen.findByRole('heading', { name: 'Session statistics' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Token usage' })).toBeVisible();
    expect(screen.getByText('27m 47s')).toBeVisible();
    const totals = within(screen.getByRole('table', { name: 'Token usage totals' }));
    expect(totals.getByRole('row', { name: 'Model calls 2' })).toBeVisible();
    expect(totals.getByText('$0.0097')).toHaveAttribute('title', '0.00978569');
    expect(screen.queryByText(/Gateway totals for this run/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Estimates from recorded activity/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Gateway:/)).not.toBeInTheDocument();
    expect(screen.queryByText('Finalized')).not.toBeInTheDocument();
    expect(screen.queryByText('Not finalized')).not.toBeInTheDocument();
    expect(screen.queryByText('Accounting update in progress')).not.toBeInTheDocument();
    expect(screen.queryByText('Usage details')).not.toBeInTheDocument();
    expect(screen.queryByText('Gateway ledger')).not.toBeInTheDocument();
    expect(screen.queryByText('Runtime reported')).not.toBeInTheDocument();
    expect(screen.queryByText('Attribution windows')).not.toBeInTheDocument();
    expect(screen.queryByText(/Snapshot:/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Rows missing metrics:/)).not.toBeInTheDocument();
    expect(
      screen.queryByRole('table', { name: 'Usage source comparison' }),
    ).not.toBeInTheDocument();
    expect(screen.getByText('LLM requests')).toBeInTheDocument();
    // Request metrics now load with the section; an empty source list makes no gateway read.
    expect(getAdminLlmRequestSources).toHaveBeenCalledWith(
      'job-1',
      { attempt_id: 10, limit: 100 },
      expect.any(AbortSignal),
    );
    expect(getAdminLlmRequests).not.toHaveBeenCalled();
    expect(screen.queryByText('Failure classification')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /Publish|View changes|Raw events/i }),
    ).not.toBeInTheDocument();
  });

  it('keeps incomplete, estimated and unavailable usage clear in the summary', async () => {
    const partial = usage();
    partial.ledger.missing_attempts = 1;
    partial.ledger.estimated_calls = 2;
    partial.ledger.unknown_rows!.tokens_in = 1;
    partial.ledger.unknown_rows!.spent_usd = 1;
    vi.mocked(getAdminRun).mockResolvedValue(
      detail({ usage: partial, session_statistics: undefined }),
    );

    render(<AdminRunPage />);
    await screen.findByRole('heading', { name: 'Token usage' });
    const statistics = within(screen.getByRole('region', { name: 'Session statistics' }));
    expect(statistics.getAllByText('Unavailable')).toHaveLength(4);
    const totals = within(screen.getByRole('table', { name: 'Token usage totals' }));
    expect(
      totals.getByRole('row', { name: 'Input tokens (including cache) 40,086 (1 unknown)' }),
    ).toBeVisible();
    expect(totals.getByRole('row', { name: 'Cost (USD) $0.0097 (1 unknown)' })).toBeVisible();
    expect(totals.getByRole('row', { name: 'Reasoning tokens Unavailable' })).toBeVisible();
    expect(screen.getByText('1 execution(s) have no gateway totals.')).toBeVisible();
    expect(screen.getByText('2 calls contain estimated usage.')).toBeVisible();
    expect(screen.queryByText('Usage details')).not.toBeInTheDocument();
  });

  it('keeps zero-coverage totals unavailable and exposes timing provenance and incomplete process evidence', async () => {
    render(<AdminRunPage />);
    await screen.findByText('Exact · hooks');
    expect(screen.getByText('Approximate · messages')).toBeInTheDocument();
    expect(screen.getByText('≥ 200ms')).toBeInTheDocument();
    expect(screen.getByText('≥ 0B')).toBeInTheDocument();
    expect(screen.getByText(/whole-sandbox totals were not collected/)).toBeInTheDocument();
    expect(screen.queryByText('16m 39s')).not.toBeInTheDocument();
    expect(screen.getByText(/2 events lost/)).toBeInTheDocument();
    expect(screen.getByText(/largest single process peak/)).toBeInTheDocument();
  });

  it('shows the whole-turn window at the contract\u2019s scope and length', async () => {
    render(<AdminRunPage />);
    expect(await screen.findByText('Turn resource summary')).toBeInTheDocument();
    expect(screen.getByText('80 CPU-s')).toBeInTheDocument();
    expect(screen.getByText('1.2 GiB \u00b7 sampled')).toBeInTheDocument();
    // The panel must never imply the number is the platform's whole cost.
    expect(
      screen.getByText('Sandbox member execution, including the agent and its tools'),
    ).toBeInTheDocument();
    expect(screen.getByText('sampled every 1s')).toBeInTheDocument();
    expect(
      screen.getByText(/CPU complete\. memory sampling complete\. I\/O unavailable\./),
    ).toBeInTheDocument();
    // Said wherever the metric appears, never only in one place.
    expect(
      screen.getAllByText(/do not prove which filesystem this I\/O reached/).length,
    ).toBeGreaterThan(1);
  });

  it('says the scope and the run-level coverage when the numbers are not the run\u2019s total', async () => {
    vi.mocked(getAdminRun).mockResolvedValue(
      detail({
        run: run({
          resource_summary: runSummary({ attempts_measured: 3, attempts_total: 5 }),
        }),
      }),
    );
    render(<AdminRunPage />);
    await screen.findByText('Turn resource summary');
    expect(
      screen.getByText('3 of 5 executions measured; this is not the run\u2019s total.'),
    ).toBeInTheDocument();
  });

  it('keeps an interrupted collection out of the finalised form and names its cut-off', async () => {
    vi.mocked(getAdminAttemptResources).mockResolvedValue({
      ...resources({
        turn_summary: turnSummary({ state: 'interrupted', observed_through_ms: 130_000 }),
      }),
    });
    render(<AdminRunPage />);
    await screen.findByText('Turn resource summary');
    expect(
      screen.getAllByText(/Collection interrupted: data reached 2m 10s/).length,
    ).toBeGreaterThan(1);
    // The turn panel must not add a claim of its own that collection
    // finished. The gateway ledger's "not finalized" sentence lives in the
    // owner-facing RunDetails tree, which this admin page does not render, so
    // there is nothing here to cross-check against.
    expect(screen.queryByText(/Collection finished with the turn/)).not.toBeInTheDocument();
  });

  it('keeps the lower bound off a partial memory peak and on a partial CPU one', async () => {
    vi.mocked(getAdminAttemptResources).mockResolvedValue({
      ...resources({
        turn_summary: turnSummary({
          cpu: {
            status: 'partial',
            method: 'live_and_exit_task_counters',
            usec: 80_000_000,
            reason_codes: ['ring_loss'],
          },
          memory: {
            status: 'partial',
            method: 'sampled_member_pss',
            peak_bytes: 1_288_490_189,
            sample_interval_ms: 1000,
            valid_samples: 9,
            expected_samples: 20,
            max_gap_ms: 4000,
            reason_codes: ['pss_unreadable'],
          },
        }),
      }),
    });
    render(<AdminRunPage />);
    await screen.findByText('Turn resource summary');
    expect(screen.getByText('\u2265 80 CPU-s')).toBeInTheDocument();
    expect(screen.getByText('1.2 GiB \u00b7 sampled \u00b7 observed')).toBeInTheDocument();
    expect(screen.queryByText(/\u2265 1\.2 GiB/)).not.toBeInTheDocument();
    expect(screen.getAllByText(/This value is a lower bound/).length).toBeGreaterThan(1);
  });

  it('says a missing window was not collected rather than showing a zero', async () => {
    vi.mocked(getAdminAttemptResources).mockResolvedValue({
      ...resources({ turn_summary: null }),
    });
    render(<AdminRunPage />);
    await screen.findByText('Turn resource summary');
    expect(screen.getByText(/Not collected for this execution/)).toBeInTheDocument();
    expect(screen.getByText(/This is not a measured zero/)).toBeInTheDocument();
    expect(screen.queryByText('0 CPU-s')).not.toBeInTheDocument();
  });

  it.each(['Refresh run', 'Refresh resources'])(
    'removes all retained metadata after %s receives 403, even with cached admin permission',
    async (button) => {
      render(<AdminRunPage />);
      await screen.findByText('Inspect parser');
      const denied = new APIError('FORBIDDEN', 'Admin permission revoked', 403);
      if (button === 'Refresh run') vi.mocked(getAdminRun).mockRejectedValueOnce(denied);
      if (button === 'Refresh resources')
        vi.mocked(getAdminAttemptResources).mockRejectedValueOnce(denied);
      fireEvent.click(screen.getByRole('button', { name: button }));
      await screen.findByText('Admin access required');
      expect(auth.access?.permissions).toContain('agent.admin');
      expect(screen.queryByText('Inspect parser')).not.toBeInTheDocument();
      expect(screen.queryByText('$0.00978569')).not.toBeInTheDocument();
    },
  );

  it('keeps run summaries whole while viewing history and aborts an in-flight panel read when admin permission is lost', async () => {
    vi.mocked(getAdminRun).mockResolvedValue(attemptsDetail());
    const view = render(<AdminRunPage />);
    await screen.findByText('host-b');
    fireEvent.click(screen.getByRole('button', { name: 'Previous attempts (1)' }));
    const pending = deferred<Awaited<ReturnType<typeof getAdminLlmRequestSources>>>();
    vi.mocked(getAdminLlmRequestSources).mockReturnValueOnce(pending.promise);
    fireEvent.change(screen.getByLabelText('View attempt'), { target: { value: '10' } });
    await screen.findByText('host-a');
    // Resources follow the selected history. Both summary sections continue to
    // describe the entire run without another detail read.
    expect(getAdminRun).toHaveBeenCalledTimes(1);
    expect(screen.getByText('27m 47s')).toBeVisible();
    expect(screen.getByText('257 tok/s')).toBeVisible();
    expect(
      within(screen.getByRole('table', { name: 'Token usage totals' })).getByRole('row', {
        name: 'Model calls 2',
      }),
    ).toBeVisible();
    await waitFor(() => expect(getAdminLlmRequestSources).toHaveBeenCalled());
    auth.access = { allowed: true, permissions: ['agent.use'] };
    view.rerender(<AdminRunPage />);
    expect(vi.mocked(getAdminLlmRequestSources).mock.lastCall?.[2]?.aborted).toBe(true);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
    view.rerender(<AdminRunPage />);
    await screen.findByText('host-b');
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('discards history and aborts its reads when navigating to another run', async () => {
    vi.mocked(getAdminRun).mockResolvedValueOnce(attemptsDetail());
    const view = render(<AdminRunPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Previous attempts (1)' }));
    const pending = deferred<Awaited<ReturnType<typeof getAdminLlmRequestSources>>>();
    vi.mocked(getAdminLlmRequestSources).mockReturnValueOnce(pending.promise);
    fireEvent.change(screen.getByLabelText('View attempt'), { target: { value: '10' } });
    await waitFor(() => expect(getAdminLlmRequestSources).toHaveBeenCalled());
    const historyRead = vi.mocked(getAdminLlmRequestSources).mock.lastCall![2];

    route.jobId = 'job-2';
    vi.mocked(getAdminRun).mockResolvedValueOnce(
      detail({
        run: run({ id: 'job-2', title: 'Second run' }),
        attempts: [{ ...detail().attempts[0], attempt_id: 40, host: 'host-d' }],
      }),
    );
    view.rerender(<AdminRunPage />);
    await screen.findByText('host-d');
    expect(historyRead?.aborted).toBe(true);
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Previous attempts/ })).not.toBeInTheDocument();
    expect(getAdminAttemptResources).toHaveBeenLastCalledWith('job-2', 40, expect.any(AbortSignal));
  });
});

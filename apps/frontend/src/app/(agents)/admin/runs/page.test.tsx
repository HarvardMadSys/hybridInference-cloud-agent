// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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
  RunResourceSummaryApi,
  TurnResourceSummaryApi,
} from '@/lib/api/adminRuns';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({ access: undefined as AgentAccessApi | undefined, id: 'admin-1' }));
const route = vi.hoisted(() => ({ jobId: 'job-1' }));
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
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock('@/lib/api/adminRuns', () => ({
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
  getAdminRun,
  getAdminAttemptResources,
  getAdminLlmRequestSources,
  getAdminLlmRequests,
} from '@/lib/api/adminRuns';
import { listMeasurements } from '@/lib/api/adminMeasurements';
import AdminRunPage from './[jobId]/page';
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

/** The compact per-run resource projection a run carries. */
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
    window: {
      duration_ms: 130_000,
      observed_through_ms: 130_000,
      open_scan_ms: 12,
      close_scan_ms: 15,
    },
    cpu: {
      status: 'available',
      method: 'live_and_exit_task_counters',
      usec: 80_000_000,
      capacity_cores: 2,
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

beforeEach(() => {
  vi.clearAllMocks();
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  auth.id = 'admin-1';
  route.jobId = 'job-1';
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

describe('run state badge', () => {
  it('renders a legacy publishing badge as running', () => {
    render(<RunState state="publishing" />);
    expect(screen.getByText('running')).toBeInTheDocument();
    expect(screen.queryByText('publishing')).not.toBeInTheDocument();
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
    // The turn names its session as the admin list does.
    expect(screen.getByText('Session', { selector: 'dt' }).nextElementSibling).toHaveTextContent(
      'thread-1',
    );
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

  it('shows turn statistics and one token usage summary without usage diagnostics', async () => {
    render(<AdminRunPage />);
    expect(await screen.findByRole('heading', { name: 'Turn statistics' })).toBeVisible();
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
    const statistics = within(screen.getByRole('region', { name: 'Turn statistics' }));
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

  it('shows a concise per-execution resource summary with independent missing values', async () => {
    render(<AdminRunPage />);
    const panel = await screen.findByRole('group', { name: 'Execution resource summary' });
    expect(within(panel).getByText('This execution · Agent and tools')).toBeVisible();
    expect(within(panel).getByText('CPU usage (avg)').parentElement).toHaveTextContent('30.8%');
    expect(within(panel).getByText('Peak memory (sampled)').parentElement).toHaveTextContent(
      '1.2 GiB',
    );
    expect(within(panel).getByText('Storage read').parentElement).toHaveTextContent('Unavailable');
    expect(within(panel).getByText('Storage write').parentElement).toHaveTextContent('Unavailable');
    expect(within(panel).getByText('Storage read').nextElementSibling).toHaveAttribute(
      'title',
      expect.stringContaining('do not prove which filesystem this I/O reached'),
    );
    expect(screen.queryByText('Turn resource summary')).not.toBeInTheDocument();
    expect(screen.queryByText('Method and reasons')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Refresh resources' })).not.toBeInTheDocument();
  });

  it('continues to label the values as one execution when the run has partial coverage', async () => {
    vi.mocked(getAdminRun).mockResolvedValue(
      detail({
        run: run({
          resource_summary: runSummary({ attempts_measured: 3, attempts_total: 5 }),
        }),
      }),
    );
    render(<AdminRunPage />);
    const panel = await screen.findByRole('group', { name: 'Execution resource summary' });
    expect(within(panel).getByText('This execution · Agent and tools')).toBeVisible();
  });

  it('keeps an interrupted collection out of the finalised form and names its cut-off', async () => {
    vi.mocked(getAdminAttemptResources).mockResolvedValue({
      ...resources({
        turn_summary: turnSummary({
          state: 'interrupted',
          window: { ...turnSummary().window, observed_through_ms: 130_000 },
        }),
      }),
    });
    render(<AdminRunPage />);
    const panel = await screen.findByRole('group', { name: 'Execution resource summary' });
    expect(within(panel).getByText(/Collection interrupted/)).toBeVisible();
    expect(within(panel).getByText('CPU usage (avg)').parentElement).toHaveTextContent('—');
    expect(within(panel).getByText('Peak memory (sampled)').parentElement).toHaveTextContent(
      'observed',
    );
    expect(within(panel).getByText('Storage read').parentElement).toHaveTextContent('Unavailable');
    expect(within(panel).getByText('CPU usage (avg)').nextElementSibling).toHaveAttribute(
      'title',
      expect.stringContaining('data reached 2m 10s'),
    );
  });

  it('withholds partial CPU utilisation while preserving the observed memory peak', async () => {
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
    const panel = await screen.findByRole('group', { name: 'Execution resource summary' });
    expect(screen.queryByText(/CPU-s/)).not.toBeInTheDocument();
    expect(within(panel).getByText('CPU usage (avg)').nextElementSibling).toHaveTextContent('—');
    expect(within(panel).getByText('1.2 GiB · observed')).toBeInTheDocument();
    expect(screen.queryByText(/\u2265 1\.2 GiB/)).not.toBeInTheDocument();
    expect(within(panel).getByText('CPU usage (avg)').nextElementSibling).toHaveAttribute(
      'title',
      expect.stringContaining('requires a complete execution measurement'),
    );
  });

  it('says a missing window was not collected rather than showing a zero', async () => {
    vi.mocked(getAdminAttemptResources).mockResolvedValue({
      ...resources({ turn_summary: null }),
    });
    render(<AdminRunPage />);
    expect(
      await screen.findByText('No resource measurements were recorded for this execution.'),
    ).toBeVisible();
    expect(screen.queryByText('0 CPU-s')).not.toBeInTheDocument();
  });

  it('removes retained metadata when a run refresh receives 403', async () => {
    render(<AdminRunPage />);
    await screen.findByText('Inspect parser');
    vi.mocked(getAdminRun).mockRejectedValueOnce(
      new APIError('FORBIDDEN', 'Admin permission revoked', 403),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Refresh run' }));
    await screen.findByText('Admin access required');
    expect(auth.access?.permissions).toContain('agent.admin');
    expect(screen.queryByText('Inspect parser')).not.toBeInTheDocument();
    expect(screen.queryByText('$0.00978569')).not.toBeInTheDocument();
  });

  it('allows a failed resource read to retry and revokes access if that read receives 403', async () => {
    vi.mocked(getAdminAttemptResources).mockRejectedValueOnce(new Error('Resource read failed'));
    render(<AdminRunPage />);
    await screen.findByText('Inspect parser');
    await screen.findByText('Resource read failed');
    vi.mocked(getAdminAttemptResources).mockRejectedValueOnce(
      new APIError('FORBIDDEN', 'Admin permission revoked', 403),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('Admin access required');
    expect(screen.queryByText('Inspect parser')).not.toBeInTheDocument();
    expect(screen.queryByText('$0.00978569')).not.toBeInTheDocument();
  });

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

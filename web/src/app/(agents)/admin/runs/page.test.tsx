// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentAccessApi } from '@/lib/api/session';
import type { AgentAttemptTelemetryApi, AgentJobUsageApi } from '@/lib/api/agents';
import type {
  AdminActivityEvent,
  AdminRun,
  AdminRunDetail,
  AdminRunsPage,
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
vi.mock('next/navigation', () => ({ useParams: () => route }));
vi.mock('@/lib/api/adminRuns', () => ({
  getAdminRuns: vi.fn(),
  getAdminRun: vi.fn(),
  getAdminRunActivity: vi.fn(),
  getAdminAttemptResources: vi.fn(),
  revealAdminEventContent: vi.fn(),
}));

import {
  getAdminRuns,
  getAdminRun,
  getAdminRunActivity,
  getAdminAttemptResources,
  revealAdminEventContent,
} from '@/lib/api/adminRuns';
import AdminRunsPageComponent from './page';
import AdminRunPage from './[jobId]/page';

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
    capabilities: { llm_requests: false },
    ...overrides,
  };
}

function event(overrides: Partial<AdminActivityEvent> = {}): AdminActivityEvent {
  return {
    id: 42,
    attempt_id: 10,
    attempt_no: 1,
    seq: 1,
    event_type: 'tool_use',
    created_at: '2026-09-10T08:01:00Z',
    tool_use_id: 'tool-1',
    tool_name: 'Bash',
    is_error: null,
    has_content: true,
    ...overrides,
  };
}

function resources(): AgentAttemptTelemetryApi {
  return {
    attempt_id: 10,
    attempt_no: 1,
    continuity_mode: 'hot',
    context_status: 'live',
    duration_ms: 120000,
    resources: {
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
  vi.mocked(getAdminRuns).mockResolvedValue(page());
  vi.mocked(getAdminRun).mockResolvedValue(detail());
  vi.mocked(getAdminRunActivity).mockResolvedValue({ events: [event()], next_cursor: null });
  vi.mocked(getAdminAttemptResources).mockResolvedValue(resources());
  vi.mocked(revealAdminEventContent).mockResolvedValue({
    id: 42,
    attempt_id: 10,
    seq: 1,
    event_type: 'tool_use',
    created_at: '2026-09-10T08:01:00Z',
    payload: { input: { command: 'private command' } },
    truncated: false,
  });
});
afterEach(cleanup);

describe('admin runs list', () => {
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

  it('defaults to an anchored seven-day window and exposes only working admin sections', async () => {
    render(<AdminRunsPageComponent />);
    expect(await screen.findByRole('link', { name: 'Inspect parser' })).toHaveAttribute(
      'href',
      '/admin/runs/job-1',
    );
    const filters = vi.mocked(getAdminRuns).mock.calls[0][0];
    expect(Date.parse(filters.until!) - Date.parse(filters.since!)).toBe(7 * 86_400_000);
    expect(screen.getByLabelText('Created')).toHaveValue('7');
    const nav = screen.getByRole('navigation', { name: 'Admin sections' });
    expect(within(nav).getByRole('link', { name: 'Runs' })).toHaveAttribute('aria-current', 'page');
    expect(within(nav).getByRole('link', { name: 'Servers' })).toHaveAttribute(
      'href',
      '/admin/hosts',
    );
    expect(within(nav).queryByRole('link', { name: 'Usage' })).not.toBeInTheDocument();
  });

  it('applies filters and paginates with the same window and filter scope', async () => {
    render(<AdminRunsPageComponent />);
    await screen.findByRole('link', { name: 'Inspect parser' });
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run()], 'cursor-2'))
      .mockResolvedValueOnce(page([run({ id: 'job-2', title: 'Second run' })]));
    fireEvent.change(screen.getByLabelText('User ID'), { target: { value: 'person-2' } });
    fireEvent.change(screen.getByLabelText('Repository'), { target: { value: 'org/other' } });
    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'failed' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Load more runs' }));
    await screen.findByRole('link', { name: 'Second run' });
    expect(getAdminRuns).toHaveBeenLastCalledWith(
      expect.objectContaining({
        user_id: 'person-2',
        repo: 'org/other',
        state: 'failed',
        cursor: 'cursor-2',
        since: page().since,
        until: page().until,
      }),
      expect.any(AbortSignal),
    );
    expect(screen.getByRole('link', { name: 'Inspect parser' })).toBeInTheDocument();
  });

  it('ignores an old response when the filters change', async () => {
    const pending = deferred<AdminRunsPage>();
    vi.mocked(getAdminRuns)
      .mockReturnValueOnce(pending.promise)
      .mockResolvedValueOnce(page([run({ title: 'Current match' })]));
    render(<AdminRunsPageComponent />);
    fireEvent.change(screen.getByLabelText('User ID'), { target: { value: 'new-user' } });
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }));
    await screen.findByRole('link', { name: 'Current match' });
    await act(async () => pending.resolve(page([run({ title: 'Stale match' })])));
    expect(screen.queryByText('Stale match')).not.toBeInTheDocument();
    expect(vi.mocked(getAdminRuns).mock.calls[0][1]?.aborted).toBe(true);
  });

  it('clears retained cross-user rows when a later page rejects revoked access', async () => {
    vi.mocked(getAdminRuns)
      .mockResolvedValueOnce(page([run()], 'more'))
      .mockRejectedValueOnce(new APIError('FORBIDDEN', 'Admin permission revoked', 403));
    render(<AdminRunsPageComponent />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load more runs' }));
    await screen.findByText('Admin access required');
    expect(screen.queryByRole('link', { name: 'Inspect parser' })).not.toBeInTheDocument();
  });
});

describe('admin run observation', () => {
  it('rejects an owner without admin permission before any detail, activity, resource or content read', () => {
    auth.access = { allowed: true, permissions: ['agent.use'] };
    render(<AdminRunPage />);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    for (const fn of [
      getAdminRun,
      getAdminRunActivity,
      getAdminAttemptResources,
      revealAdminEventContent,
    ])
      expect(fn).not.toHaveBeenCalled();
  });

  it('loads metadata first and shows source-scoped accounting without inventing request metrics', async () => {
    render(<AdminRunPage />);
    await screen.findByRole('article', { name: 'Execution 1 event 1' });
    expect(getAdminRunActivity).toHaveBeenCalledWith(
      'job-1',
      { attempt_id: 10, limit: 100 },
      expect.any(AbortSignal),
    );
    expect(revealAdminEventContent).not.toHaveBeenCalled();
    expect(screen.queryByText('private command')).not.toBeInTheDocument();
    const calls = screen.getByRole('row', { name: 'Model calls 2 Unavailable' });
    expect(calls).toBeInTheDocument();
    expect(screen.getByText('$0.00978569')).toBeInTheDocument();
    expect(screen.getByText('Not finalized')).toBeInTheDocument();
    expect(screen.getByText(/Rows missing metrics: reasoning: 2/)).toBeInTheDocument();
    expect(screen.getByText(/Per-request LLM records are not stored here/)).toBeInTheDocument();
    expect(screen.queryByText('Failure classification')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /Publish|View changes|Raw events/i }),
    ).not.toBeInTheDocument();
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

  it('reveals one event only after an explicit click, renders content as text, and discards it when hidden', async () => {
    vi.mocked(revealAdminEventContent).mockResolvedValue({
      id: 42,
      attempt_id: 10,
      seq: 1,
      event_type: 'tool_use',
      created_at: '2026-09-10T08:01:00Z',
      payload: { input: '<img src=x onerror=alert(1)>' },
      truncated: true,
    });
    render(<AdminRunPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Reveal content' }));
    await screen.findByText(/"input": "<img/);
    expect(revealAdminEventContent).toHaveBeenCalledExactlyOnceWith(
      'job-1',
      42,
      expect.any(AbortSignal),
    );
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(screen.getByText(/Content was truncated/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Hide content' }));
    expect(screen.queryByText(/"input": "<img/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Reveal content' }));
    await waitFor(() => expect(revealAdminEventContent).toHaveBeenCalledTimes(2));
  });

  it('keeps a failed audit/content access local to the event and allows a deliberate retry', async () => {
    vi.mocked(revealAdminEventContent).mockRejectedValueOnce(
      new Error('Content access could not be recorded'),
    );
    render(<AdminRunPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Reveal content' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Content access could not be recorded',
    );
    expect(screen.getByText('$0.00978569')).toBeInTheDocument();
    expect(screen.queryByText('private command')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Reveal content' }));
    await screen.findByText(/private command/);
  });

  it.each(['Refresh run', 'Refresh activity', 'Refresh resources'])(
    'removes all retained metadata and revealed content after %s receives 403, even with cached admin permission',
    async (button) => {
      render(<AdminRunPage />);
      fireEvent.click(await screen.findByRole('button', { name: 'Reveal content' }));
      await screen.findByText(/private command/);
      const denied = new APIError('FORBIDDEN', 'Admin permission revoked', 403);
      if (button === 'Refresh run') vi.mocked(getAdminRun).mockRejectedValueOnce(denied);
      if (button === 'Refresh activity')
        vi.mocked(getAdminRunActivity).mockRejectedValueOnce(denied);
      if (button === 'Refresh resources')
        vi.mocked(getAdminAttemptResources).mockRejectedValueOnce(denied);
      fireEvent.click(screen.getByRole('button', { name: button }));
      await screen.findByText('Admin access required');
      expect(auth.access?.permissions).toContain('agent.admin');
      expect(screen.queryByText(/private command/)).not.toBeInTheDocument();
      expect(screen.queryByText('Inspect parser')).not.toBeInTheDocument();
      expect(screen.queryByText('$0.00978569')).not.toBeInTheDocument();
    },
  );

  it('keeps repeated event sequences separate across executions and scopes paged activity by execution', async () => {
    vi.mocked(getAdminRun).mockResolvedValue(
      detail({
        attempts: [
          ...detail().attempts,
          {
            attempt_id: 20,
            attempt_no: 2,
            host: 'host-b',
            status: 'succeeded',
            started_at: '2026-09-10T08:02:00Z',
            finished_at: '2026-09-10T08:03:00Z',
          },
        ],
      }),
    );
    const view = render(<AdminRunPage />);
    await screen.findByRole('article', { name: 'Execution 1 event 1' });
    vi.mocked(getAdminRunActivity)
      .mockResolvedValueOnce({
        events: [event({ id: 70, attempt_id: 20, attempt_no: 2 })],
        next_cursor: 'second-attempt',
      })
      .mockResolvedValueOnce({
        events: [event({ id: 71, attempt_id: 20, attempt_no: 2, seq: 2 })],
        next_cursor: null,
      });
    fireEvent.change(screen.getByLabelText('Execution'), { target: { value: '20' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Load more activity' }));
    await screen.findByRole('article', { name: 'Execution 2 event 2' });
    expect(getAdminRunActivity).toHaveBeenLastCalledWith(
      'job-1',
      { attempt_id: 20, limit: 100, cursor: 'second-attempt' },
      expect.any(AbortSignal),
    );
    expect(screen.queryByRole('article', { name: 'Execution 1 event 1' })).not.toBeInTheDocument();
    const pending = deferred<Awaited<ReturnType<typeof revealAdminEventContent>>>();
    vi.mocked(revealAdminEventContent).mockReturnValueOnce(pending.promise);
    fireEvent.click(screen.getAllByRole('button', { name: 'Reveal content' })[0]);
    auth.access = { allowed: true, permissions: ['agent.use'] };
    view.rerender(<AdminRunPage />);
    expect(vi.mocked(revealAdminEventContent).mock.lastCall?.[2]?.aborted).toBe(true);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
  });
});

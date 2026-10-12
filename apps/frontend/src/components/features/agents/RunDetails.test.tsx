// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  createAgentJob,
  getAgentJobUsage,
  refreshAgentJobUsage,
  type AgentJobApi,
  type AgentJobUsageApi,
  type AgentThreadEventApi,
} from '@/lib/api/agents';

import { RunDetails } from './RunDetails';
import { PUSH_BATCH_MS, receiveHint, resetAgentPush } from './agentPush';
import type { AgentJob } from './types';

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => navigation,
}));

vi.mock('@/lib/api/agents', () => ({
  createAgentJob: vi.fn(),
  getAgentJobUsage: vi.fn(),
  refreshAgentJobUsage: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(getAgentJobUsage).mockReset();
  vi.mocked(refreshAgentJobUsage).mockReset();
  vi.mocked(createAgentJob).mockReset();
  navigation.push.mockReset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function event(over: Partial<AgentThreadEventApi> & { id: number }): AgentThreadEventApi {
  return {
    attempt_id: 1,
    seq: over.id,
    event_type: 'message',
    payload: {},
    created_at: '2026-08-19T10:00:00.000Z',
    job_id: 'ajob_0',
    turn_no: 2,
    ...over,
  } as AgentThreadEventApi;
}

const HISTORY_JOB = {
  id: 'ajob_0',
  turn_no: 2,
  state: 'succeeded',
  repo: 'acme/widgets',
  task_prompt: '  Add checkout to the catalog page  ',
  runtime: 'codex',
  model: 'gpt-observed',
  reasoning_effort: 'high',
  started_at: '2026-08-19T09:00:00.000Z',
  finished_at: '2026-08-19T09:02:00.000Z',
  attempt_telemetry: [
    {
      attempt_id: 4,
      attempt_no: 1,
      continuity_mode: 'warm_native',
      context_status: 'restored',
      duration_ms: 120000,
    },
  ],
} as unknown as AgentJobApi;

const CURRENT_JOB = {
  id: 'ajob_1',
  runtime: 'claude-code',
  model: 'qwen-test',
  turnNo: 7,
} as AgentJob;

function usage(overrides: Partial<AgentJobUsageApi['ledger']> = {}): AgentJobUsageApi {
  return {
    ledger: {
      status: 'available',
      source: 'gateway_api_logs',
      attribution: 'grant_time_window',
      finalized: false,
      totals: {
        calls: 3,
        tokens_in: 1000,
        tokens_out: 200,
        cache_read: null,
        cache_write: null,
        reasoning: null,
        spent_usd: '0.42',
      },
      unknown_rows: null,
      estimated_calls: 0,
      queried_at: null,
      missing_attempts: 0,
      attempts: [
        {
          attempt_id: 4,
          attempt_no: 1,
          status: 'available',
          reason: null,
          totals: {
            calls: 3,
            tokens_in: 1000,
            tokens_out: 200,
            cache_read: null,
            cache_write: null,
            reasoning: null,
            spent_usd: '0.42',
          },
          unknown_rows: null,
          estimated_calls: 0,
          queried_at: null,
          windows: [],
        },
      ],
      ...overrides,
    },
    runtime: { source: 'runtime', input_tokens: 900, output_tokens: null, cache_read_tokens: null },
  };
}

/** The ledger as it reads while a queued refresh has not run yet. */
function pendingUsage(): AgentJobUsageApi {
  return usage({ status: 'pending', refreshing: true, totals: null });
}

describe('RunDetails', () => {
  it('describes the selected turn, not the turn the page is showing', () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(usage());
    render(
      <RunDetails
        jobId="ajob_0"
        turnNo={2}
        tab="overview"
        job={HISTORY_JOB}
        fallback={CURRENT_JOB}
        events={[event({ id: 1 }), event({ id: 2, job_id: 'ajob_1', turn_no: 7 })]}
        onOpenGitDiff={vi.fn()}
      />,
    );

    expect(screen.getByText('codex')).toBeInTheDocument();
    expect(screen.getByText('gpt-observed')).toBeInTheDocument();
    expect(screen.getByText('high')).toBeInTheDocument();
    expect(screen.queryByText('qwen-test')).toBeNull();
    expect(screen.getByText('Attempt 1')).toBeInTheDocument();
    // Elapsed for the turn and the recorded attempt duration are separate
    // readings of the same two minutes, and both stay visible.
    expect(screen.getAllByText('2m')).toHaveLength(2);
  });

  it('falls back to the loaded turn when the thread row is not in hand', () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    render(
      <RunDetails
        jobId="ajob_1"
        turnNo={7}
        tab="overview"
        fallback={CURRENT_JOB}
        events={[]}
        onOpenGitDiff={vi.fn()}
      />,
    );

    expect(screen.getByText('qwen-test')).toBeInTheDocument();
  });

  it('uses a matching loaded turn for its status summary when the thread row is unavailable', () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    render(
      <RunDetails
        jobId="ajob_1"
        turnNo={7}
        tab="overview"
        fallback={{
          ...CURRENT_JOB,
          state: 'done',
          startedAt: '2026-08-19T09:00:00.000Z',
          finishedAt: '2026-08-19T09:02:00.000Z',
        }}
        events={[]}
        onOpenGitDiff={vi.fn()}
      />,
    );

    const summary = screen.getByRole('region', { name: 'Turn 7 summary' });
    expect(within(summary).getByRole('heading', { name: 'Completed' })).toBeInTheDocument();
    expect(within(summary).getByText('2m')).toBeInTheDocument();
  });

  it('never borrows status, failure, or metadata from another turn when a historical row is missing', () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    render(
      <RunDetails
        jobId="ajob_missing"
        turnNo={2}
        tab="overview"
        fallback={{
          ...CURRENT_JOB,
          state: 'failed',
          stateNote: 'The latest turn failed.',
          startedAt: '2026-08-19T09:00:00.000Z',
          finishedAt: '2026-08-19T09:02:00.000Z',
          prUrl: 'https://github.test/current-pr',
        }}
        events={[]}
        onOpenGitDiff={vi.fn()}
      />,
    );

    expect(screen.getByRole('heading', { name: 'Status not recorded' })).toBeInTheDocument();
    expect(screen.queryByText('qwen-test')).not.toBeInTheDocument();
    expect(screen.queryByText('The latest turn failed.')).not.toBeInTheDocument();
    expect(screen.queryByText('2m')).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Open draft pull request' })).not.toBeInTheDocument();
    expect(screen.queryByText(/follow-up/)).not.toBeInTheDocument();
  });

  it('shows the selected turn’s owner-facing failure reason and retry guidance', () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    render(
      <RunDetails
        jobId="ajob_0"
        turnNo={2}
        tab="overview"
        job={{
          ...HISTORY_JOB,
          state: 'failed',
          detail: 'shared Agent CLI turn exceeded its execution timeout',
        }}
        fallback={{ ...CURRENT_JOB, state: 'failed', stateNote: 'Unrelated current failure' }}
        events={[]}
        onOpenGitDiff={vi.fn()}
      />,
    );

    const summary = screen.getByRole('region', { name: 'Turn 2 summary' });
    expect(within(summary).getByRole('heading', { name: 'Failed' })).toBeInTheDocument();
    expect(summary).toHaveTextContent('This response took too long and was stopped.');
    expect(summary).toHaveTextContent(
      'Review these details before sending a follow-up to try again.',
    );
    expect(screen.queryByText('Unrelated current failure')).not.toBeInTheDocument();
  });

  it('reports missing failure details without inventing a reason', () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    render(
      <RunDetails
        jobId="ajob_0"
        turnNo={2}
        tab="overview"
        job={{ ...HISTORY_JOB, state: 'failed', detail: null }}
        events={[]}
        onOpenGitDiff={vi.fn()}
      />,
    );

    expect(screen.getByText(/No failure details were recorded/)).toBeInTheDocument();
  });

  it.each([
    {
      detail: 'source checkout exceeds the retained import size limit',
      guidance: /Ask an admin to raise the repository import limit/,
    },
    {
      detail: 'Site misconfigured: no MCP relay is configured. Ask an administrator.',
      guidance: /An admin needs to finish setting up the site/,
    },
  ])('gives an actionable prerequisite instead of a retry for $detail', ({ detail, guidance }) => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    render(
      <RunDetails
        jobId="ajob_0"
        turnNo={2}
        tab="overview"
        job={{ ...HISTORY_JOB, state: 'failed', detail }}
        events={[]}
        onOpenGitDiff={vi.fn()}
      />,
    );

    expect(screen.getByText(guidance)).toBeInTheDocument();
    expect(screen.queryByText(/sending a follow-up to try again/)).not.toBeInTheDocument();
  });

  it('explains lost context without offering to continue the same chat', () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    render(
      <RunDetails
        jobId="ajob_0"
        turnNo={2}
        tab="overview"
        job={{ ...HISTORY_JOB, state: 'failed', context_status: 'lost', detail: null }}
        events={[]}
        onOpenGitDiff={vi.fn()}
      />,
    );

    expect(screen.getByText(/Start a new chat to continue/)).toBeInTheDocument();
    expect(screen.queryByText(/sending a follow-up to try again/)).not.toBeInTheDocument();
  });

  it('offers to restart a failed run in a new session, then navigates to it', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_new' } as AgentJobApi);
    render(
      <RunDetails
        jobId="ajob_0"
        turnNo={2}
        tab="overview"
        job={{ ...HISTORY_JOB, state: 'failed', detail: 'Build step timed out.' }}
        events={[]}
        onOpenGitDiff={vi.fn()}
      />,
    );

    const restart = screen.getByRole('button', { name: 'Restart in a new session' });
    expect(restart).toBeInTheDocument();
    fireEvent.click(restart);

    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith({
        repo: 'acme/widgets',
        task_prompt: 'Add checkout to the catalog page',
        model: 'gpt-observed',
        reasoning_effort: 'high',
      }),
    );
    expect(navigation.push).toHaveBeenCalledWith('/ajob_new');
  });

  it('does not offer to restart in a new session for a run that is not failed', () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    render(
      <RunDetails
        jobId="ajob_0"
        turnNo={2}
        tab="overview"
        job={HISTORY_JOB}
        events={[]}
        onOpenGitDiff={vi.fn()}
      />,
    );

    expect(
      screen.queryByRole('button', { name: 'Restart in a new session' }),
    ).not.toBeInTheDocument();
  });

  it('offers the workspace diff while saying it is the current workspace', () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    const onOpenGitDiff = vi.fn();
    render(
      <RunDetails
        jobId="ajob_0"
        turnNo={2}
        tab="overview"
        job={HISTORY_JOB}
        events={[]}
        onOpenGitDiff={onOpenGitDiff}
      />,
    );

    expect(screen.getByText(/current shared workspace, not this turn/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Open current Git diff' }));
    expect(onOpenGitDiff).toHaveBeenCalled();
  });

  it('keeps every ledger field that was recorded and never invents a zero cost', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(
      usage({
        totals: {
          calls: 3,
          tokens_in: 1000,
          tokens_out: 200,
          cache_read: null,
          cache_write: null,
          reasoning: null,
          spent_usd: null,
        },
      }),
    );
    render(
      <RunDetails
        jobId="ajob_0"
        turnNo={2}
        tab="usage"
        job={HISTORY_JOB}
        events={[]}
        onOpenGitDiff={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByText('1,000')).toBeInTheDocument());
    expect(screen.queryByText('$0')).toBeNull();
    expect(screen.getAllByText('Not recorded').length).toBeGreaterThan(0);
  });

  it('shows usage the change stream sent without reading it again', async () => {
    vi.useFakeTimers();
    vi.mocked(getAgentJobUsage).mockResolvedValue(usage());
    await act(async () => {
      render(
        <RunDetails
          jobId="ajob_pushed"
          turnNo={2}
          tab="usage"
          job={HISTORY_JOB}
          events={[]}
          onOpenGitDiff={vi.fn()}
        />,
      );
    });
    expect(screen.getByText('1,000')).toBeInTheDocument();
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);

    const pushed = usage();
    pushed.ledger.totals = { ...pushed.ledger.totals!, tokens_in: 4321 };
    act(() => receiveHint('usage', 'ajob_pushed', pushed));
    await act(async () => vi.advanceTimersByTimeAsync(PUSH_BATCH_MS));

    expect(screen.getByText('4,321')).toBeInTheDocument();
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);
    resetAgentPush();
  });

  it('keeps the newer of the usage it read and the usage it was pushed', async () => {
    vi.useFakeTimers();
    const read = usage();
    read.version = '0005';
    read.ledger.totals = { ...read.ledger.totals!, tokens_in: 5555 };
    vi.mocked(getAgentJobUsage).mockResolvedValue(read);
    await act(async () => {
      render(
        <RunDetails
          jobId="ajob_versions"
          turnNo={2}
          tab="usage"
          job={HISTORY_JOB}
          events={[]}
          onOpenGitDiff={vi.fn()}
        />,
      );
    });
    expect(screen.getByText('5,555')).toBeInTheDocument();

    // Computed before that read, delivered after it.
    const older = usage();
    older.version = '0004';
    older.ledger.totals = { ...older.ledger.totals!, tokens_in: 4444 };
    act(() => receiveHint('usage', 'ajob_versions', older));
    await act(async () => vi.advanceTimersByTimeAsync(PUSH_BATCH_MS));
    expect(screen.getByText('5,555')).toBeInTheDocument();
    expect(screen.queryByText('4,444')).toBeNull();
    resetAgentPush();
  });

  it("keeps a newer pushed usage over the answer to the reader's refresh", async () => {
    vi.useFakeTimers();
    const before = usage();
    before.version = '0002';
    before.ledger.totals = { ...before.ledger.totals!, tokens_in: 2000 };
    vi.mocked(getAgentJobUsage)
      .mockResolvedValueOnce(before)
      // Later reads are slow: only the refresh's answer can change what is shown.
      .mockImplementation(() => new Promise(() => undefined));
    await act(async () => {
      render(
        <RunDetails
          jobId="ajob_refreshed"
          turnNo={2}
          tab="usage"
          job={HISTORY_JOB}
          events={[]}
          onOpenGitDiff={vi.fn()}
        />,
      );
    });
    expect(screen.getByText('2,000')).toBeInTheDocument();

    const pushed = usage();
    pushed.version = '0003';
    pushed.ledger.totals = { ...pushed.ledger.totals!, tokens_in: 3000 };
    act(() => receiveHint('usage', 'ajob_refreshed', pushed));
    await act(async () => vi.advanceTimersByTimeAsync(PUSH_BATCH_MS));
    expect(screen.getByText('3,000')).toBeInTheDocument();

    // The refresh answers with the cached usage as it was before that push.
    let answer!: (value: AgentJobUsageApi) => void;
    vi.mocked(refreshAgentJobUsage).mockImplementation(
      () => new Promise((resolve) => (answer = resolve)),
    );
    fireEvent.click(screen.getAllByRole('button', { name: 'Refresh' })[0]);
    await act(async () => answer(before));

    expect(screen.getByText('3,000')).toBeInTheDocument();
    expect(screen.queryByText('2,000')).toBeNull();
    resetAgentPush();
  });

  it('says the deployment has no ledger instead of showing nothing', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    render(
      <RunDetails
        jobId="ajob_without_ledger"
        turnNo={2}
        tab="usage"
        job={HISTORY_JOB}
        events={[]}
        onOpenGitDiff={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByText(/no billing ledger/)).toBeInTheDocument());
  });

  it('exports only this turn and groups it by attempt', () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(usage());
    render(
      <RunDetails
        jobId="ajob_0"
        turnNo={2}
        tab="raw"
        job={HISTORY_JOB}
        events={[
          event({
            id: 1,
            attempt_id: 4,
            event_type: 'tool_use',
            payload: { id: 't', name: 'Bash' },
          }),
          event({ id: 2, attempt_id: 4 }),
          event({ id: 3, job_id: 'ajob_1', turn_no: 7 }),
        ]}
        onOpenGitDiff={vi.fn()}
      />,
    );

    expect(screen.getByText('2 stored events for Turn 2, grouped by attempt.')).toBeInTheDocument();
    expect(screen.getByText('Attempt 4 · 2 events')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download' })).toBeInTheDocument();
  });

  it('reads the finished ledger instead of stopping at the queued snapshot', async () => {
    vi.useFakeTimers();
    vi.mocked(getAgentJobUsage).mockResolvedValue(pendingUsage());
    vi.mocked(refreshAgentJobUsage).mockResolvedValue(pendingUsage());
    await act(async () => {
      render(
        <RunDetails
          jobId="ledger-refresh"
          turnNo={2}
          tab="usage"
          events={[]}
          onOpenGitDiff={vi.fn()}
        />,
      );
    });
    expect(screen.getByText('Pending')).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    });
    // Asking for a refresh only queues it. The read that follows is the one
    // that can see what the worker wrote.
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);

    vi.mocked(getAgentJobUsage).mockResolvedValue(usage());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    expect(screen.queryByText('Available')).toBeInTheDocument();
  });

  it('keeps reading an available ledger whose next refresh is already scheduled', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-11T10:00:00.000Z'));
    const scheduled = usage({ next_refresh_at: '2026-09-11T10:00:05.000Z' });
    vi.mocked(getAgentJobUsage).mockResolvedValue(scheduled);
    vi.mocked(refreshAgentJobUsage).mockResolvedValue(scheduled);
    await act(async () => {
      render(
        <RunDetails
          jobId="ledger-scheduled"
          turnNo={2}
          tab="usage"
          events={[]}
          onOpenGitDiff={vi.fn()}
        />,
      );
    });
    expect(screen.getByText('Available')).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    });
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);

    // Available but with a refresh the server has already scheduled is a
    // snapshot that is going to change, so the panel reads it when it is due
    // instead of caching the old total for the next open.
    vi.mocked(getAgentJobUsage).mockResolvedValue(usage());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6_000);
    });
    expect(getAgentJobUsage).toHaveBeenCalledTimes(3);

    // With nothing queued and nothing scheduled, the reads stop.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });
    expect(getAgentJobUsage).toHaveBeenCalledTimes(3);
  });

  it('waits out a scheduled refresh instead of spending its reads before it', async () => {
    vi.useFakeTimers();
    // A terminal turn: its 30-second refresh has just completed and the server
    // scheduled the last one five minutes after the turn finished.
    const openedAt = Date.parse('2026-09-11T10:00:30.000Z');
    const refreshAt = '2026-09-11T10:05:00.000Z';
    const due = Date.parse(refreshAt);
    vi.setSystemTime(new Date(openedAt));
    const reads: number[] = [];
    vi.mocked(getAgentJobUsage).mockImplementation(async () => {
      reads.push(Date.now());
      return Date.now() >= due ? usage() : usage({ next_refresh_at: refreshAt });
    });
    await act(async () => {
      render(
        <RunDetails
          jobId="ledger-far-schedule"
          turnNo={2}
          tab="usage"
          events={[]}
          onOpenGitDiff={vi.fn()}
        />,
      );
    });
    expect(reads).toHaveLength(1);

    // Polling towards the schedule would spend the panel's reads long before
    // the answer exists. The read belongs at the time the server named.
    for (let elapsed = 0; elapsed < 285_000; elapsed += 15_000) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });
    }
    expect(reads).toHaveLength(2);
    expect(reads[1]).toBeGreaterThanOrEqual(due);
  });

  it('revalidates an unfinished cached ledger when the panel is reopened', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(pendingUsage());
    const element = (
      <RunDetails
        jobId="ledger-reopen"
        turnNo={2}
        tab="usage"
        events={[]}
        onOpenGitDiff={vi.fn()}
      />
    );
    const first = render(element);
    await waitFor(() => expect(screen.getByText('Pending')).toBeInTheDocument());
    first.unmount();

    // A settled snapshot is worth reusing; one still waiting for the worker is
    // not, or reopening the panel would show yesterday's state for good.
    vi.mocked(getAgentJobUsage).mockResolvedValue(usage());
    render(element);
    await waitFor(() => expect(screen.getByText('Available')).toBeInTheDocument());
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);
  });
});

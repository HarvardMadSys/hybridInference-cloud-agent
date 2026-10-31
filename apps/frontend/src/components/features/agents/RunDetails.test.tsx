// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  createAgentJob,
  getAgentJobUsage,
  type AgentJobApi,
  type AgentJobUsageApi,
  type AgentThreadEventApi,
} from '@/lib/api/agents';

import { RunDetails } from './RunDetails';
import { AgentReadProvider } from './AgentReadProvider';
import { PUSH_BATCH_MS, receiveHint, resetAgentPush } from './agentPush';
import type { AgentJob } from './types';

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => navigation,
}));

vi.mock('@/lib/api/agents', () => ({
  createAgentJob: vi.fn(),
  getAgentJobUsage: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(getAgentJobUsage).mockReset();
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

function usage(): AgentJobUsageApi {
  return {
    runtime: { source: 'runtime', input_tokens: 1000, output_tokens: 200, cache_read_tokens: null },
  };
}

/** Usage whose runtime-reported input names the snapshot, for assertions. */
function usageWithTokens(tokensIn: number): AgentJobUsageApi {
  const snapshot = usage();
  snapshot.runtime = { ...snapshot.runtime, input_tokens: tokensIn };
  return snapshot;
}

/** Every panel mounts inside the shell's per-owner read scope, as the app does. */
function renderPanel(element: ReactElement) {
  return render(<AgentReadProvider>{element}</AgentReadProvider>);
}

describe('RunDetails', () => {
  it('describes the selected turn, not the turn the page is showing', () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(usage());
    renderPanel(
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
    renderPanel(
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
    renderPanel(
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
    renderPanel(
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
    renderPanel(
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
    renderPanel(
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
    renderPanel(
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
    renderPanel(
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
    renderPanel(
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
      expect(createAgentJob).toHaveBeenCalledWith(
        {
          repo: 'acme/widgets',
          task_prompt: 'Add checkout to the catalog page',
          model: 'gpt-observed',
          reasoning_effort: 'high',
        },
        { account: undefined },
      ),
    );
    expect(navigation.push).toHaveBeenCalledWith('/ajob_new');
  });

  it('does not offer to restart in a new session for a run that is not failed', () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    renderPanel(
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
    renderPanel(
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

  it('shows what the agent reported and says the provider bills the cost', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(usage());
    renderPanel(
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
    expect(screen.getByText('200')).toBeInTheDocument();
    expect(screen.getByText(/your provider bills it/)).toBeInTheDocument();
    // An unreported counter is not recorded, never zero; no cost is shown.
    expect(screen.getAllByText('Not recorded').length).toBeGreaterThan(0);
    expect(screen.queryByText(/\$/)).toBeNull();
    expect(screen.queryByRole('button', { name: 'Refresh' })).toBeNull();
  });

  it('says a turn on an owner connection is billed by that provider, and stops polling', async () => {
    vi.useFakeTimers();
    vi.mocked(getAgentJobUsage).mockResolvedValue({ ...usage(), external_provider: true });
    renderPanel(
      <RunDetails
        jobId="ajob_0"
        turnNo={2}
        tab="usage"
        job={HISTORY_JOB}
        events={[]}
        onOpenGitDiff={vi.fn()}
      />,
    );
    await act(async () => vi.advanceTimersByTimeAsync(0));
    expect(screen.getByText(/your provider bills it/)).toBeInTheDocument();
    expect(screen.queryByText('Ledger')).toBeNull();
    expect(screen.queryByText(/\$/)).toBeNull();
    // A pending Gateway ledger would be read again; this one never fills in.
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it('shows usage the change stream sent without reading it again', async () => {
    vi.useFakeTimers();
    vi.mocked(getAgentJobUsage).mockResolvedValue(usage());
    await act(async () => {
      renderPanel(
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
    pushed.runtime = { ...pushed.runtime, input_tokens: 4321 };
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
    read.runtime = { ...read.runtime, input_tokens: 5555 };
    vi.mocked(getAgentJobUsage).mockResolvedValue(read);
    await act(async () => {
      renderPanel(
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
    older.runtime = { ...older.runtime, input_tokens: 4444 };
    act(() => receiveHint('usage', 'ajob_versions', older));
    await act(async () => vi.advanceTimersByTimeAsync(PUSH_BATCH_MS));
    expect(screen.getByText('5,555')).toBeInTheDocument();
    expect(screen.queryByText('4,444')).toBeNull();
    resetAgentPush();
  });

  it('exports only this turn and groups it by attempt', () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(usage());
    renderPanel(
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

  it('reuses a settled snapshot when the same account reopens the panel', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(usage());
    const panel = (
      <RunDetails
        jobId="ledger-same-owner"
        turnNo={2}
        tab="usage"
        events={[]}
        onOpenGitDiff={vi.fn()}
      />
    );
    const view = render(<AgentReadProvider>{panel}</AgentReadProvider>);
    await waitFor(() => expect(screen.getByText('1,000')).toBeInTheDocument());
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);

    // The shell keeps the provider mounted across one panel closing; the same
    // account reopening the turn reads nothing new.
    view.rerender(<AgentReadProvider>{null}</AgentReadProvider>);
    view.rerender(<AgentReadProvider>{panel}</AgentReadProvider>);
    await waitFor(() => expect(screen.getByText('1,000')).toBeInTheDocument());
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);
  });

  it('shows the next account its own usage, never the previous account snapshot', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(usageWithTokens(1111));
    const first = render(
      <AgentReadProvider userId="alice">
        <RunDetails
          jobId="ajob_shared"
          turnNo={2}
          tab="usage"
          events={[]}
          onOpenGitDiff={vi.fn()}
        />
      </AgentReadProvider>,
    );
    await waitFor(() => expect(screen.getByText('1,111')).toBeInTheDocument());
    first.unmount();

    vi.mocked(getAgentJobUsage).mockClear();
    vi.mocked(getAgentJobUsage).mockResolvedValue(usageWithTokens(2222));
    render(
      <AgentReadProvider userId="bob">
        <RunDetails
          jobId="ajob_shared"
          turnNo={2}
          tab="usage"
          events={[]}
          onOpenGitDiff={vi.fn()}
        />
      </AgentReadProvider>,
    );

    // Bob's scope starts empty: the panel reads his usage and never shows the
    // snapshot it kept for Alice.
    await waitFor(() => expect(screen.getByText('2,222')).toBeInTheDocument());
    expect(screen.queryByText('1,111')).toBeNull();
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);
  });

  it('ignores a usage read that outlives its account', async () => {
    let answer!: (value: AgentJobUsageApi) => void;
    vi.mocked(getAgentJobUsage).mockImplementation(
      () =>
        new Promise<AgentJobUsageApi>((resolve) => {
          answer = resolve;
        }),
    );
    const first = render(
      <AgentReadProvider userId="alice">
        <RunDetails jobId="ajob_late" turnNo={2} tab="usage" events={[]} onOpenGitDiff={vi.fn()} />
      </AgentReadProvider>,
    );
    first.unmount();

    vi.mocked(getAgentJobUsage).mockResolvedValue(usageWithTokens(2222));
    render(
      <AgentReadProvider userId="bob">
        <RunDetails jobId="ajob_late" turnNo={2} tab="usage" events={[]} onOpenGitDiff={vi.fn()} />
      </AgentReadProvider>,
    );
    await waitFor(() => expect(screen.getByText('2,222')).toBeInTheDocument());

    // Alice's read lands after the switch; it belongs to a scope that is gone.
    answer(usageWithTokens(1111));
    await waitFor(() => expect(screen.getByText('2,222')).toBeInTheDocument());
    expect(screen.queryByText('1,111')).toBeNull();
  });

  it('drops a read that left under one account when the same panel joins another', async () => {
    let aliceRead!: (value: AgentJobUsageApi) => void;
    vi.mocked(getAgentJobUsage).mockImplementationOnce(
      () =>
        new Promise<AgentJobUsageApi>((resolve) => {
          aliceRead = resolve;
        }),
    );
    const view = render(
      <AgentReadProvider userId="alice">
        <RunDetails
          jobId="ajob_rejoin"
          turnNo={2}
          tab="usage"
          events={[]}
          onOpenGitDiff={vi.fn()}
        />
      </AgentReadProvider>,
    );

    // The shell swaps the signed-in account without unmounting the panel.
    vi.mocked(getAgentJobUsage).mockResolvedValue(usageWithTokens(2222));
    view.rerender(
      <AgentReadProvider userId="bob">
        <RunDetails
          jobId="ajob_rejoin"
          turnNo={2}
          tab="usage"
          events={[]}
          onOpenGitDiff={vi.fn()}
        />
      </AgentReadProvider>,
    );
    await waitFor(() => expect(screen.getByText('2,222')).toBeInTheDocument());
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);

    // Alice's answer then lands on the still-mounted panel; it belongs to a
    // scope the panel has left.
    aliceRead(usageWithTokens(1111));
    await waitFor(() => expect(screen.getByText('2,222')).toBeInTheDocument());
    expect(screen.queryByText('1,111')).toBeNull();
  });
});

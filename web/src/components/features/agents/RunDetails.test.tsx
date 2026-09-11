// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAgentJobUsage,
  type AgentJobApi,
  type AgentJobUsageApi,
  type AgentThreadEventApi,
} from '@/lib/api/agents';

import { RunDetails } from './RunDetails';
import type { AgentJob } from './types';

vi.mock('@/lib/api/agents', () => ({
  getAgentJobUsage: vi.fn(),
  refreshAgentJobUsage: vi.fn(),
}));

beforeEach(() => {
  vi.mocked(getAgentJobUsage).mockReset();
});

afterEach(cleanup);

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
});

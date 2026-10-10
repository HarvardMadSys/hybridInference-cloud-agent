// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAgentJobUsage,
  refreshAgentJobUsage,
  type AgentJobUsageApi,
  type AgentLedgerTotalsApi,
} from '@/lib/api/agents';

import { LedgerUsage as UsageView, useLedgerUsage } from './LedgerUsage';
import {
  PUSH_BATCH_MS,
  receiveHint,
  receiveJobChange,
  resetAgentPush,
  setAgentPushLive,
} from './agentPush';
import type { AgentJob } from './types';

function LedgerUsage({ job: currentJob }: { job: AgentJob }) {
  const data = useLedgerUsage(currentJob);
  return <UsageView job={currentJob} usage={data} />;
}

vi.mock('@/lib/api/agents', () => ({
  getAgentJobUsage: vi.fn(),
  refreshAgentJobUsage: vi.fn(),
}));

const job = { id: 'ajob_1', state: 'done', runtime: 'claude-code' } as AgentJob;
const totals: AgentLedgerTotalsApi = {
  calls: 3,
  tokens_in: 1200,
  tokens_out: 80,
  cache_read: 1000,
  cache_write: 0,
  reasoning: null,
  spent_usd: '0.12345678',
};

function usage(): AgentJobUsageApi {
  const unknown = {
    tokens_in: 0,
    tokens_out: 0,
    cache_read: 0,
    cache_write: 0,
    reasoning: 3,
    spent_usd: 0,
  };
  return {
    ledger: {
      status: 'partial',
      source: 'gateway_api_logs',
      attribution: 'grant_time_window',
      finalized: false,
      totals: { ...totals },
      unknown_rows: { ...unknown },
      estimated_calls: 0,
      queried_at: '2026-09-09T10:10:00Z',
      missing_attempts: 0,
      refreshing: false,
      attempts: [
        {
          attempt_id: 10,
          attempt_no: 1,
          status: 'partial',
          reason: null,
          totals: { ...totals },
          unknown_rows: { ...unknown },
          estimated_calls: 0,
          queried_at: '2026-09-09T10:10:00Z',
          windows: [
            {
              since: '2026-09-09T10:00:00Z',
              until: '2026-09-09T10:05:00Z',
              basis: 'request_started_at',
            },
          ],
        },
      ],
    },
    runtime: {
      source: 'runtime',
      input_tokens: 200,
      normalized_input_tokens: 1200,
      output_tokens: 80,
      cache_read_tokens: 1000,
    },
  };
}

function unavailable(reason = 'attribution_unavailable'): AgentJobUsageApi {
  const data = usage();
  data.ledger.status = 'unavailable';
  data.ledger.totals = null;
  data.ledger.unknown_rows = null;
  data.ledger.queried_at = null;
  data.ledger.missing_attempts = 1;
  data.ledger.attempts[0] = {
    ...data.ledger.attempts[0],
    status: 'unavailable',
    reason,
    totals: null,
    windows: [],
    queried_at: null,
  };
  return data;
}

async function mount(currentJob = job) {
  let view!: ReturnType<typeof render>;
  await act(async () => {
    view = render(<LedgerUsage job={currentJob} />);
  });
  return view;
}

async function advance(milliseconds: number) {
  await act(async () => vi.advanceTimersByTimeAsync(milliseconds));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-09-09T10:12:00Z'));
  vi.mocked(getAgentJobUsage).mockReset().mockResolvedValue(usage());
  vi.mocked(refreshAgentJobUsage).mockReset().mockResolvedValue(usage());
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('LedgerUsage', () => {
  it('uses one loading skeleton, then only the current run summary', async () => {
    let finish!: (value: AgentJobUsageApi) => void;
    vi.mocked(getAgentJobUsage).mockImplementation(
      () => new Promise((resolve) => (finish = resolve)),
    );
    await mount();
    expect(screen.getAllByRole('status')).toHaveLength(1);
    expect(screen.getByRole('status', { name: 'Loading usage' })).toBeInTheDocument();
    expect(screen.queryByText('Input tokens')).not.toBeInTheDocument();
    expect(screen.queryByText('—')).not.toBeInTheDocument();
    await act(async () => finish(usage()));

    expect(screen.getByLabelText('Run usage')).toHaveTextContent(
      '3 model calls · 1.2k input tokens · 80 output tokens',
    );
    expect(screen.queryByText('Usage details')).not.toBeInTheDocument();
    expect(screen.queryByText('Estimated inference cost')).not.toBeInTheDocument();
    expect(screen.getByTitle('1,200 input tokens')).toHaveTextContent('1.2k input tokens');
    expect(screen.queryByText('Reasoning tokens')).not.toBeInTheDocument();
    expect(screen.queryByText(/Some usage is/)).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /refresh/i })).not.toBeInTheDocument();
    expect(
      screen.queryByText(/gateway|runtime|attempt|retr(y|ies)|snapshot/i),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/Updated at/)).not.toBeInTheDocument();
  });

  it('keeps measured zeros and hides unknown detail values without inventing costs', async () => {
    const data = usage();
    Object.assign(data.ledger.totals!, {
      calls: 0,
      tokens_in: 0,
      tokens_out: 0,
      cache_read: null,
      cache_write: null,
      reasoning: 0,
      spent_usd: null,
    });
    vi.mocked(getAgentJobUsage).mockResolvedValue(data);
    await mount();
    expect(screen.getByLabelText('Run usage')).toHaveTextContent(
      '0 model calls · 0 input tokens · 0 output tokens',
    );
    expect(screen.queryByText('Reasoning tokens')).not.toBeInTheDocument();
    expect(screen.queryByText('Cache read tokens')).not.toBeInTheDocument();
    expect(screen.queryByText('Cache write tokens')).not.toBeInTheDocument();
    expect(screen.queryByText('Estimated inference cost')).not.toBeInTheDocument();
    expect(screen.queryByText('—')).not.toBeInTheDocument();
  });

  it('identifies incomplete primary totals and estimates without exposing reconciliation fields', async () => {
    const data = usage();
    data.ledger.missing_attempts = 1;
    data.ledger.estimated_calls = 1;
    data.ledger.unknown_rows!.tokens_in = 2;
    vi.mocked(getAgentJobUsage).mockResolvedValue(data);
    await mount();
    expect(screen.getByText('Some usage is unavailable.')).toBeInTheDocument();
    expect(screen.getByTitle('1,200 input tokens')).toBeInTheDocument();
    expect(screen.getByText('Some token counts are estimated.')).toBeInTheDocument();
    expect(screen.queryByText(/attempt|missing this value/i)).not.toBeInTheDocument();
  });

  it('falls back only when measured totals are absent and uses normalized inclusive input', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(unavailable());
    await mount();
    expect(screen.getByLabelText('Run usage')).toHaveTextContent(
      '1.2k input tokens · 80 output tokens',
    );
    expect(screen.getByText('Reported by agent.')).toBeInTheDocument();
    expect(screen.getByTitle('1,200 input tokens')).toBeInTheDocument();
    expect(screen.queryByText(/model call/)).not.toBeInTheDocument();
    expect(screen.queryByText('Estimated inference cost')).not.toBeInTheDocument();
    expect(refreshAgentJobUsage).not.toHaveBeenCalled();
  });

  it('does not guess a compatible input count for legacy runtime reports', async () => {
    const data = unavailable();
    delete data.runtime.normalized_input_tokens;
    vi.mocked(getAgentJobUsage).mockResolvedValue(data);
    await mount();
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('80 output tokens');
    expect(screen.getByLabelText('Run usage')).not.toHaveTextContent('input');
    expect(screen.queryByText('Input tokens')).not.toBeInTheDocument();
    expect(screen.queryByText('200')).not.toBeInTheDocument();
  });

  it('does not mistake a cache-only legacy report for total input tokens', async () => {
    const data = unavailable();
    data.runtime.normalized_input_tokens = null;
    data.runtime.output_tokens = null;
    vi.mocked(getAgentJobUsage).mockResolvedValue(data);
    await mount();
    expect(screen.getByText('Usage temporarily unavailable.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Run usage')).not.toBeInTheDocument();
    expect(screen.queryByText('Input tokens')).not.toBeInTheDocument();
    expect(screen.queryByText('Output tokens')).not.toBeInTheDocument();
  });

  it('keeps measured totals primary even when the runtime reports a different count', async () => {
    const data = usage();
    data.runtime.normalized_input_tokens = 9000;
    vi.mocked(getAgentJobUsage).mockResolvedValue(data);
    await mount();
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('1.2k input tokens');
    expect(screen.queryByText('Reported by agent.')).not.toBeInTheDocument();
    expect(screen.queryByText(/9k/)).not.toBeInTheDocument();
  });

  it('never substitutes conversation totals for the current run', async () => {
    const data = unavailable();
    data.conversation = {
      turn_count: 3,
      totals: { ...totals, calls: 6, tokens_in: 2800 },
      unknown_rows: { ...usage().ledger.unknown_rows! },
      estimated_calls: 0,
      queried_at: '2026-09-09T10:09:00Z',
      incomplete: true,
      refreshing: false,
    };
    vi.mocked(getAgentJobUsage).mockResolvedValue(data);
    await mount();
    expect(screen.queryByText('Conversation total')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Conversation usage')).not.toBeInTheDocument();
    expect(screen.queryByText(/6 model calls|2.8k/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('1.2k input tokens');
  });

  it('ignores unavailable conversation totals when this run has usage', async () => {
    const data = unavailable();
    data.conversation = {
      turn_count: 3,
      totals: null,
      unknown_rows: null,
      estimated_calls: 0,
      queried_at: null,
      incomplete: true,
      refreshing: false,
    };
    vi.mocked(getAgentJobUsage).mockResolvedValue(data);
    await mount();
    expect(screen.queryByLabelText('Conversation usage')).not.toBeInTheDocument();
    expect(screen.queryByText('Usage temporarily unavailable.')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('1.2k input tokens');
  });

  it('treats an unsupported deployment as unavailable without displaying old placeholder counts', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(null);
    await mount({ ...job, state: 'running', usage: { turns: 0 } } as AgentJob);
    expect(screen.getByText('Usage temporarily unavailable.')).toBeInTheDocument();
    expect(screen.queryByText(/0 model calls|Estimated inference cost/)).not.toBeInTheDocument();
    await advance(600_000);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);
    expect(refreshAgentJobUsage).not.toHaveBeenCalled();
  });

  it('requests fresh source usage throughout active runs, including after five minutes', async () => {
    const view = await mount({ ...job, state: 'running' });
    expect(refreshAgentJobUsage).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Counting usage…')).toBeVisible();
    await advance(360_000);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(25);
    expect(refreshAgentJobUsage).toHaveBeenCalledTimes(25);
    expect(screen.getByText('Counting usage…')).toBeVisible();
    expect(refreshAgentJobUsage).toHaveBeenLastCalledWith('ajob_1', expect.any(AbortSignal));
    view.unmount();
    await advance(60_000);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(25);
    expect(refreshAgentJobUsage).toHaveBeenCalledTimes(25);
  });

  it('reads in-flight and throttled work before requesting another source update', async () => {
    const busy = usage();
    busy.ledger.refreshing = true;
    const delayed = usage();
    delayed.ledger.next_refresh_at = new Date(Date.now() + 30_000).toISOString();
    vi.mocked(getAgentJobUsage)
      .mockResolvedValueOnce(busy)
      .mockResolvedValueOnce(delayed)
      .mockResolvedValue(usage());
    await mount({ ...job, state: 'running' });
    expect(refreshAgentJobUsage).not.toHaveBeenCalled();
    await advance(15_000);
    expect(refreshAgentJobUsage).not.toHaveBeenCalled();
    await advance(15_000);
    expect(refreshAgentJobUsage).toHaveBeenCalledTimes(1);
  });

  it('follows terminal delayed checks without pulling them forward or declaring final totals', async () => {
    const delayed = usage();
    delayed.ledger.next_refresh_at = new Date(Date.now() + 30_000).toISOString();
    const updated = usage();
    updated.ledger.totals!.calls = 4;
    updated.ledger.next_refresh_at = null;
    vi.mocked(getAgentJobUsage)
      .mockResolvedValueOnce(delayed)
      .mockResolvedValueOnce(delayed)
      .mockResolvedValue(updated);
    await mount();
    expect(screen.getByText(/Usage may change/)).toBeInTheDocument();
    expect(screen.queryByText(/Updating/)).not.toBeInTheDocument();
    await advance(30_000);
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('4 model calls');
    expect(screen.queryByText(/Usage may change/)).not.toBeInTheDocument();
    expect(screen.queryByText(/final|completed|settled/i)).not.toBeInTheDocument();
    await advance(600_000);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(3);
    expect(refreshAgentJobUsage).not.toHaveBeenCalled();
  });

  it.each(['active', 'refreshing', 'scheduled'])(
    'does not poll a settled run for a sibling that is %s',
    async (state) => {
      const data = usage();
      data.conversation = {
        turn_count: 2,
        totals: { ...totals, calls: 5 },
        unknown_rows: data.ledger.unknown_rows,
        estimated_calls: 0,
        queried_at: data.ledger.queried_at,
        incomplete: false,
        active: state === 'active',
        refreshing: state === 'refreshing',
        next_refresh_at: state === 'scheduled' ? new Date(Date.now() + 30_000).toISOString() : null,
      };
      vi.mocked(getAgentJobUsage).mockResolvedValue(data);
      await mount();
      await advance(360_000);
      expect(getAgentJobUsage).toHaveBeenCalledTimes(1);
      expect(refreshAgentJobUsage).not.toHaveBeenCalled();
      expect(screen.getByLabelText('Run usage')).toHaveTextContent('3 model calls');
      expect(screen.queryByLabelText('Conversation usage')).not.toBeInTheDocument();
    },
  );

  it('keeps cached values when a request fails, then recovers automatically', async () => {
    vi.mocked(refreshAgentJobUsage).mockRejectedValueOnce(new Error('secret upstream details'));
    const updated = usage();
    updated.ledger.totals!.calls = 4;
    vi.mocked(getAgentJobUsage).mockResolvedValueOnce(usage()).mockResolvedValue(updated);
    await mount({ ...job, state: 'running' });
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('3 model calls');
    expect(screen.getByText(/Update temporarily unavailable/)).toBeInTheDocument();
    expect(screen.queryByText('secret upstream details')).not.toBeInTheDocument();
    vi.mocked(refreshAgentJobUsage).mockResolvedValue(updated);
    await advance(15_000);
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('4 model calls');
    expect(screen.queryByText(/Update temporarily unavailable/)).not.toBeInTheDocument();
  });

  it('retains measured values when a later response loses the source instead of switching to runtime', async () => {
    vi.mocked(refreshAgentJobUsage).mockResolvedValue(unavailable('usage_unavailable'));
    await mount({ ...job, state: 'running' });
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('3 model calls');
    expect(screen.getByText(/Update temporarily unavailable/)).toBeInTheDocument();
    expect(screen.queryByText('Reported by agent.')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('1.2k input tokens');
  });

  it('backs off transient terminal source failures and recovers without manual refresh', async () => {
    const stale = usage();
    stale.ledger.status = 'stale';
    stale.ledger.attempts[0].reason = 'usage_unavailable';
    vi.mocked(getAgentJobUsage).mockResolvedValue(stale);
    vi.mocked(refreshAgentJobUsage).mockResolvedValue(stale);
    await mount();
    expect(refreshAgentJobUsage).toHaveBeenCalledTimes(1);
    await advance(15_000);
    expect(refreshAgentJobUsage).toHaveBeenCalledTimes(2);
    await advance(15_000);
    expect(refreshAgentJobUsage).toHaveBeenCalledTimes(2);
    await advance(15_000);
    expect(refreshAgentJobUsage).toHaveBeenCalledTimes(3);
    await advance(30_000);
    expect(refreshAgentJobUsage).toHaveBeenCalledTimes(3);
    const recovered = usage();
    recovered.ledger.totals!.calls = 4;
    vi.mocked(getAgentJobUsage).mockResolvedValue(recovered);
    await advance(30_000);
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('4 model calls');
    expect(screen.queryByText(/Update temporarily unavailable/)).not.toBeInTheDocument();
    const reads = vi.mocked(getAgentJobUsage).mock.calls.length;
    await advance(120_000);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(reads);
  });

  it('backs off failed initial reads and can recover a completed run', async () => {
    vi.mocked(getAgentJobUsage).mockRejectedValue(new Error('network unavailable'));
    await mount();
    expect(screen.getByText('Usage temporarily unavailable.')).toBeInTheDocument();
    await advance(15_000);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);
    await advance(30_000);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(3);
    vi.mocked(getAgentJobUsage).mockResolvedValue(usage());
    await advance(60_000);
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('3 model calls');
    expect(refreshAgentJobUsage).not.toHaveBeenCalled();
  });

  it.each([
    'attribution_unavailable',
    'usage_unconfigured',
    'internal_api_not_configured',
    'binding_changed',
    'unknown_grant',
    'usage_unsupported_schema',
  ])('does not repeatedly request unusable attribution/configuration: %s', async (reason) => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(unavailable(reason));
    await mount({ ...job, state: 'running' });
    await advance(360_000);
    expect(refreshAgentJobUsage).not.toHaveBeenCalled();
    expect(screen.queryByText(reason)).not.toBeInTheDocument();
  });

  it('aborts old requests and rejects their late responses after switching jobs', async () => {
    let finishOld!: (value: AgentJobUsageApi) => void;
    vi.mocked(getAgentJobUsage)
      .mockImplementationOnce(() => new Promise((resolve) => (finishOld = resolve)))
      .mockResolvedValue(null);
    const view = await mount();
    const oldSignal = vi.mocked(getAgentJobUsage).mock.calls[0][1]!;
    await act(async () => view.rerender(<LedgerUsage job={{ ...job, id: 'ajob_2' }} />));
    expect(oldSignal.aborted).toBe(true);
    await act(async () => finishOld(usage()));
    expect(screen.getByText('Usage temporarily unavailable.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Run usage')).not.toBeInTheDocument();
    expect(screen.queryByText('$0.12345678')).not.toBeInTheDocument();
  });

  it('clears already loaded totals immediately when switching jobs', async () => {
    const view = await mount();
    vi.mocked(getAgentJobUsage).mockImplementation(() => new Promise(() => {}));
    view.rerender(<LedgerUsage job={{ ...job, id: 'ajob_2' }} />);
    expect(screen.getByRole('status', { name: 'Loading usage' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Run usage')).not.toBeInTheDocument();
    expect(screen.queryByText('$0.12345678')).not.toBeInTheDocument();
    view.unmount();
    expect(vi.mocked(getAgentJobUsage).mock.calls.at(-1)![1]!.aborted).toBe(true);
  });
});

describe('LedgerUsage with the owner change stream', () => {
  afterEach(() => resetAgentPush());

  function nothingCalled(): AgentJobUsageApi {
    const data = unavailable('no_model_calls');
    data.ledger.status = 'available';
    data.ledger.missing_attempts = 0;
    data.ledger.attempts[0] = { ...data.ledger.attempts[0], status: 'available' };
    data.runtime = {
      source: 'runtime',
      input_tokens: null,
      output_tokens: null,
      cache_read_tokens: null,
      normalized_input_tokens: null,
    };
    return data;
  }

  it('shows nothing for a turn that ended before its agent called a model', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue(nothingCalled());
    await mount({ ...job, state: 'failed' } as AgentJob);
    expect(screen.queryByLabelText('Usage')).not.toBeInTheDocument();
    expect(screen.queryByText(/temporarily unavailable|Counting usage/)).not.toBeInTheDocument();
  });

  it('shows nothing for a turn that never ran', async () => {
    const data = nothingCalled();
    data.ledger.status = 'unavailable';
    data.ledger.attempts = [];
    vi.mocked(getAgentJobUsage).mockResolvedValue(data);
    await mount({ ...job, state: 'cancelled' } as AgentJob);
    expect(screen.queryByLabelText('Usage')).not.toBeInTheDocument();
  });

  it('reads a pushed usage change at once and never asks for a refresh itself', async () => {
    act(() => setAgentPushLive(true));
    await mount({ ...job, state: 'running' } as AgentJob);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);
    expect(refreshAgentJobUsage).not.toHaveBeenCalled();

    // The fifteen-second clock is gone while pushes flow.
    await advance(15_000);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);

    act(() => receiveHint('usage', 'ajob_1'));
    await advance(PUSH_BATCH_MS);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);
    expect(refreshAgentJobUsage).not.toHaveBeenCalled();

    act(() => receiveHint('usage', 'another_job'));
    await advance(PUSH_BATCH_MS);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);
  });

  function versioned(version: string, calls: number): AgentJobUsageApi {
    const data = usage();
    data.version = version;
    data.ledger.totals = { ...data.ledger.totals!, calls };
    return data;
  }

  it('shows usage the stream sent without reading it, over a read that left before it', async () => {
    act(() => setAgentPushLive(true));
    await mount({ ...job, state: 'running' } as AgentJob);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);

    // A read is on its way when the stream sends newer usage.
    let finishOlder!: (value: AgentJobUsageApi) => void;
    vi.mocked(getAgentJobUsage).mockImplementationOnce(
      () => new Promise((resolve) => (finishOlder = resolve)),
    );
    act(() => receiveHint('usage', 'ajob_1'));
    await advance(PUSH_BATCH_MS);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);

    act(() => receiveHint('usage', 'ajob_1', versioned('0002', 7)));
    await advance(PUSH_BATCH_MS);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('7 model calls');

    await act(async () => finishOlder(versioned('0001', 3)));
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('7 model calls');
    expect(refreshAgentJobUsage).not.toHaveBeenCalled();
  });

  it('keeps a read over an older usage pushed after it', async () => {
    act(() => setAgentPushLive(true));
    vi.mocked(getAgentJobUsage).mockResolvedValue(versioned('0005', 9));
    await mount({ ...job, state: 'running' } as AgentJob);
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('9 model calls');

    // Computed before the read, delivered after it.
    act(() => receiveHint('usage', 'ajob_1', versioned('0004', 8)));
    await advance(PUSH_BATCH_MS);
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('9 model calls');
  });

  it('shows a read still on its way when it is newer than a usage pushed meanwhile', async () => {
    act(() => setAgentPushLive(true));
    vi.mocked(getAgentJobUsage).mockResolvedValue(versioned('0001', 1));
    await mount({ ...job, state: 'running' } as AgentJob);

    let finishNewer!: (value: AgentJobUsageApi) => void;
    vi.mocked(getAgentJobUsage).mockImplementationOnce(
      () => new Promise((resolve) => (finishNewer = resolve)),
    );
    act(() => receiveHint('usage', 'ajob_1'));
    await advance(PUSH_BATCH_MS);
    act(() => receiveHint('usage', 'ajob_1', versioned('0002', 2)));
    await advance(PUSH_BATCH_MS);
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('2 model calls');

    await act(async () => finishNewer(versioned('0003', 3)));
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('3 model calls');
  });

  it('keeps the newer usage across a change of the turn it shows', async () => {
    act(() => setAgentPushLive(true));
    vi.mocked(getAgentJobUsage).mockResolvedValue(versioned('0007', 7));
    const view = await mount({ ...job, state: 'running' } as AgentJob);
    // The turn ends: the line reads again and is shown the newer usage...
    await act(async () => {
      view.rerender(<LedgerUsage job={{ ...job, state: 'done' } as AgentJob} />);
    });
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('7 model calls');
    // ...and an older one pushed before the turn ended does not replace it.
    act(() => receiveHint('usage', 'ajob_1', versioned('0006', 6)));
    await advance(PUSH_BATCH_MS);
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('7 model calls');
  });

  it('reads a turn that changed in the same batch rather than trust the usage sent', async () => {
    act(() => setAgentPushLive(true));
    await mount({ ...job, state: 'running' } as AgentJob);
    act(() => {
      receiveJobChange({ id: 'ajob_1', revision: 9, state: 'succeeded' });
      receiveHint('usage', 'ajob_1', usage());
    });
    await advance(PUSH_BATCH_MS);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);
  });
});

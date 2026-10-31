// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentJobUsage, type AgentJobUsageApi } from '@/lib/api/agents';

import { LedgerUsage as UsageView, useLedgerUsage } from './LedgerUsage';
import {
  PUSH_BATCH_MS,
  receiveHint,
  receiveJobChange,
  resetAgentPush,
  setAgentPushLive,
} from './agentPush';
import type { AgentJob } from './types';

vi.mock('@/lib/api/agents', () => ({ getAgentJobUsage: vi.fn() }));

/** The footer as the job view mounts it, and the hook's own state beside it. */
function LedgerUsage({ job: currentJob }: { job: AgentJob }) {
  const data = useLedgerUsage(currentJob);
  return (
    <>
      <UsageView usage={data} />
      <output aria-label="Usage state">
        {data === null
          ? 'none'
          : `${data.jobId}:${data.loading ? 'loading' : ''}${data.failed ? 'failed' : ''}`}
      </output>
    </>
  );
}

const job = { id: 'ajob_1', state: 'done', runtime: 'claude-code' } as AgentJob;

/** What one turn's agent reported: Claude Code's disjoint counters, cache-inclusive input. */
function usage(input = 1200, output = 80): AgentJobUsageApi {
  return {
    runtime: {
      source: 'runtime',
      input_tokens: 200,
      normalized_input_tokens: input,
      output_tokens: output,
      cache_read_tokens: 1000,
      cache_write_tokens: 0,
      reasoning_tokens: null,
    },
    external_provider: true,
  };
}

function versioned(version: string, output: number): AgentJobUsageApi {
  return { ...usage(1200, output), version };
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

const state = () => screen.getByLabelText('Usage state').textContent;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-09-09T10:12:00Z'));
  vi.mocked(getAgentJobUsage).mockReset().mockResolvedValue(usage());
});

afterEach(() => {
  cleanup();
  resetAgentPush();
  vi.useRealTimers();
});

describe('LedgerUsage', () => {
  it('shows the agent-reported tokens and that the provider bills it, never a cost', async () => {
    await mount();
    expect(screen.getByLabelText('Run usage')).toHaveTextContent(
      '1.2k input tokens · 80 output tokens',
    );
    expect(screen.getByTitle('1,200 input tokens')).toHaveTextContent('1.2k input tokens');
    expect(screen.getByText('Reported by agent.')).toBeInTheDocument();
    expect(screen.getByText(/your provider bills it/)).toBeInTheDocument();
    expect(screen.getByLabelText('Usage')).not.toHaveTextContent('$');
    // Agents count no model calls: none is claimed.
    expect(screen.getByLabelText('Usage')).not.toHaveTextContent(/model call/);
    expect(screen.queryByRole('button', { name: /refresh/i })).not.toBeInTheDocument();
  });

  it('keeps measured zeros and hides unknown values without inventing any', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue({
      runtime: {
        source: 'runtime',
        input_tokens: 0,
        normalized_input_tokens: 0,
        output_tokens: 0,
        cache_read_tokens: null,
        cache_write_tokens: null,
        reasoning_tokens: null,
      },
    });
    await mount();
    expect(screen.getByLabelText('Run usage')).toHaveTextContent(
      '0 input tokens · 0 output tokens',
    );
    expect(screen.queryByText('—')).not.toBeInTheDocument();
    expect(screen.getByLabelText('Usage')).not.toHaveTextContent('$');
  });

  it('shows no counts until the agent reports one, and still says who bills it', async () => {
    vi.mocked(getAgentJobUsage).mockResolvedValue({
      runtime: {
        source: 'runtime',
        input_tokens: 200,
        normalized_input_tokens: null,
        output_tokens: null,
        cache_read_tokens: null,
      },
    });
    await mount();
    // Uncached input alone is not a total; it is not shown as one.
    expect(screen.queryByLabelText('Run usage')).not.toBeInTheDocument();
    expect(screen.queryByText(/200/)).not.toBeInTheDocument();
    expect(screen.getByText(/your provider bills it/)).toBeInTheDocument();
  });

  it('never substitutes a conversation total for the current run', async () => {
    // An older server's response, still carrying a conversation's sum.
    vi.mocked(getAgentJobUsage).mockResolvedValue({
      ...usage(),
      conversation: { totals: { tokens_in: 2800, tokens_out: 900 }, turn_count: 3 },
    } as AgentJobUsageApi);
    await mount();
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('1.2k input tokens');
    expect(screen.queryByText(/2\.8k|900/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Conversation usage')).not.toBeInTheDocument();
  });

  it('keeps the values it has when a later read fails, then recovers on its clock', async () => {
    vi.mocked(getAgentJobUsage)
      .mockResolvedValueOnce(usage(1200, 80))
      .mockRejectedValueOnce(new Error('secret upstream details'))
      .mockResolvedValue(usage(1300, 90));
    await mount({ ...job, state: 'running' });
    await advance(15_000);
    expect(state()).toBe('ajob_1:failed');
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('80 output tokens');
    expect(screen.queryByText('secret upstream details')).not.toBeInTheDocument();
    await advance(15_000);
    expect(state()).toBe('ajob_1:');
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('90 output tokens');
  });

  it('backs off failed initial reads and can recover a completed run', async () => {
    vi.mocked(getAgentJobUsage).mockRejectedValue(new Error('network unavailable'));
    await mount();
    expect(state()).toBe('ajob_1:failed');
    await advance(15_000);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);
    await advance(29_999);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);
    await advance(1);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(3);
    // Capped at a minute, however long it keeps failing.
    await advance(60_000);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(4);
    await advance(60_000);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(5);
    vi.mocked(getAgentJobUsage).mockResolvedValue(usage());
    await advance(60_000);
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('1.2k input tokens');
    // A finished turn's settled read is the last one.
    const reads = vi.mocked(getAgentJobUsage).mock.calls.length;
    await advance(300_000);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(reads);
  });

  it('reads a running turn again every fifteen seconds without a push stream', async () => {
    await mount({ ...job, state: 'running' });
    await advance(14_999);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);
    await advance(1);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);
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
    expect(state()).toBe('ajob_2:');
    expect(screen.queryByLabelText('Run usage')).not.toBeInTheDocument();
  });

  it('clears already loaded totals immediately when switching jobs', async () => {
    const view = await mount();
    expect(screen.getByLabelText('Run usage')).toBeInTheDocument();
    vi.mocked(getAgentJobUsage).mockImplementation(() => new Promise(() => {}));
    view.rerender(<LedgerUsage job={{ ...job, id: 'ajob_2' }} />);
    // The first frame for the new job shows none of the old one's numbers.
    expect(screen.queryByLabelText('Run usage')).not.toBeInTheDocument();
    expect(state()).not.toContain('ajob_1');
    await act(async () => {});
    expect(state()).toBe('ajob_2:loading');
    view.unmount();
    expect(vi.mocked(getAgentJobUsage).mock.calls.at(-1)![1]!.aborted).toBe(true);
  });
});

describe('LedgerUsage with the owner change stream', () => {
  it('reads a pushed usage change at once, and only for its own job', async () => {
    act(() => setAgentPushLive(true));
    await mount({ ...job, state: 'running' });
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);

    // The fifteen-second clock is only a backstop while pushes flow.
    await advance(15_000);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(1);

    act(() => receiveHint('usage', 'ajob_1'));
    await advance(PUSH_BATCH_MS);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);

    act(() => receiveHint('usage', 'another_job'));
    await advance(PUSH_BATCH_MS);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);
  });

  it('shows usage the stream sent without reading it, over a read that left before it', async () => {
    act(() => setAgentPushLive(true));
    await mount({ ...job, state: 'running' });

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
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('7 output tokens');

    await act(async () => finishOlder(versioned('0001', 3)));
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('7 output tokens');
  });

  it('keeps a read over an older usage pushed after it', async () => {
    act(() => setAgentPushLive(true));
    vi.mocked(getAgentJobUsage).mockResolvedValue(versioned('0005', 9));
    await mount({ ...job, state: 'running' });
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('9 output tokens');

    // Computed before the read, delivered after it.
    act(() => receiveHint('usage', 'ajob_1', versioned('0004', 8)));
    await advance(PUSH_BATCH_MS);
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('9 output tokens');
  });

  it('shows a read still on its way when it is newer than a usage pushed meanwhile', async () => {
    act(() => setAgentPushLive(true));
    vi.mocked(getAgentJobUsage).mockResolvedValue(versioned('0001', 1));
    await mount({ ...job, state: 'running' });

    let finishNewer!: (value: AgentJobUsageApi) => void;
    vi.mocked(getAgentJobUsage).mockImplementationOnce(
      () => new Promise((resolve) => (finishNewer = resolve)),
    );
    act(() => receiveHint('usage', 'ajob_1'));
    await advance(PUSH_BATCH_MS);
    act(() => receiveHint('usage', 'ajob_1', versioned('0002', 2)));
    await advance(PUSH_BATCH_MS);
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('2 output tokens');

    await act(async () => finishNewer(versioned('0003', 3)));
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('3 output tokens');
  });

  it('keeps the newer usage across a change of the turn it shows', async () => {
    act(() => setAgentPushLive(true));
    vi.mocked(getAgentJobUsage).mockResolvedValue(versioned('0007', 7));
    const view = await mount({ ...job, state: 'running' });
    await act(async () => {
      view.rerender(<LedgerUsage job={{ ...job, state: 'done' } as AgentJob} />);
    });
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('7 output tokens');
    act(() => receiveHint('usage', 'ajob_1', versioned('0006', 6)));
    await advance(PUSH_BATCH_MS);
    expect(screen.getByLabelText('Run usage')).toHaveTextContent('7 output tokens');
  });

  it('reads a turn that changed in the same batch rather than trust the usage sent', async () => {
    act(() => setAgentPushLive(true));
    await mount({ ...job, state: 'running' });
    act(() => {
      receiveJobChange({ id: 'ajob_1', revision: 9, state: 'succeeded' });
      receiveHint('usage', 'ajob_1', usage());
    });
    await advance(PUSH_BATCH_MS);
    expect(getAgentJobUsage).toHaveBeenCalledTimes(2);
  });
});

it('names provider-owned billing before any usage response arrives', () => {
  render(<UsageView usage={null} />);
  expect(screen.getByText(/your provider bills it/)).toBeInTheDocument();
  expect(screen.getByLabelText('Usage')).not.toHaveTextContent('$');
});

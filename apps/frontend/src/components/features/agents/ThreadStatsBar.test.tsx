// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentThreadStats, type AgentThreadStatsApi } from '@/lib/api/agents';

import { ThreadStatsBar, formatCount, statSegments } from './ThreadStatsBar';
import type { AgentJob } from './types';

vi.mock('@/lib/api/agents', () => ({
  getAgentThreadStats: vi.fn(),
}));

function makeStats(overrides: Partial<AgentThreadStatsApi> = {}): AgentThreadStatsApi {
  return {
    thread_id: 'athr_1',
    turns: 3,
    tool_calls: 56,
    failed_tool_calls: 0,
    wall_seconds: 798,
    model_seconds: 778,
    tool_seconds: 19.7,
    ttft_seconds: 2,
    input_tokens: 5_800_000,
    output_tokens: 89_700,
    cache_read_tokens: 0,
    cache_hit_ratio: 0,
    output_tokens_per_second: 135,
    tokens_source: 'runtime',
    timing_source: 'message_events',
    ...overrides,
  };
}

function makeJob(): AgentJob {
  return { id: 'ajob_1', state: 'done' } as AgentJob;
}

describe('statSegments', () => {
  it('states the whole conversation when every measure is available', () => {
    const text = statSegments(makeStats())
      .map((segment) => segment.text)
      .join(' | ');

    expect(text).toBe(
      '3 turns · 56 tool calls | Model 12m 58s · Tools 19.7s | ' +
        'TTFT 2s · 135 tok/s | Cache hit 0% | Input 5.8M tok · Output 89.7k tok',
    );
  });

  it('renders a dash for tokens a runtime does not report, never a zero', () => {
    const segments = statSegments(
      makeStats({
        input_tokens: null,
        output_tokens: null,
        cache_read_tokens: null,
        cache_hit_ratio: null,
        output_tokens_per_second: null,
        tokens_source: 'unavailable',
      }),
    );
    const tokens = segments.find((segment) => segment.key === 'tokens');

    expect(tokens?.text).toBe('Tokens —');
    expect(tokens?.title).toMatch(/does not report token counts/);
    // A zero cache-hit rate is a fact; an absent one is not, so the segment
    // is gone rather than showing 0%.
    expect(segments.some((segment) => segment.key === 'cache')).toBe(false);
  });

  it('singularizes a one-turn thread', () => {
    const segments = statSegments(makeStats({ turns: 1, tool_calls: 1 }));

    expect(segments[0].text).toBe('1 turn · 1 tool call');
  });

  it('says how many tool calls failed when any did', () => {
    const segments = statSegments(makeStats({ failed_tool_calls: 2 }));

    expect(segments[0].title).toBe('2 tool calls returned an error.');
  });

  it('labels the time split as derived from message arrival', () => {
    const segments = statSegments(makeStats());

    expect(segments.find((segment) => segment.key === 'time')?.title).toMatch(
      /includes the model’s thinking before the tool ran/,
    );
  });

  it('omits the speed segment when neither number exists', () => {
    const segments = statSegments(
      makeStats({ ttft_seconds: null, output_tokens_per_second: null }),
    );

    expect(segments.some((segment) => segment.key === 'speed')).toBe(false);
  });
});

describe('ThreadStatsBar live totals', () => {
  const running = { id: 'ajob_1', state: 'running' } as AgentJob;

  async function tick(ms = 0) {
    await act(() => vi.advanceTimersByTimeAsync(ms));
  }

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(getAgentThreadStats).mockReset();
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('updates calls, both timings and both token counts without a job state change', async () => {
    vi.mocked(getAgentThreadStats)
      .mockResolvedValueOnce(
        makeStats({
          turns: 1,
          tool_calls: 0,
          model_seconds: 0,
          tool_seconds: 0,
          input_tokens: null,
          output_tokens: null,
          tokens_source: 'unavailable',
        }),
      )
      .mockResolvedValueOnce(
        makeStats({
          turns: 1,
          tool_calls: 15,
          model_seconds: 30,
          tool_seconds: 5,
          input_tokens: 384_800,
          output_tokens: 1500,
        }),
      );
    render(<ThreadStatsBar job={running} />);
    await tick();
    expect(screen.getByText('1 turn · 0 tool calls')).toBeInTheDocument();
    expect(screen.getByText('Tokens —')).toBeInTheDocument();

    await tick(2_000);

    expect(screen.getByText('1 turn · 15 tool calls')).toBeInTheDocument();
    expect(screen.getByText('Model 30s · Tools 5s')).toBeInTheDocument();
    expect(screen.getByText('Input 385k tok · Output 1.5k tok')).toBeInTheDocument();
  });

  it('refreshes while the job is queued', async () => {
    vi.mocked(getAgentThreadStats).mockResolvedValue(makeStats());
    render(<ThreadStatsBar job={{ ...running, state: 'queued' }} />);
    await tick(4_000);
    expect(getAgentThreadStats).toHaveBeenCalledTimes(3);
  });

  it('refreshes a historical turn while another turn in its conversation is active', async () => {
    vi.mocked(getAgentThreadStats).mockResolvedValue(makeStats());
    const { rerender } = render(
      <ThreadStatsBar job={{ ...makeJob(), conversationState: 'running' }} />,
    );
    await tick(2_000);
    expect(getAgentThreadStats).toHaveBeenCalledTimes(2);

    rerender(<ThreadStatsBar job={{ ...makeJob(), conversationState: 'idle' }} />);
    await tick(10_000);
    expect(getAgentThreadStats).toHaveBeenCalledTimes(3);
  });

  it.each(['done', 'failed', 'cancelled'] as const)(
    'loads final totals immediately on %s, then stops polling',
    async (state) => {
      vi.mocked(getAgentThreadStats)
        .mockResolvedValueOnce(makeStats({ tool_calls: 15 }))
        .mockResolvedValueOnce(makeStats({ tool_calls: 16 }));
      const { rerender } = render(<ThreadStatsBar job={running} />);
      await tick();

      rerender(<ThreadStatsBar job={{ ...running, state }} />);
      await tick();
      expect(screen.getByText('3 turns · 16 tool calls')).toBeInTheDocument();
      await tick(10_000);
      expect(getAgentThreadStats).toHaveBeenCalledTimes(2);
    },
  );

  it('does not overlap slow requests', async () => {
    let resolve!: (value: AgentThreadStatsApi) => void;
    vi.mocked(getAgentThreadStats).mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    render(<ThreadStatsBar job={running} />);
    await tick(6_000);
    expect(getAgentThreadStats).toHaveBeenCalledTimes(1);

    await act(async () => resolve(makeStats()));
    vi.mocked(getAgentThreadStats).mockResolvedValue(makeStats({ tool_calls: 57 }));
    await tick(2_000);
    expect(screen.getByText('3 turns · 57 tool calls')).toBeInTheDocument();
  });

  it('retains totals on a transient failure and recovers at the next refresh', async () => {
    vi.mocked(getAgentThreadStats)
      .mockResolvedValueOnce(makeStats({ tool_calls: 15 }))
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(makeStats({ tool_calls: 16 }));
    render(<ThreadStatsBar job={running} />);
    await tick(2_000);
    expect(screen.getByText('3 turns · 15 tool calls')).toBeInTheDocument();

    await tick(2_000);
    expect(screen.getByText('3 turns · 16 tool calls')).toBeInTheDocument();
  });

  it('pauses hidden-tab polling and refreshes immediately on return', async () => {
    vi.mocked(getAgentThreadStats).mockResolvedValue(makeStats());
    render(<ThreadStatsBar job={running} />);
    await tick();
    vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    await tick(6_000);
    expect(getAgentThreadStats).toHaveBeenCalledTimes(1);

    vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(getAgentThreadStats).toHaveBeenCalledTimes(2);
  });

  it('hides the previous conversation totals when the next conversation cannot load', async () => {
    vi.mocked(getAgentThreadStats)
      .mockResolvedValueOnce(makeStats())
      .mockRejectedValueOnce(new Error('offline'));
    const { rerender } = render(<ThreadStatsBar job={running} />);
    await tick();
    expect(screen.getByLabelText('Conversation totals')).toBeInTheDocument();

    rerender(<ThreadStatsBar job={{ ...running, id: 'ajob_2' }} />);
    expect(screen.queryByLabelText('Conversation totals')).not.toBeInTheDocument();
    await tick();
    expect(screen.queryByLabelText('Conversation totals')).not.toBeInTheDocument();
  });

  it('ignores a late running response after the final totals arrive', async () => {
    let resolve!: (value: AgentThreadStatsApi) => void;
    vi.mocked(getAgentThreadStats)
      .mockReturnValueOnce(
        new Promise((done) => {
          resolve = done;
        }),
      )
      .mockResolvedValueOnce(makeStats({ tool_calls: 16 }));
    const { rerender } = render(<ThreadStatsBar job={running} />);
    const signal = vi.mocked(getAgentThreadStats).mock.calls[0][1];

    rerender(<ThreadStatsBar job={makeJob()} />);
    await tick();
    expect(signal?.aborted).toBe(true);
    await act(async () => resolve(makeStats({ tool_calls: 1 })));
    expect(screen.getByText('3 turns · 16 tool calls')).toBeInTheDocument();
  });

  it('aborts pending requests and removes polling and visibility listeners on unmount', async () => {
    vi.mocked(getAgentThreadStats).mockReturnValue(new Promise(() => undefined));
    const { unmount } = render(<ThreadStatsBar job={running} />);
    const signal = vi.mocked(getAgentThreadStats).mock.calls[0][1];
    unmount();

    expect(signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await tick(4_000);
    expect(getAgentThreadStats).toHaveBeenCalledTimes(1);
  });
});

describe('formatCount', () => {
  it.each([
    [0, '0'],
    [999, '999'],
    [1000, '1k'],
    [89_700, '89.7k'],
    [5_800_000, '5.8M'],
    [150_000, '150k'],
    [2_400_000_000, '2.4B'],
  ])('formats %i as %s', (value, expected) => {
    expect(formatCount(value)).toBe(expected);
  });

  it('returns null for a missing count', () => {
    expect(formatCount(null)).toBeNull();
    expect(formatCount(undefined)).toBeNull();
  });
});

describe('ThreadStatsBar', () => {
  beforeEach(() => {
    vi.mocked(getAgentThreadStats).mockReset();
  });

  afterEach(() => cleanup());

  it('renders the conversation totals once they load', async () => {
    vi.mocked(getAgentThreadStats).mockResolvedValue(makeStats());

    render(<ThreadStatsBar job={makeJob()} />);

    expect(await screen.findByText('3 turns · 56 tool calls')).toBeInTheDocument();
    expect(screen.getByLabelText('Conversation totals')).toBeInTheDocument();
  });

  it('stays absent for a thread with no stats rather than showing an empty bar', async () => {
    vi.mocked(getAgentThreadStats).mockResolvedValue(null);

    const { container } = render(<ThreadStatsBar job={makeJob()} />);

    await waitFor(() => expect(getAgentThreadStats).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('does not put an error over the composer when the summary fails', async () => {
    vi.mocked(getAgentThreadStats).mockRejectedValue(new Error('boom'));

    const { container } = render(<ThreadStatsBar job={makeJob()} />);

    await waitFor(() => expect(getAgentThreadStats).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it('reloads when the turn ends', async () => {
    vi.mocked(getAgentThreadStats).mockResolvedValue(makeStats());
    const running = { id: 'ajob_1', state: 'running' } as AgentJob;

    const { rerender } = render(<ThreadStatsBar job={running} />);
    await waitFor(() => expect(getAgentThreadStats).toHaveBeenCalledTimes(1));

    rerender(<ThreadStatsBar job={{ ...running, state: 'done' } as AgentJob} />);

    await waitFor(() => expect(getAgentThreadStats).toHaveBeenCalledTimes(2));
  });
});

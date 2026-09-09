// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen, waitFor } from '@testing-library/react';
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

  it('distinguishes a measured zero cache-hit rate from an absent one', () => {
    const measured = statSegments(makeStats({ cache_hit_ratio: 0 }));

    expect(measured.find((segment) => segment.key === 'cache')?.text).toBe('Cache hit 0%');
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

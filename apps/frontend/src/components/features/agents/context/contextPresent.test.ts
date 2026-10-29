import { describe, expect, it } from 'vitest';

import type { ContextPointApi, ContextTurnApi } from '@/lib/api/agents';
import {
  chartScale,
  compositionSegments,
  contextBar,
  contextHref,
  opensContext,
  signedTokens,
  turnPoints,
} from './contextPresent';

function point(
  index: number,
  prompt: number,
  overrides: Partial<ContextPointApi> = {},
): ContextPointApi {
  return {
    index,
    job_id: 'j1',
    turn_no: 1,
    attempt_id: 1,
    seq: index + 1,
    event_id: 100 + index,
    at: null,
    prompt_tokens: prompt,
    cache_read_tokens: null,
    cache_write_tokens: null,
    uncached_input_tokens: null,
    output_tokens: null,
    context_window: null,
    model: null,
    delta_tokens: null,
    after_compaction: false,
    ...overrides,
  };
}

describe('contextPresent', () => {
  it('links a conversation to its Context view and recognizes the link', () => {
    expect(contextHref('ajob 1')).toBe('/ajob%201?view=context');
    expect(opensContext('?view=context')).toBe(true);
    expect(opensContext('?view=chat')).toBe(false);
    expect(opensContext('')).toBe(false);
  });

  it('draws a conversation against its window only when one was reported', () => {
    expect(contextBar(50_000, 25_000, 200_000, 90_000)).toEqual({
      peak: 0.25,
      latest: 0.125,
      againstWindow: true,
    });
    // Without a window: against the widest shown, and never as a full window.
    expect(contextBar(45_000, null, null, 90_000)).toEqual({
      peak: 0.5,
      latest: null,
      againstWindow: false,
    });
    expect(contextBar(null, null, null, 0)).toEqual({
      peak: 0,
      latest: null,
      againstWindow: false,
    });
  });

  it('signs changes and keeps an unknown one a dash', () => {
    const compact = (value: number) => `${value / 1000}k`;
    expect(signedTokens(12_000, compact)).toBe('+12k');
    expect(signedTokens(-4_000, compact)).toBe('−4k');
    expect(signedTokens(0, compact)).toBe('0');
    expect(signedTokens(null, compact)).toBe('—');
  });

  it('scales to a shared reported window only when it is near the requests', () => {
    expect(chartScale([point(0, 50_000, { context_window: 200_000 })])).toEqual({
      top: 200_000,
      window: 1,
    });
    // A 1M window over 20k requests would flatten them: scale to the requests.
    expect(chartScale([point(0, 20_000, { context_window: 1_000_000 })])).toEqual({
      top: 20_000,
      window: null,
    });
    // Different windows (or none) are not drawn as one line.
    expect(
      chartScale([point(0, 10, { context_window: 100 }), point(1, 20, { context_window: 50 })]),
    ).toEqual({ top: 20, window: null });
  });

  it('takes each turn’s last request for the by-turn view', () => {
    const turns = [
      { job_id: 'j1', turn_no: 1 },
      { job_id: 'j2', turn_no: 2 },
      { job_id: 'j3', turn_no: 3 },
    ] as ContextTurnApi[];
    const points = [point(0, 1), point(1, 2), point(2, 3, { job_id: 'j3', turn_no: 3 })];
    expect(
      turnPoints(turns, points).map((entry) => [entry.turn.turn_no, entry.point.index]),
    ).toEqual([
      [1, 1],
      [3, 2],
    ]);
  });

  it('adds the unattributed part only when the server could attribute it', () => {
    const segments = compositionSegments({
      basis: 'whole_conversation',
      chars_per_token: 4,
      categories: { user: 100, assistant: 300, tool_results: 0 },
      estimated_tokens: 400,
      truncated: false,
      unattributed_tokens: 600,
    });
    expect(segments.map((segment) => [segment.key, segment.share])).toEqual([
      ['user', 0.1],
      ['assistant', 0.3],
      ['unattributed', 0.6],
    ]);
    const unknown = compositionSegments({
      basis: 'since_last_compaction',
      chars_per_token: 4,
      categories: { user: 100, assistant: 100 },
      estimated_tokens: 200,
      truncated: true,
      unattributed_tokens: null,
    });
    expect(unknown.map((segment) => segment.key)).toEqual(['user', 'assistant']);
  });
});

// A settlement must move the File Activity revision even though the turn row
// already existed while the turn ran, so a pending operation can be re-read.
import { describe, expect, it } from 'vitest';

import type { ContextPointApi, ContextTurnApi, ThreadContextApi } from '@/lib/api/agents/context';
import { settledRevision } from '@/components/features/agents/context/ContextView';

function turn(overrides: Partial<ContextTurnApi> = {}): ContextTurnApi {
  return {
    job_id: 'j1',
    turn_no: 1,
    created_at: '2026-10-31T08:00:00Z',
    runtime: 'claude-code',
    model: 'm',
    state: 'running',
    copied: false,
    attempts: 1,
    requests: 2,
    subagent_requests: 0,
    first_prompt_tokens: null,
    last_prompt_tokens: null,
    peak_prompt_tokens: null,
    change_tokens: null,
    compactions: 0,
    context_window: null,
    first_index: 0,
    ...overrides,
  };
}

function point(seq: number): ContextPointApi {
  return {
    index: seq,
    job_id: 'j1',
    turn_no: 1,
    attempt_id: 1,
    seq,
    event_id: seq,
    at: '2026-10-31T08:00:00Z',
    prompt_tokens: 10,
    cache_read_tokens: null,
    cache_write_tokens: null,
    uncached_input_tokens: null,
    output_tokens: null,
    context_window: null,
    model: 'm',
    delta_tokens: null,
    after_compaction: false,
  };
}

function view(overrides: Partial<ThreadContextApi> = {}): ThreadContextApi {
  return {
    thread_id: 't',
    generated_at: '2026-10-31T09:00:00Z',
    capabilities: [],
    latest: null,
    peak: null,
    turns: [turn()],
    requests: [point(1), point(2)],
    compactions: [],
    largest_changes: [],
    coverage: {
      turns: 1,
      attempts: 1,
      attempts_observed: 1,
      main_requests: 2,
      subagent_requests: 0,
      compactions: 0,
      compactions_unknown_outcome: 0,
      points_omitted: 0,
      unparsed_raw: 0,
    },
    events: [],
    event_counts: {},
    events_omitted: 0,
    event_capabilities: [],
    ...overrides,
  };
}

describe('settledRevision', () => {
  it('changes when the same turn settles, with its count unchanged', () => {
    const running = view();
    const settled = view({ turns: [turn({ state: 'succeeded' })] });
    expect(settled.turns.length).toBe(running.turns.length);
    expect(settledRevision(settled)).not.toBe(settledRevision(running));
  });

  it('changes when a settled turn gains a request', () => {
    const before = view({ turns: [turn({ state: 'succeeded' })] });
    const after = view({
      turns: [turn({ state: 'succeeded', requests: 3 })],
      requests: [point(1), point(2), point(3)],
    });
    expect(settledRevision(after)).not.toBe(settledRevision(before));
  });

  it('changes when a new turn is recorded', () => {
    const one = view({ turns: [turn({ state: 'succeeded' })] });
    const two = view({
      turns: [turn({ state: 'succeeded' }), turn({ job_id: 'j2', turn_no: 2, state: 'succeeded' })],
    });
    expect(settledRevision(two)).not.toBe(settledRevision(one));
  });
});

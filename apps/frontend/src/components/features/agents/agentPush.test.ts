// @vitest-environment jsdom
// What a tab holds from the owner's change stream. The cases that matter are
// the ones where a wrong answer shows the owner something older than they were
// already shown: a late or repeated push replacing a newer one, or an API read
// that left before a change putting the old title back after its push.
import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentJobApi, AgentJobUsageApi } from '@/lib/api/agents';
import { streamAgentChanges, type StreamAgentChangesOptions } from '@/lib/api/agentChanges';

import {
  type AgentRecordsChange,
  isAgentPushLive,
  onAgentRecordsChanged,
  isOlderUsage,
  overlayPushed,
  PUSH_BATCH_MS,
  receiveHint,
  receiveJobChange,
  receiveSync,
  receiveThreadChange,
  resetAgentPush,
  useAgentChangeStream,
  useAgentPushLive,
} from './agentPush';

vi.mock('@/lib/api/agentChanges', () => ({ streamAgentChanges: vi.fn() }));

const JOB = {
  id: 'ajob_1',
  thread_id: 'athr_1',
  state: 'running',
  conversation_state: 'running',
  published_pr_url: null,
  title: 'Old title',
  title_source: 'fallback',
  revision: 3,
  thread_revision: 5,
} as AgentJobApi;

function collect(): AgentRecordsChange[] {
  const seen: AgentRecordsChange[] = [];
  onAgentRecordsChanged((change) => seen.push(change));
  return seen;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.useFakeTimers();
  resetAgentPush();
});

afterEach(() => {
  resetAgentPush();
  vi.useRealTimers();
});

describe('pushed records', () => {
  it('delivers changes that land together as one batch', () => {
    const seen = collect();
    receiveJobChange({ id: 'ajob_1', thread_id: 'athr_1', revision: 4, state: 'succeeded' });
    receiveThreadChange({ id: 'athr_1', revision: 6, title: 'New title' });
    receiveSync();
    expect(seen).toEqual([]);

    vi.advanceTimersByTime(PUSH_BATCH_MS);

    expect(seen).toHaveLength(1);
    expect(seen[0].jobs.map((job) => job.id)).toEqual(['ajob_1']);
    expect(seen[0].threads.map((thread) => thread.id)).toEqual(['athr_1']);
    expect(seen[0].resync).toBe(true);
  });

  it('names usage, queue, approval and publication changes once per batch', () => {
    const seen = collect();
    receiveHint('usage', 'ajob_1');
    receiveHint('usage', 'ajob_1');
    receiveHint('mcp', 'ajob_1');
    receiveHint('queue', 'athr_1');
    receiveHint('publication', 'athr_1');
    vi.advanceTimersByTime(PUSH_BATCH_MS);

    expect(seen).toHaveLength(1);
    expect(seen[0]).toMatchObject({
      usage: ['ajob_1'],
      mcp: ['ajob_1'],
      queues: ['athr_1'],
      publications: ['athr_1'],
      resync: false,
    });
  });

  it('keeps the newest usage sent for a job; a later bare hint asks for a read', () => {
    const seen = collect();
    const first = { ledger: { status: 'pending' } } as AgentJobUsageApi;
    const second = { ledger: { status: 'available' } } as AgentJobUsageApi;
    receiveHint('usage', 'ajob_1', first);
    receiveHint('usage', 'ajob_1', second);
    receiveHint('usage', 'ajob_2', first);
    receiveHint('usage', 'ajob_2');
    vi.advanceTimersByTime(PUSH_BATCH_MS);

    expect(seen[0].usage).toEqual(['ajob_1', 'ajob_2']);
    expect(seen[0].usageValues).toEqual({ ajob_1: second });
  });

  it('drops a push that is not newer than one already held', () => {
    const seen = collect();
    receiveJobChange({ id: 'ajob_1', revision: 4, state: 'succeeded' });
    receiveJobChange({ id: 'ajob_1', revision: 4, state: 'succeeded' });
    receiveJobChange({ id: 'ajob_1', revision: 2, state: 'running' });
    vi.advanceTimersByTime(PUSH_BATCH_MS);

    expect(seen).toHaveLength(1);
    expect(seen[0].jobs).toEqual([{ id: 'ajob_1', revision: 4, state: 'succeeded' }]);
  });

  it('keeps a removal over anything that arrives after it', () => {
    const seen = collect();
    receiveThreadChange({ id: 'athr_1', deleted: true });
    receiveThreadChange({ id: 'athr_1', revision: 9, title: 'Too late' });
    vi.advanceTimersByTime(PUSH_BATCH_MS);

    expect(seen[0].threads).toEqual([{ id: 'athr_1', deleted: true }]);
  });
});

describe('overlayPushed', () => {
  it('keeps newer pushed values over an API read that left before them', () => {
    receiveJobChange({
      id: 'ajob_1',
      thread_id: 'athr_1',
      revision: 4,
      state: 'running',
      conversation_state: 'running',
      published_pr_url: null,
    });
    receiveThreadChange({
      id: 'athr_1',
      revision: 6,
      title: 'New title',
      title_source: 'generated',
    });

    const read = { ...JOB, state: 'queued', conversation_state: 'queued' } as AgentJobApi;
    expect(overlayPushed(read)).toMatchObject({
      state: 'running',
      conversation_state: 'running',
      revision: 4,
      title: 'New title',
      title_source: 'generated',
      thread_revision: 6,
    });
  });

  it('lays a pushed pull request link over a turn read as ended', () => {
    receiveJobChange({
      id: 'ajob_1',
      thread_id: 'athr_1',
      revision: 4,
      state: 'succeeded',
      conversation_state: 'idle',
      published_pr_url: 'https://example.test/pr/1',
    });

    const read = { ...JOB, state: 'succeeded', conversation_state: 'idle' } as AgentJobApi;
    expect(overlayPushed(read)).toMatchObject({
      published_pr_url: 'https://example.test/pr/1',
      revision: 4,
    });
  });

  it('never ends a turn the read still shows active; its own stream does', () => {
    receiveJobChange({
      id: 'ajob_1',
      thread_id: 'athr_1',
      revision: 4,
      state: 'succeeded',
      conversation_state: 'idle',
      published_pr_url: 'https://example.test/pr/1',
    });
    receiveThreadChange({ id: 'athr_1', revision: 6, title: 'New title' });

    expect(overlayPushed(JOB)).toMatchObject({
      state: 'running',
      conversation_state: 'running',
      published_pr_url: null,
      revision: 3,
      title: 'New title',
    });
  });

  it('leaves a read alone when it is as new as what was pushed', () => {
    receiveJobChange({ id: 'ajob_1', revision: 3, state: 'failed' });
    receiveThreadChange({ id: 'athr_1', revision: 5, title: 'Pushed' });

    expect(overlayPushed(JOB)).toBe(JOB);
  });
});

describe('isOlderUsage', () => {
  const at = (version: string | null) => ({ version }) as AgentJobUsageApi;

  it('orders two reads of a usage by their versions, and nothing without both', () => {
    expect(isOlderUsage(at('00000000000000000009.999998'), at('00000000000000000009.999999'))).toBe(
      true,
    );
    expect(isOlderUsage(at('00000000000000000010.999990'), at('00000000000000000009.999999'))).toBe(
      false,
    );
    expect(isOlderUsage(at('0001'), at('0001'))).toBe(false);
    expect(isOlderUsage(at(null), at('0002'))).toBe(false);
    expect(isOlderUsage(at('0001'), null)).toBe(false);
  });
});

describe('the tab stream', () => {
  it('goes live with hello, stops when the connection drops, and ends with the page', () => {
    let options!: StreamAgentChangesOptions;
    vi.mocked(streamAgentChanges).mockImplementation((opts) => {
      options = opts;
      return new Promise(() => undefined);
    });
    const seen = collect();
    const live = renderHook(() => useAgentPushLive());
    const stream = renderHook(() => useAgentChangeStream('user-1'));
    expect(live.result.current).toBe(false);

    act(() => options.onSync(true));
    expect(live.result.current).toBe(true);
    act(() => vi.advanceTimersByTime(PUSH_BATCH_MS));
    expect(seen.at(-1)?.resync).toBe(true);

    act(() => options.onDisconnect());
    expect(isAgentPushLive()).toBe(false);

    act(() => options.onSync(true));
    stream.unmount();
    expect(options.signal.aborted).toBe(true);
    expect(isAgentPushLive()).toBe(false);
  });

  it('holds nothing without a signed-in owner', () => {
    renderHook(() => useAgentChangeStream(undefined));
    expect(streamAgentChanges).not.toHaveBeenCalled();
  });
});

// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { announceAgentSessionsChanged, onAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import {
  getAgentJob,
  getAgentJobGit,
  getAgentJobThread,
  listAllAgentThreadEvents,
  streamAgentJob,
  type AgentJobApi,
} from '@/lib/api/agents';
import { useAgentJob } from './useAgentJobs';

vi.mock('@/lib/api/agents', () => ({
  getAgentJob: vi.fn(),
  getAgentJobGit: vi.fn(),
  getAgentJobThread: vi.fn(),
  listAllAgentThreadEvents: vi.fn(),
  streamAgentJob: vi.fn(),
}));
const initial = {
  id: 'job-one',
  thread_id: 'thread-one',
  turn_no: 1,
  state: 'running',
  repo: 'owner/repo',
  task_prompt: 'Please help me design the export function for SPRUCE_8462',
  title: 'Please help me design the export…',
  title_source: 'fallback',
} as AgentJobApi;
beforeEach(() => {
  vi.useFakeTimers();
  vi.mocked(getAgentJob).mockResolvedValue(initial);
  vi.mocked(getAgentJobGit).mockRejectedValue(new Error('No workspace'));
  vi.mocked(getAgentJobThread).mockResolvedValue({
    thread_id: initial.thread_id!,
    title: initial.title!,
    jobs: [],
    messages: [],
  });
  vi.mocked(listAllAgentThreadEvents).mockResolvedValue([]);
  vi.mocked(streamAgentJob).mockImplementation(() => new Promise(() => {}));
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.resetAllMocks();
});

describe('queued turn lifecycle', () => {
  it('streams a queued follow-up and shows it running when its attempt starts', async () => {
    vi.mocked(getAgentJob).mockResolvedValue({
      ...initial,
      state: 'queued',
      parent_job_id: 'previous-job',
      turn_no: 2,
    });
    const { result } = renderHook(() => useAgentJob(initial.id));
    await act(async () => {});
    expect(result.current.job?.state).toBe('queued');
    expect(streamAgentJob).toHaveBeenCalledWith(initial.id, expect.any(Object));

    await act(async () => {
      vi.mocked(streamAgentJob).mock.lastCall![1].onEvent({
        id: 1,
        attempt_id: 10,
        seq: 1,
        event_type: 'lifecycle',
        payload: { phase: 'started' },
        created_at: null,
      });
    });
    expect(result.current.job?.state).toBe('running');
    expect(result.current.job?.currentAttemptId).toBe(10);
  });
});

describe('automatic task title refresh', () => {
  it.each(['running', 'succeeded'] as const)(
    'shows the generated title for a %s task despite an older thread snapshot',
    async (state) => {
      const changed = vi.fn();
      const unsubscribe = onAgentSessionsChanged(changed);
      vi.mocked(getAgentJob).mockResolvedValue({ ...initial, state });
      const { result } = renderHook(() => useAgentJob(initial.id));
      await act(async () => {});
      expect(result.current.job?.title).toBe(initial.title);
      vi.mocked(getAgentJob).mockResolvedValue({
        ...initial,
        title: 'Name SPRUCE_8462 export function',
        title_source: 'generated',
      });
      await act(async () => vi.advanceTimersByTimeAsync(2_000));
      expect(result.current.job?.title).toBe('Name SPRUCE_8462 export function');
      expect(result.current.job?.prompt).toBe(initial.task_prompt);
      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      expect(getAgentJob).toHaveBeenCalledTimes(2);
      expect(getAgentJobThread).toHaveBeenCalledTimes(1);
      expect(changed).toHaveBeenCalledExactlyOnceWith({
        threadIds: [initial.thread_id],
        title: 'Name SPRUCE_8462 export function',
        titleSource: 'generated',
      });
      unsubscribe();
    },
  );
  it('bounds polling when generation does not produce a title', async () => {
    const { result } = renderHook(() => useAgentJob(initial.id));
    await act(async () => {});
    await act(async () => vi.advanceTimersByTimeAsync(180_000));
    expect(result.current.job?.title).toBe(initial.title);
    expect(getAgentJob).toHaveBeenCalledTimes(66);
  });
  it('keeps a recovered title when an older activity refresh arrives later', async () => {
    const idle = { ...initial, state: 'succeeded', conversation_state: 'running' } as AgentJobApi;
    vi.mocked(getAgentJob).mockResolvedValue(idle);
    const { result } = renderHook(() => useAgentJob(initial.id));
    await act(async () => {});
    await act(async () => vi.advanceTimersByTimeAsync(4_000));
    let resolve!: (row: AgentJobApi) => void;
    vi.mocked(getAgentJob).mockReturnValueOnce(new Promise((done) => (resolve = done)));
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    vi.mocked(getAgentJob).mockResolvedValue({
      ...idle,
      title: 'Export CSV',
      title_source: 'generated',
    });
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(result.current.job?.title).toBe('Export CSV');
    await act(async () => resolve(idle));
    expect(result.current.job?.title).toBe('Export CSV');
  });
  it('keeps a recovered title when an older full reload arrives later', async () => {
    const { result } = renderHook(() => useAgentJob(initial.id));
    await act(async () => {});
    let resolve!: (row: AgentJobApi) => void;
    vi.mocked(getAgentJob).mockReturnValueOnce(new Promise((done) => (resolve = done)));
    await act(async () => result.current.reload());
    vi.mocked(getAgentJob).mockResolvedValue({
      ...initial,
      title: 'Export CSV',
      title_source: 'generated',
    });
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(result.current.job?.title).toBe('Export CSV');
    await act(async () => resolve(initial));
    expect(result.current.job?.title).toBe('Export CSV');
    expect(result.current.loading).toBe(false);
  });
  it('does not broadcast a stale generated title after a manual title is observed', async () => {
    const idle = { ...initial, state: 'succeeded', conversation_state: 'running' } as AgentJobApi;
    vi.mocked(getAgentJob).mockResolvedValue(idle);
    const { result } = renderHook(() => useAgentJob(initial.id));
    await act(async () => {});
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    let resolve!: (row: AgentJobApi) => void;
    vi.mocked(getAgentJob).mockReturnValueOnce(new Promise((done) => (resolve = done)));
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    vi.mocked(getAgentJob).mockResolvedValue({ ...idle, title: 'My name', title_source: 'manual' });
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    expect(result.current.job?.title).toBe('My name');
    const changed = vi.fn();
    const unsubscribe = onAgentSessionsChanged(changed);
    await act(async () =>
      resolve({ ...idle, title: 'Old generated name', title_source: 'generated' }),
    );
    expect(result.current.job?.title).toBe('My name');
    expect(changed).not.toHaveBeenCalled();
    unsubscribe();
  });
  it('does not carry a resolved title into a different conversation', async () => {
    vi.mocked(getAgentJob).mockResolvedValue({
      ...initial,
      title: 'Export CSV',
      title_source: 'generated',
    });
    const { result, rerender } = renderHook(({ id }) => useAgentJob(id), {
      initialProps: { id: initial.id },
    });
    await act(async () => {});
    const next = { ...initial, id: 'job-two', thread_id: 'thread-two', title: 'Fix login' };
    vi.mocked(getAgentJob).mockResolvedValue(next);
    rerender({ id: next.id });
    await act(async () => {});
    expect(result.current.job?.title).toBe('Fix login');
  });
  it.each([
    { title: initial.title, title_source: 'fallback' },
    { title: '⏳ The model is starting up — this t…', title_source: 'generated' },
    { title: '⏳ The model is starting up — this t… (fork)', title_source: 'inherited' },
  ] as const)('refreshes a delayed recovery on a later turn: $title_source', async (title) => {
    const followup = { ...initial, ...title, turn_no: 2 };
    vi.mocked(getAgentJob).mockResolvedValue(followup);
    const { result } = renderHook(() => useAgentJob(initial.id));
    await act(async () => {});
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(result.current.job?.title).toBe(title.title);
    vi.mocked(getAgentJob).mockResolvedValue({
      ...followup,
      title: 'Name SPRUCE_8462 export function',
      title_source: 'generated',
    });
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(result.current.job?.title).toBe('Name SPRUCE_8462 export function');
    const count = vi.mocked(getAgentJob).mock.calls.length;
    await act(async () => vi.advanceTimersByTimeAsync(180_000));
    expect(getAgentJob).toHaveBeenCalledTimes(count);
  });
  it.each(['generated', 'manual'] as const)('does not poll a stable %s title', async (source) => {
    vi.mocked(getAgentJob).mockResolvedValue({
      ...initial,
      title: source === 'manual' ? '⏳ The model is starting up — this t…' : 'Export CSV',
      title_source: source,
    });
    renderHook(() => useAgentJob(initial.id));
    await act(async () => {});
    await act(async () => vi.advanceTimersByTimeAsync(180_000));
    expect(getAgentJob).toHaveBeenCalledTimes(1);
  });
  it('ignores a pending automatic-title lookup when the user renames the task', async () => {
    const { result } = renderHook(() => useAgentJob(initial.id));
    await act(async () => {});
    let resolve!: (row: AgentJobApi) => void;
    vi.mocked(getAgentJob).mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    vi.mocked(getAgentJob).mockResolvedValue({
      ...initial,
      title: 'My chosen name',
      title_source: 'manual',
    });
    await act(async () =>
      announceAgentSessionsChanged({ threadIds: [initial.thread_id!], title: 'My chosen name' }),
    );
    expect(result.current.job?.title).toBe('My chosen name');
    await act(async () =>
      resolve({ ...initial, title: 'Generated title', title_source: 'generated' }),
    );
    expect(result.current.job?.title).toBe('My chosen name');
  });
});

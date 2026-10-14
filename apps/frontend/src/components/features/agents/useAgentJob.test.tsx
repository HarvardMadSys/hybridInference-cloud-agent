// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';

import {
  getAgentJob,
  getAgentJobGit,
  getAgentJobThread,
  listAllAgentThreadEvents,
  streamAgentJob,
  type AgentJobApi,
  type AgentThreadApi,
  type AgentThreadEventApi,
} from '@/lib/api/agents';

import {
  PUSH_BATCH_MS,
  PUSH_FALLBACK_POLL_MS,
  receiveHint,
  receiveJobChange,
  receiveThreadChange,
  resetAgentPush,
  setAgentPushLive,
} from './agentPush';
import { buildTrajectorySteps, turnContextFromJobs } from './trajectorySteps';
import { STREAM_RETRY_MAX_MS, STREAM_RETRY_MIN_MS, useAgentJob } from './useAgentJobs';
import { AgentReadProvider } from './AgentReadProvider';

vi.mock('@/lib/api/agents', () => ({
  getAgentJob: vi.fn(),
  getAgentJobGit: vi.fn(),
  getAgentJobThread: vi.fn(),
  listAllAgentThreadEvents: vi.fn(),
  streamAgentJob: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getAgentJobGit).mockResolvedValue({
    available: false,
  } as Awaited<ReturnType<typeof getAgentJobGit>>);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/** A turn's stream that stays open; one that returns without the turn ending could not connect. */
function stayOpen(): Promise<void> {
  return new Promise(() => undefined);
}

function event(
  id: number,
  event_type: string,
  payload: Record<string, unknown>,
  attempt_id = 10,
): AgentThreadEventApi {
  return {
    id,
    seq: id,
    event_type,
    payload,
    attempt_id,
    job_id: 'live-job',
    turn_no: 1,
    created_at: `2026-09-11T10:00:0${id}.000Z`,
  } as AgentThreadEventApi;
}

describe('useAgentJob history-to-stream handoff', () => {
  beforeEach(() => resetAgentPush());
  afterEach(() => resetAgentPush());

  function running(id = 'live-job'): AgentJobApi {
    return {
      id,
      thread_id: 'live-thread',
      state: 'running',
      conversation_state: 'running',
      current_attempt_id: 10,
      task_prompt: 'Fix it',
      title: 'Fix it',
      title_source: 'manual',
      runtime: 'codex',
      model: 'test',
      revision: 1,
    } as AgentJobApi;
  }

  function serve(job: AgentJobApi, history: AgentThreadEventApi[]) {
    vi.mocked(getAgentJob).mockResolvedValue(job);
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: job.thread_id,
      jobs: [job],
      messages: [],
    } as AgentThreadApi);
    vi.mocked(listAllAgentThreadEvents).mockResolvedValue(history);
    vi.mocked(streamAgentJob).mockImplementation(stayOpen);
  }

  it('shows durable replies before activity finishes and keeps them through partial pages', async () => {
    const previous = { ...running('previous-job'), state: 'succeeded', turn_no: 1 } as AgentJobApi;
    const current = { ...running(), state: 'succeeded', turn_no: 2 } as AgentJobApi;
    const first = event(10, 'message', { text: 'Current first reply' });
    const second = event(11, 'message', { text: 'Current second reply' });
    const olderFirst = {
      ...event(1, 'message', { text: 'Previous first reply' }),
      job_id: previous.id,
    };
    const olderSecond = {
      ...event(2, 'message', { text: 'Previous second reply' }),
      job_id: previous.id,
    };
    serve(current, []);
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: current.thread_id,
      jobs: [previous, current],
      messages: [
        { id: 1, role: 'user', job_id: previous.id, content: 'Previous prompt', created_at: null },
        {
          id: 2,
          role: 'assistant',
          job_id: previous.id,
          content: 'Previous first reply',
          created_at: null,
        },
        {
          id: 3,
          role: 'assistant',
          job_id: previous.id,
          content: 'Previous second reply',
          created_at: null,
        },
        { id: 4, role: 'user', job_id: current.id, content: 'Current prompt', created_at: null },
        {
          id: 5,
          role: 'assistant',
          job_id: current.id,
          content: 'Current first reply',
          created_at: null,
        },
        {
          id: 6,
          role: 'assistant',
          job_id: current.id,
          content: 'Current second reply',
          created_at: null,
        },
      ],
    } as AgentThreadApi);
    let pageOptions!: NonNullable<Parameters<typeof listAllAgentThreadEvents>[1]>;
    let finish!: (rows: AgentThreadEventApi[]) => void;
    vi.mocked(listAllAgentThreadEvents).mockImplementationOnce((_id, options) => {
      pageOptions = options!;
      return new Promise((resolve) => (finish = resolve));
    });
    const { result } = renderHook(() => useAgentJob(current.id));
    await waitFor(() => expect(result.current.job?.id).toBe(current.id));
    expect(result.current.loading).toBe(true);
    expect(result.current.job?.threadMessages?.map((message) => message.content)).toContain(
      'Current second reply',
    );
    expect(
      result.current.job?.historyTurns?.[0].messages.map((message) => message.content),
    ).toContain('Previous second reply');
    expect(streamAgentJob).not.toHaveBeenCalled();

    act(() => pageOptions.onPage?.([olderFirst, first]));
    expect(result.current.events).toEqual([olderFirst, first]);
    expect(result.current.job?.events.filter((row) => row.kind === 'message')).toHaveLength(0);
    expect(result.current.job?.threadMessages?.map((message) => message.content)).toContain(
      'Current second reply',
    );
    expect(
      result.current.job?.historyTurns?.[0].events.filter((row) => row.kind === 'message'),
    ).toHaveLength(0);
    expect(
      result.current.job?.historyTurns?.[0].messages.map((message) => message.content),
    ).toContain('Previous second reply');

    await act(async () => finish([olderFirst, olderSecond, first, second]));
    expect(result.current.loading).toBe(false);
    expect(result.current.job?.events.filter((row) => row.kind === 'message')).toHaveLength(2);
    expect(
      result.current.job?.historyTurns?.[0].events.filter((row) => row.kind === 'message'),
    ).toHaveLength(2);
    expect(streamAgentJob).not.toHaveBeenCalled();
  });

  it('keeps all durable replies when a partial activity download fails', async () => {
    const current = { ...running(), state: 'succeeded' } as AgentJobApi;
    serve(current, []);
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: current.thread_id,
      jobs: [current],
      messages: [
        { id: 1, role: 'assistant', job_id: current.id, content: 'First reply', created_at: null },
        { id: 2, role: 'assistant', job_id: current.id, content: 'Second reply', created_at: null },
      ],
    } as AgentThreadApi);
    let pageOptions!: NonNullable<Parameters<typeof listAllAgentThreadEvents>[1]>;
    let fail!: (cause: Error) => void;
    vi.mocked(listAllAgentThreadEvents).mockImplementationOnce((_id, options) => {
      pageOptions = options!;
      return new Promise((_, reject) => (fail = reject));
    });
    const { result } = renderHook(() => useAgentJob(current.id));
    await waitFor(() => expect(pageOptions).toBeDefined());
    act(() => pageOptions.onPage?.([event(1, 'message', { text: 'First reply' })]));
    await act(async () => fail(new Error('History unavailable')));
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBe('History unavailable');
    expect(result.current.job?.threadMessages?.map((message) => message.content)).toEqual([
      'First reply',
      'Second reply',
    ]);
    expect(result.current.job?.events.filter((row) => row.kind === 'message')).toHaveLength(0);
  });

  it('aborts an abandoned history download and ignores its late pages', async () => {
    const first = running();
    const next = running('next-job');
    serve(first, []);
    let pageOptions!: NonNullable<Parameters<typeof listAllAgentThreadEvents>[1]>;
    let finish!: (rows: AgentThreadEventApi[]) => void;
    vi.mocked(listAllAgentThreadEvents).mockImplementationOnce((_id, options) => {
      pageOptions = options!;
      return new Promise((resolve) => (finish = resolve));
    });
    const { result, rerender } = renderHook(({ id }) => useAgentJob(id), {
      initialProps: { id: first.id },
    });
    await waitFor(() => expect(pageOptions).toBeDefined());
    const nextEvent = { ...event(5, 'message', { text: 'Next chat' }), job_id: next.id };
    serve(next, [nextEvent]);
    rerender({ id: next.id });
    expect(pageOptions.signal?.aborted).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    const late = event(100, 'message', { text: 'Abandoned chat' });
    await act(async () => {
      pageOptions.onPage?.([late]);
      finish([late]);
    });
    expect(result.current.job?.id).toBe(next.id);
    expect(result.current.events).toEqual([nextEvent]);
    expect(result.current.error).toBeNull();
  });

  it('retains complete older activity and new stream frames during paged refreshes', async () => {
    const job = running();
    const first = { ...event(1, 'message', { text: 'Older first' }), job_id: 'older-job' };
    const second = { ...event(2, 'message', { text: 'Older second' }), job_id: 'older-job' };
    const own = event(3, 'message', { text: 'Current' });
    serve(job, [first, second, own]);
    const { result } = renderHook(() => useAgentJob(job.id));
    await waitFor(() => expect(result.current.loading).toBe(false));
    const stream = vi.mocked(streamAgentJob).mock.calls[0][1];
    let pageOptions!: NonNullable<Parameters<typeof listAllAgentThreadEvents>[1]>;
    let finish!: (rows: AgentThreadEventApi[]) => void;
    vi.mocked(listAllAgentThreadEvents).mockImplementationOnce((_id, options) => {
      pageOptions = options!;
      return new Promise((resolve) => (finish = resolve));
    });
    act(() => result.current.reload());
    await waitFor(() => expect(pageOptions).toBeDefined());
    const live = event(4, 'message', { text: 'Arrived during refresh' });
    act(() => {
      stream.onEvent(live);
      pageOptions.onPage?.([first]);
    });
    expect(result.current.events).toEqual([first, second, own, live]);
    await act(async () => finish([first, second, own]));
    expect(result.current.events).toEqual([first, second, own, live]);
    expect(streamAgentJob).toHaveBeenCalledOnce();
  });

  it('keeps older transcript activity on refresh when the selected turn has no events', async () => {
    const previous = { ...running('previous-job'), state: 'succeeded', turn_no: 1 } as AgentJobApi;
    const current = {
      ...running(),
      state: 'queued',
      turn_no: 2,
      current_attempt_id: null,
    } as AgentJobApi;
    const first = { ...event(1, 'message', { text: 'Older first' }), job_id: previous.id };
    const second = { ...event(2, 'message', { text: 'Older second' }), job_id: previous.id };
    serve(current, [first, second]);
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: current.thread_id,
      jobs: [previous, current],
      messages: [
        { id: 1, role: 'assistant', job_id: previous.id, content: 'Older first', created_at: null },
        {
          id: 2,
          role: 'assistant',
          job_id: previous.id,
          content: 'Older second',
          created_at: null,
        },
      ],
    } as AgentThreadApi);
    const { result } = renderHook(() => useAgentJob(current.id));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.job?.events).toHaveLength(0);
    expect(result.current.events).toEqual([first, second]);

    let pageOptions!: NonNullable<Parameters<typeof listAllAgentThreadEvents>[1]>;
    let finish!: (rows: AgentThreadEventApi[]) => void;
    vi.mocked(listAllAgentThreadEvents).mockImplementationOnce((_id, options) => {
      pageOptions = options!;
      return new Promise((resolve) => (finish = resolve));
    });
    act(() => result.current.reload());
    await waitFor(() => expect(pageOptions).toBeDefined());
    expect(result.current.loading).toBe(true);
    expect(result.current.events).toEqual([first, second]);
    expect(result.current.job?.historyTurns?.[0].events.map((row) => row.sourceEventId)).toEqual([
      first.id,
      second.id,
    ]);

    act(() => pageOptions.onPage?.([first]));
    expect(result.current.events).toEqual([first, second]);
    expect(result.current.job?.historyTurns?.[0].events.map((row) => row.sourceEventId)).toEqual([
      first.id,
      second.id,
    ]);
    await act(async () => finish([first, second]));
    expect(result.current.loading).toBe(false);
    expect(result.current.events).toEqual([first, second]);
    expect(streamAgentJob).toHaveBeenCalledOnce();
  });

  it('keeps the previous live replies while the next turn metadata is pending', async () => {
    const first = { ...running(), turn_no: 1 } as AgentJobApi;
    const next = {
      ...running('next-job'),
      state: 'queued',
      turn_no: 2,
      current_attempt_id: null,
    } as AgentJobApi;
    const before = event(1, 'message', { text: 'Saved first reply' });
    const live = event(2, 'message', { text: 'Streamed after the durable snapshot' });
    const thread = {
      thread_id: first.thread_id,
      jobs: [first],
      messages: [
        { id: 1, role: 'user', job_id: first.id, content: 'First prompt', created_at: null },
        {
          id: 2,
          role: 'assistant',
          job_id: first.id,
          content: 'Saved first reply',
          created_at: null,
        },
      ],
    } as AgentThreadApi;
    serve(first, [before]);
    vi.mocked(getAgentJobThread).mockResolvedValue(thread);
    const { result, rerender } = renderHook(({ id }) => useAgentJob(id), {
      initialProps: { id: first.id },
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    const stream = vi.mocked(streamAgentJob).mock.calls[0][1];
    act(() => stream.onEvent(live));
    expect(
      result.current.job?.events.filter((row) => row.kind === 'message').map((row) => row.text),
    ).toContain('Streamed after the durable snapshot');

    let resolveNext!: (job: AgentJobApi) => void;
    vi.mocked(getAgentJob).mockImplementationOnce(
      () => new Promise((resolve) => (resolveNext = resolve)),
    );
    vi.mocked(getAgentJobThread).mockResolvedValueOnce({ ...thread, jobs: [first, next] });
    rerender({ id: next.id });
    await act(async () => {});
    expect(result.current.loading).toBe(true);
    expect(result.current.job?.id).toBe(first.id);
    expect(stream.signal?.aborted).toBe(true);
    expect(
      result.current.job?.events.filter((row) => row.kind === 'message').map((row) => row.text),
    ).toEqual(['Saved first reply', 'Streamed after the durable snapshot']);

    await act(async () => resolveNext(next));
    expect(result.current.loading).toBe(false);
    expect(result.current.job?.id).toBe(next.id);
    expect(result.current.error).toBeNull();
  });

  it('resumes a 5,000-event backlog after the selected turn, despite newer other-turn ids', async () => {
    const own = Array.from({ length: 5_000 }, (_, index) => ({
      ...event(index + 1, 'message', { text: `Saved ${index + 1}` }),
      created_at: '2026-09-11T10:00:00Z',
    }));
    const other = { ...event(9_000, 'message', { text: 'Another turn' }), job_id: 'other-job' };
    serve(running(), [...own, other]);
    const { result } = renderHook(() => useAgentJob('live-job'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(streamAgentJob).toHaveBeenCalledOnce();
    expect(vi.mocked(streamAgentJob).mock.calls[0][1].lastEventId).toBe(5_000);
    expect(result.current.events).toHaveLength(5_001);
  });

  it('receives an event stored after the history snapshot and before stream subscription', async () => {
    const snapshot = event(3, 'message', { text: 'In the snapshot' });
    const between = event(4, 'message', { text: 'After the snapshot' });
    serve(running(), [snapshot]);
    const stored = [snapshot];
    vi.mocked(listAllAgentThreadEvents).mockImplementation(async () => {
      const rows = [...stored];
      stored.push(between);
      return rows;
    });
    vi.mocked(streamAgentJob).mockImplementation((_id, options) => {
      // The server returns exactly the events newer than the submitted cursor.
      for (const item of stored) {
        if (item.id > (options.lastEventId ?? 0)) options.onEvent(item);
      }
      return stayOpen();
    });
    const { result } = renderHook(() => useAgentJob('live-job'));
    await waitFor(() => expect(result.current.events.map((item) => item.id)).toEqual([3, 4]));
    expect(vi.mocked(streamAgentJob).mock.calls[0][1].lastEventId).toBe(3);
  });

  it('uses an empty turn cursor and a new owner cursor instead of the previous snapshot', async () => {
    const first = running();
    const next = running('next-job');
    const oldEvent = event(50, 'message', { text: 'Previous turn' });
    serve(first, [oldEvent]);
    let userId = 'alice';
    const wrapper = ({ children }: { children: ReactNode }) => (
      <AgentReadProvider userId={userId}>{children}</AgentReadProvider>
    );
    const { result, rerender } = renderHook(({ id }) => useAgentJob(id), {
      initialProps: { id: first.id },
      wrapper,
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    const oldStream = vi.mocked(streamAgentJob).mock.calls[0][1];
    expect(oldStream.lastEventId).toBe(50);

    serve(next, [oldEvent]);
    rerender({ id: next.id });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(oldStream.signal?.aborted).toBe(true);
    expect(streamAgentJob).toHaveBeenCalledTimes(2);
    const nextStream = vi.mocked(streamAgentJob).mock.calls[1][1];
    expect(nextStream.lastEventId).toBe(0);

    let resolveHistory!: (rows: AgentThreadEventApi[]) => void;
    vi.mocked(listAllAgentThreadEvents).mockImplementationOnce(
      () => new Promise((resolve) => (resolveHistory = resolve)),
    );
    userId = 'bob';
    rerender({ id: next.id });
    await act(async () => {});
    expect(nextStream.signal?.aborted).toBe(true);
    expect(streamAgentJob).toHaveBeenCalledTimes(2);
    await act(async () =>
      resolveHistory([{ ...event(2, 'message', { text: 'New owner' }), job_id: next.id }]),
    );
    expect(streamAgentJob).toHaveBeenCalledTimes(3);
    expect(vi.mocked(streamAgentJob).mock.calls[2][1].lastEventId).toBe(2);
  });

  it('waits for the history snapshot when an owner metadata push arrives first', async () => {
    vi.useFakeTimers();
    const job = running();
    serve(job, []);
    let resolveHistory!: (rows: AgentThreadEventApi[]) => void;
    vi.mocked(listAllAgentThreadEvents).mockImplementationOnce(
      () => new Promise((resolve) => (resolveHistory = resolve)),
    );
    const { result } = renderHook(() => useAgentJob(job.id));
    await act(async () => {});
    expect(listAllAgentThreadEvents).toHaveBeenCalledOnce();
    expect(streamAgentJob).not.toHaveBeenCalled();
    vi.mocked(getAgentJob).mockResolvedValue({ ...job, revision: 2 });
    await act(async () => {
      receiveJobChange({
        id: job.id,
        thread_id: job.thread_id ?? undefined,
        revision: 2,
        state: 'running',
      });
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    expect(result.current.job?.state).toBe('running');
    expect(getAgentJob).toHaveBeenCalledTimes(2);
    expect(streamAgentJob).not.toHaveBeenCalled();
    await act(async () => resolveHistory([event(17, 'message', { text: 'Stored history' })]));
    expect(streamAgentJob).toHaveBeenCalledOnce();
    expect(vi.mocked(streamAgentJob).mock.calls[0][1].lastEventId).toBe(17);
  });

  it('discards buffered stream rows when the owner read scope changes', async () => {
    const job = running();
    serve(job, [event(50, 'message', { text: 'Alice history' })]);
    let userId = 'alice';
    const wrapper = ({ children }: { children: ReactNode }) => (
      <AgentReadProvider userId={userId}>{children}</AgentReadProvider>
    );
    const { result, rerender } = renderHook(() => useAgentJob(job.id), { wrapper });
    await waitFor(() => expect(result.current.loading).toBe(false));
    const oldStream = vi.mocked(streamAgentJob).mock.calls[0][1];
    act(() => oldStream.onEvent(event(51, 'message', { text: 'Alice live reply' })));
    expect(result.current.events.map((row) => row.id)).toEqual([50, 51]);

    serve(job, [event(3, 'message', { text: 'Bob history' })]);
    userId = 'bob';
    rerender();
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(oldStream.signal?.aborted).toBe(true);
    expect(result.current.events.map((row) => row.id)).toEqual([3]);
    expect(result.current.job?.events.map((row) => row.sourceEventId)).toEqual([3]);
    expect(vi.mocked(streamAgentJob).mock.calls[1][1].lastEventId).toBe(3);
  });

  it('skips the history request when a previous route metadata response arrives late', async () => {
    const first = running();
    const next = running('next-job');
    serve(next, []);
    let resolveOldJob!: (job: AgentJobApi) => void;
    vi.mocked(getAgentJob).mockImplementationOnce(
      () => new Promise((resolve) => (resolveOldJob = resolve)),
    );
    const { result, rerender } = renderHook(({ id }) => useAgentJob(id), {
      initialProps: { id: first.id },
    });
    expect(listAllAgentThreadEvents).not.toHaveBeenCalled();
    rerender({ id: next.id });
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(async () => resolveOldJob(first));
    expect(vi.mocked(listAllAgentThreadEvents).mock.calls).toEqual([
      [next.id, { after: 0, signal: expect.any(AbortSignal), onPage: expect.any(Function) }],
    ]);
    expect(vi.mocked(streamAgentJob).mock.calls.map(([id]) => id)).toEqual([next.id]);
    expect(result.current.job?.id).toBe(next.id);
  });

  it('retries a dropped stream using the refreshed history cursor', async () => {
    vi.useFakeTimers();
    const earlier = event(5, 'message', { text: 'Before the drop' });
    const later = event(8, 'message', { text: 'While disconnected' });
    serve(running(), [earlier, later]);
    vi.mocked(listAllAgentThreadEvents).mockResolvedValueOnce([earlier]);
    vi.mocked(streamAgentJob).mockResolvedValueOnce(undefined).mockImplementation(stayOpen);
    const { result } = renderHook(() => useAgentJob('live-job'));
    await act(async () => {});
    expect(vi.mocked(streamAgentJob).mock.calls[0][1].lastEventId).toBe(5);
    expect(listAllAgentThreadEvents).toHaveBeenCalledTimes(2);
    await act(async () => vi.advanceTimersByTimeAsync(STREAM_RETRY_MIN_MS));
    expect(streamAgentJob).toHaveBeenCalledTimes(2);
    expect(vi.mocked(streamAgentJob).mock.calls[1][1].lastEventId).toBe(8);
    expect(result.current.events.map((item) => item.id)).toEqual([5, 8]);
  });
});

describe('useAgentJob live turn rows', () => {
  it('preserves and deduplicates streamed events while a history reload is pending', async () => {
    const job = {
      id: 'live-job',
      state: 'queued',
      conversation_state: 'queued',
      current_attempt_id: null,
      thread_id: 'live-thread',
      turn_no: 1,
      task_prompt: 'Task',
      runtime: 'codex',
      model: 'test',
      title: 'Title',
      title_source: 'generated',
    } as AgentJobApi;
    const before = event(1, 'message', { text: 'Before reload' });
    const during = event(2, 'message', { text: 'During reload' });
    const started = event(3, 'lifecycle', { phase: 'started' });
    vi.mocked(getAgentJob).mockResolvedValue(job);
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: 'live-thread',
      jobs: [job],
      messages: [],
    } as unknown as AgentThreadApi);
    vi.mocked(listAllAgentThreadEvents).mockResolvedValueOnce([before]);
    vi.mocked(streamAgentJob).mockImplementation(stayOpen);
    const { result } = renderHook(() => useAgentJob(job.id));
    await waitFor(() => expect(result.current.loading).toBe(false));
    let resolveHistory!: (events: AgentThreadEventApi[]) => void;
    vi.mocked(listAllAgentThreadEvents).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveHistory = resolve;
        }),
    );
    act(() => result.current.reload());
    await waitFor(() => expect(resolveHistory).toBeDefined());
    const stream = vi.mocked(streamAgentJob).mock.calls[0][1];
    act(() => {
      stream.onEvent(before);
      stream.onEvent(during);
      stream.onEvent(started);
    });
    expect(result.current.events.map((row) => row.id)).toEqual([1, 2, 3]);
    expect(result.current.job?.events.map((row) => row.sourceEventId)).toEqual([1, 2, 3]);
    expect(result.current.job?.state).toBe('running');
    await act(async () => resolveHistory([before]));
    expect(result.current.events.map((row) => row.id)).toEqual([1, 2, 3]);
    expect(result.current.job?.events.map((row) => row.sourceEventId)).toEqual([1, 2, 3]);
    expect(result.current.job?.state).toBe('running');
    expect(result.current.threadJobs[0]).toMatchObject({
      state: 'running',
      current_attempt_id: 10,
    });
    act(() => stream.onEvent(during));
    expect(result.current.events.map((row) => row.id)).toEqual([1, 2, 3]);
    expect(streamAgentJob).toHaveBeenCalledOnce();
  });

  it('retains live content after a failed read and merges it into the retry snapshot', async () => {
    const job = {
      id: 'live-job',
      state: 'running',
      conversation_state: 'running',
      current_attempt_id: 10,
      thread_id: 'live-thread',
      turn_no: 1,
      task_prompt: 'Task',
      runtime: 'codex',
      model: 'test',
      title: 'Title',
      title_source: 'generated',
    } as AgentJobApi;
    const before = event(1, 'message', { text: 'Before reload' });
    const during = event(2, 'message', { text: 'During failed reload' });
    const publishing = event(3, 'lifecycle', { phase: 'publishing' });
    vi.mocked(getAgentJob).mockResolvedValue(job);
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: 'live-thread',
      jobs: [job],
      messages: [],
    } as unknown as AgentThreadApi);
    vi.mocked(listAllAgentThreadEvents).mockResolvedValueOnce([before]);
    vi.mocked(streamAgentJob).mockImplementation(stayOpen);
    const { result } = renderHook(() => useAgentJob(job.id));
    await waitFor(() => expect(result.current.loading).toBe(false));
    let rejectHistory!: (cause: Error) => void;
    vi.mocked(listAllAgentThreadEvents).mockImplementationOnce(
      () =>
        new Promise((_, reject) => {
          rejectHistory = reject;
        }),
    );
    act(() => result.current.reload());
    await waitFor(() => expect(rejectHistory).toBeDefined());
    const stream = vi.mocked(streamAgentJob).mock.calls[0][1];
    act(() => stream.onEvent(during));
    await act(async () => rejectHistory(new Error('History unavailable')));
    expect(result.current.error).toBe('History unavailable');
    expect(result.current.job?.events.map((row) => row.sourceEventId)).toEqual([1, 2]);
    act(() => stream.onEvent(publishing));
    vi.mocked(listAllAgentThreadEvents).mockResolvedValueOnce([before]);
    act(() => result.current.reload());
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeNull();
    expect(result.current.events.map((row) => row.id)).toEqual([1, 2, 3]);
    expect(result.current.job?.events.map((row) => row.sourceEventId)).toEqual([1, 2, 3]);
    expect(result.current.job?.state).toBe('running');
    expect(result.current.threadJobs[0].state).toBe('publishing');
    expect(streamAgentJob).toHaveBeenCalledOnce();
  });

  it('keeps the previous transcript while loading a follow-up without mixing their streams', async () => {
    const first = {
      id: 'live-job',
      state: 'running',
      current_attempt_id: 10,
      thread_id: 'live-thread',
      turn_no: 1,
      task_prompt: 'First question',
      runtime: 'codex',
      model: 'test',
      title_source: 'generated',
    } as AgentJobApi;
    const next: AgentJobApi = {
      ...first,
      id: 'next-job',
      turn_no: 2,
      state: 'queued',
      current_attempt_id: null,
    };
    const answer = event(1, 'message', { text: 'First answer' });
    const firstThread = {
      thread_id: first.thread_id,
      jobs: [first],
      messages: [],
    } as unknown as AgentThreadApi;
    let resolveNext!: (job: AgentJobApi) => void;
    vi.mocked(getAgentJob).mockImplementation((id) =>
      id === first.id ? Promise.resolve(first) : new Promise((resolve) => (resolveNext = resolve)),
    );
    vi.mocked(getAgentJobThread).mockImplementation(async (id) =>
      id === first.id ? firstThread : { ...firstThread, jobs: [first, next] },
    );
    vi.mocked(listAllAgentThreadEvents).mockResolvedValue([answer]);
    vi.mocked(streamAgentJob).mockImplementation(stayOpen);

    const { result, rerender } = renderHook(({ id }) => useAgentJob(id), {
      initialProps: { id: first.id },
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    const oldStream = vi.mocked(streamAgentJob).mock.calls[0][1];
    expect(result.current.events).toEqual([answer]);

    rerender({ id: next.id });
    expect(result.current.loading).toBe(true);
    expect(result.current.job?.id).toBe(first.id);
    expect(result.current.events).toEqual([answer]);
    expect(oldStream.signal?.aborted).toBe(true);
    expect(streamAgentJob).toHaveBeenCalledTimes(1);

    await act(async () => resolveNext(next));
    expect(result.current.job?.id).toBe(next.id);
    expect(result.current.events).toEqual([answer]);
    expect(streamAgentJob).toHaveBeenCalledTimes(2);
    const newStream = vi.mocked(streamAgentJob).mock.calls[1][1];
    act(() => newStream.onEvent(event(2, 'lifecycle', { phase: 'started' }, 11)));
    expect(result.current.job?.state).toBe('running');
    expect(result.current.events.at(-1)).toMatchObject({ job_id: next.id, turn_no: 2 });
    expect(result.current.threadJobs[0]).toEqual(first);
  });

  describe('when the conversation is read again', () => {
    function turn(id: string, turn_no: number, state = 'succeeded'): AgentJobApi {
      return {
        id,
        thread_id: 'live-thread',
        state,
        current_attempt_id: turn_no,
        turn_no,
        task_prompt: `Question ${turn_no}`,
        runtime: 'codex',
        model: 'test',
        title_source: 'generated',
      } as AgentJobApi;
    }

    function stored(id: number, job: AgentJobApi): AgentThreadEventApi {
      return {
        ...event(id, 'message', { text: `Reply ${id}` }, job.turn_no!),
        job_id: job.id,
        turn_no: job.turn_no!,
      };
    }

    /** The thread's event log, read the way the server pages it: after a cursor. */
    function serveLog(jobs: AgentJobApi[], log: AgentThreadEventApi[]) {
      vi.mocked(getAgentJob).mockImplementation(async (id) => jobs.find((job) => job.id === id)!);
      vi.mocked(getAgentJobThread).mockImplementation(async (id) => {
        const job = jobs.find((row) => row.id === id)!;
        return {
          thread_id: job.thread_id,
          jobs: jobs.filter((row) => row.thread_id === job.thread_id),
          messages: [],
        } as AgentThreadApi;
      });
      vi.mocked(listAllAgentThreadEvents).mockImplementation(async (id, options) => {
        const threadId = jobs.find((job) => job.id === id)!.thread_id;
        const threadJobs = new Set(
          jobs.filter((job) => job.thread_id === threadId).map((j) => j.id),
        );
        const rows = log.filter((row) => threadJobs.has(row.job_id));
        return rows
          .filter((row) => row.id > (options?.after ?? 0))
          .sort((left, right) => left.id - right.id);
      });
      vi.mocked(streamAgentJob).mockImplementation(stayOpen);
    }

    function readCursors(): number[] {
      return vi.mocked(listAllAgentThreadEvents).mock.calls.map(([, options]) => options!.after!);
    }

    it('reads only what a follow-up adds, keeping the earlier turns on screen', async () => {
      const turns = [turn('turn-1', 1), turn('turn-2', 2), turn('turn-3', 3)];
      const log = [
        ...[1, 2, 3].map((id) => stored(id, turns[0])),
        ...[4, 5, 6].map((id) => stored(id, turns[1])),
        ...[7, 8, 9].map((id) => stored(id, turns[2])),
      ];
      serveLog(turns, log);
      const { result, rerender } = renderHook(({ id }) => useAgentJob(id), {
        initialProps: { id: 'turn-3' },
      });
      await waitFor(() => expect(result.current.loading).toBe(false));
      expect(result.current.events.map((row) => row.id)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);

      const followUp = turn('turn-4', 4, 'running');
      turns.push(followUp);
      log.push(stored(10, followUp));
      let resolveEvents!: (rows: AgentThreadEventApi[]) => void;
      vi.mocked(listAllAgentThreadEvents).mockImplementationOnce(
        () => new Promise((resolve) => (resolveEvents = resolve)),
      );
      rerender({ id: followUp.id });
      await waitFor(() => expect(result.current.job?.id).toBe(followUp.id));
      // The earlier turns stay on screen while the new activity is read.
      expect(result.current.events.map((row) => row.id)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);
      // The last two turns are read again in full; the ones before them are
      // kept as they are.
      expect(readCursors()).toEqual([0, 3]);

      await act(async () => resolveEvents(log.filter((row) => row.id > 3)));
      expect(result.current.loading).toBe(false);
      expect(result.current.events.map((row) => row.id)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
      expect(result.current.job?.historyTurns?.map((row) => row.events.length)).toEqual([3, 3, 3]);
      expect(vi.mocked(streamAgentJob).mock.calls.at(-1)![1].lastEventId).toBe(10);
    });

    it('reads a write to the ended turn that committed after the next turn began', async () => {
      const turns = [turn('turn-1', 1), turn('turn-2', 2), turn('turn-3', 3)];
      // Turn 2's snapshot event took id 5 and was still committing when turn
      // 3 stored 6 and 7, so the first read could not see it.
      const snapshot = stored(5, turns[1]);
      const log = [
        ...[1, 2].map((id) => stored(id, turns[0])),
        ...[3, 4].map((id) => stored(id, turns[1])),
        ...[6, 7].map((id) => stored(id, turns[2])),
      ];
      serveLog(turns, log);
      const { result, rerender } = renderHook(({ id }) => useAgentJob(id), {
        initialProps: { id: 'turn-3' },
      });
      await waitFor(() => expect(result.current.loading).toBe(false));
      expect(result.current.events.map((row) => row.id)).toEqual([1, 2, 3, 4, 6, 7]);

      log.push(snapshot);
      const followUp = turn('turn-4', 4, 'running');
      turns.push(followUp);
      log.push(stored(8, followUp));
      rerender({ id: followUp.id });
      await waitFor(() => expect(result.current.job?.id).toBe(followUp.id));
      await waitFor(() => expect(result.current.loading).toBe(false));
      expect(readCursors()).toEqual([0, 2]);
      expect(result.current.events.map((row) => row.id)).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
    });

    it('reads its own turn and the one before it again when it ends', async () => {
      const turns = [turn('turn-1', 1), turn('turn-2', 2), turn('turn-3', 3, 'running')];
      const log = [
        ...[1, 2].map((id) => stored(id, turns[0])),
        ...[3, 4].map((id) => stored(id, turns[1])),
        ...[5, 6].map((id) => stored(id, turns[2])),
      ];
      serveLog(turns, log);
      const { result } = renderHook(() => useAgentJob('turn-3'));
      await waitFor(() => expect(result.current.loading).toBe(false));
      const stream = vi.mocked(streamAgentJob).mock.calls[0][1];

      log.push(stored(7, turns[2]));
      turns[2] = { ...turns[2], state: 'succeeded' };
      act(() => stream.onEvent(log[6]));
      await act(async () => stream.onFinished?.({ state: 'succeeded', published_pr_url: null }));
      await waitFor(() => expect(readCursors()).toEqual([0, 2]));
      await waitFor(() => expect(result.current.loading).toBe(false));
      expect(result.current.events.map((row) => row.id)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    });

    it('reads another conversation from its start', async () => {
      const first = turn('turn-1', 1);
      const other = { ...turn('other-1', 1), thread_id: 'other-thread' };
      serveLog([first, other], [stored(1, first), stored(2, other)]);
      const { result, rerender } = renderHook(({ id }) => useAgentJob(id), {
        initialProps: { id: first.id },
      });
      await waitFor(() => expect(result.current.loading).toBe(false));
      rerender({ id: other.id });
      await waitFor(() => expect(result.current.job?.id).toBe(other.id));
      await waitFor(() => expect(result.current.loading).toBe(false));
      expect(readCursors()).toEqual([0, 0]);
      expect(result.current.events.map((row) => row.id)).toEqual([2]);
    });

    it('reads a turn without a conversation from its start each time', async () => {
      const lone = turn('lone', 1);
      serveLog([lone], [stored(1, lone), stored(2, lone)]);
      vi.mocked(getAgentJobThread).mockResolvedValue(null);
      const { result } = renderHook(() => useAgentJob(lone.id));
      await waitFor(() => expect(result.current.loading).toBe(false));
      act(() => result.current.reload());
      await waitFor(() => expect(readCursors()).toEqual([0, 0]));
      await waitFor(() => expect(result.current.loading).toBe(false));
      expect(result.current.events.map((row) => row.id)).toEqual([1, 2]);
    });

    it('reads the conversation from its start for another owner', async () => {
      const first = turn('turn-1', 1);
      serveLog([first], [stored(1, first), stored(2, first)]);
      let userId = 'alice';
      const wrapper = ({ children }: { children: ReactNode }) => (
        <AgentReadProvider userId={userId}>{children}</AgentReadProvider>
      );
      const { result, rerender } = renderHook(() => useAgentJob(first.id), { wrapper });
      await waitFor(() => expect(result.current.loading).toBe(false));
      userId = 'bob';
      rerender();
      await waitFor(() => expect(readCursors()).toEqual([0, 0]));
      await waitFor(() => expect(result.current.loading).toBe(false));
      expect(result.current.events.map((row) => row.id)).toEqual([1, 2]);
    });
  });

  it('refreshes shared activity while the selected follow-up waits and hands off to its stream', async () => {
    vi.useFakeTimers();
    const initial = {
      id: 'queued-followup',
      state: 'queued',
      conversation_state: 'running',
      thread_id: 'live-thread',
      turn_no: 2,
      task_prompt: 'A follow-up',
      runtime: 'codex',
      model: 'test',
      title_source: 'generated',
    } as AgentJobApi;
    vi.mocked(getAgentJob)
      .mockResolvedValueOnce(initial)
      .mockResolvedValueOnce({ ...initial, conversation_state: 'queued' })
      .mockResolvedValue({ ...initial, state: 'running', conversation_state: 'running' });
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: 'live-thread',
      jobs: [initial],
      messages: [],
    } as unknown as AgentThreadApi);
    vi.mocked(listAllAgentThreadEvents).mockResolvedValue([]);
    vi.mocked(streamAgentJob).mockImplementation(stayOpen);

    const { result } = renderHook(() => useAgentJob(initial.id));
    await act(async () => {});
    expect(result.current.job?.state).toBe('queued');
    expect(result.current.job?.conversationState).toBe('running');
    expect(streamAgentJob).toHaveBeenCalledOnce();

    // The earlier turn settles without sending an event on this turn's stream.
    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    expect(result.current.job?.state).toBe('queued');
    expect(result.current.job?.conversationState).toBe('queued');
    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    expect(result.current.job?.state).toBe('running');
    expect(result.current.job?.conversationState).toBe('running');
    expect(getAgentJob).toHaveBeenCalledTimes(3);

    // Its own stream now carries the running turn through completion.
    await act(async () => vi.advanceTimersByTimeAsync(15_000));
    expect(getAgentJob).toHaveBeenCalledTimes(3);
    expect(streamAgentJob).toHaveBeenCalledOnce();
  });

  it.each([
    ['cancelled', 'running'],
    ['succeeded', 'queued'],
  ] as const)(
    'refreshes a %s turn while its conversation is %s and stops once idle',
    async (state, conversation_state) => {
      vi.useFakeTimers();
      const initial = {
        id: 'old-job',
        state,
        conversation_state,
        thread_id: 'live-thread',
        task_prompt: 'An earlier turn',
        runtime: 'codex',
        model: 'test',
        title_source: 'generated',
      } as AgentJobApi;
      vi.mocked(getAgentJob)
        .mockResolvedValueOnce(initial)
        .mockResolvedValue({ ...initial, conversation_state: 'idle' });
      vi.mocked(getAgentJobThread).mockResolvedValue({
        thread_id: 'live-thread',
        jobs: [initial],
        messages: [],
      } as unknown as AgentThreadApi);
      vi.mocked(listAllAgentThreadEvents).mockResolvedValue([]);

      const { result } = renderHook(() => useAgentJob(initial.id));
      await act(async () => {});
      expect(result.current.job?.conversationState).toBe(conversation_state);
      expect(streamAgentJob).not.toHaveBeenCalled();
      await act(async () => vi.advanceTimersByTimeAsync(5_000));
      expect(result.current.job?.conversationState).toBe('idle');
      expect(result.current.job?.state).toBe(state === 'cancelled' ? 'cancelled' : 'needs_review');
      expect(getAgentJob).toHaveBeenCalledTimes(2);
      expect(getAgentJobThread).toHaveBeenCalledOnce();
      await act(async () => vi.advanceTimersByTimeAsync(15_000));
      expect(getAgentJob).toHaveBeenCalledTimes(2);
    },
  );

  it('keeps observing a goal that owes the conversation its next round', async () => {
    vi.useFakeTimers();
    // The round on screen has settled and the next one has not been queued yet,
    // so the conversation reads idle while the platform still owes this goal a
    // round. The goal row is the only thing that says so.
    const initial = {
      id: 'settled-round',
      state: 'succeeded',
      conversation_state: 'idle',
      thread_id: 'live-thread',
      turn_no: 1,
      task_prompt: 'An automatic round',
      runtime: 'codex',
      model: 'test',
      title_source: 'generated',
      goal: {
        goal_id: 'goal_1',
        objective: 'make the tests pass',
        state: 'active',
        revision: 2,
        max_rounds: 10,
        rounds_used: 1,
        pause_reason: null,
        pause_detail: null,
        completion_basis: null,
        completed_by_job_id: null,
        created_at: null,
        updated_at: null,
      },
    } as AgentJobApi;
    vi.mocked(getAgentJob)
      .mockResolvedValueOnce(initial)
      .mockResolvedValue({
        ...initial,
        conversation_state: 'queued',
        goal: { ...initial.goal!, rounds_used: 2 },
      });
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: 'live-thread',
      jobs: [initial],
      messages: [],
    } as unknown as AgentThreadApi);
    vi.mocked(listAllAgentThreadEvents).mockResolvedValue([]);

    const { result } = renderHook(() => useAgentJob(initial.id));
    await act(async () => {});
    expect(result.current.job?.conversationState).toBe('idle');
    expect(result.current.job?.goal?.roundsUsed).toBe(1);
    expect(streamAgentJob).not.toHaveBeenCalled();

    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    expect(getAgentJob).toHaveBeenCalledTimes(2);
    expect(result.current.job?.conversationState).toBe('queued');
    expect(result.current.job?.goal?.roundsUsed).toBe(2);
  });

  it('stops observing once the goal is no longer active', async () => {
    vi.useFakeTimers();
    const initial = {
      id: 'finished-goal',
      state: 'succeeded',
      conversation_state: 'idle',
      thread_id: 'live-thread',
      turn_no: 4,
      task_prompt: 'The last round',
      runtime: 'codex',
      model: 'test',
      title_source: 'generated',
      goal: {
        goal_id: 'goal_1',
        objective: 'make the tests pass',
        state: 'complete',
        revision: 3,
        max_rounds: 10,
        rounds_used: 4,
        pause_reason: null,
        pause_detail: null,
        completion_basis: 'model_reported',
        completed_by_job_id: 'finished-goal',
        created_at: null,
        updated_at: null,
      },
    } as AgentJobApi;
    vi.mocked(getAgentJob).mockResolvedValue(initial);
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: 'live-thread',
      jobs: [initial],
      messages: [],
    } as unknown as AgentThreadApi);
    vi.mocked(listAllAgentThreadEvents).mockResolvedValue([]);

    const { result } = renderHook(() => useAgentJob(initial.id));
    await act(async () => {});
    expect(result.current.job?.goal?.state).toBe('complete');
    await act(async () => vi.advanceTimersByTimeAsync(20_000));
    expect(getAgentJob).toHaveBeenCalledTimes(1);
  });

  it('carries a replaced attempt into the rows that scope the inspector', async () => {
    const initial = {
      id: 'live-job',
      state: 'running',
      current_attempt_id: 10,
      turn_no: 1,
      thread_id: 'live-thread',
      task_prompt: 'Live fixture',
      runtime: 'codex',
      model: 'test',
      created_at: '2026-09-11T10:00:00.000Z',
      title_source: 'generated',
    } as AgentJobApi;
    vi.mocked(getAgentJob).mockResolvedValue(initial);
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: 'live-thread',
      jobs: [initial],
      messages: [],
    } as unknown as AgentThreadApi);
    vi.mocked(listAllAgentThreadEvents).mockResolvedValue([
      event(1, 'tool_use', { id: 'old', name: 'Bash' }),
    ]);
    let stream: Parameters<typeof streamAgentJob>[1] | undefined;
    vi.mocked(streamAgentJob).mockImplementation((_jobId, options) => {
      stream = options;
      return stayOpen();
    });

    const { result } = renderHook(() => useAgentJob('live-job'));
    await waitFor(() => expect(result.current.loading).toBe(false));
    await waitFor(() => expect(stream).toBeDefined());

    act(() => {
      stream!.onEvent(event(2, 'attempt_superseded', { reason: 'lease_expired' }));
      stream!.onEvent(event(3, 'lifecycle', { phase: 'started' }, 11));
      stream!.onEvent(event(4, 'tool_use', { id: 'new', name: 'Bash' }, 11));
    });

    // The chat model already follows the replacement; the turn rows the
    // inspector scopes its statuses with have to follow it too, or the call on
    // the attempt that is gone reads as still waiting.
    expect(result.current.job?.currentAttemptId).toBe(11);
    const steps = buildTrajectorySteps(
      result.current.events,
      turnContextFromJobs(result.current.threadJobs),
    );
    expect(steps.filter((step) => step.kind === 'tool').map((step) => step.status)).toEqual([
      'not_recorded',
      'waiting',
    ]);
  });
});

describe('useAgentJob with the owner change stream', () => {
  afterEach(() => resetAgentPush());

  const settled = {
    id: 'settled-job',
    state: 'succeeded',
    conversation_state: 'idle',
    thread_id: 'live-thread',
    turn_no: 1,
    task_prompt: 'Fix it',
    runtime: 'codex',
    model: 'test',
    title: 'Fix it',
    title_source: 'fallback',
    published_pr_url: null,
    revision: 2,
    thread_revision: 3,
  } as AgentJobApi;

  function serve(job: AgentJobApi): void {
    vi.mocked(getAgentJob).mockResolvedValue(job);
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: job.thread_id,
      jobs: [job],
      messages: [],
    } as unknown as AgentThreadApi);
    vi.mocked(listAllAgentThreadEvents).mockResolvedValue([]);
  }

  it('shows a pushed title at once and keeps it over a read that left before it', async () => {
    vi.useFakeTimers();
    serve(settled);
    const { result } = renderHook(() => useAgentJob(settled.id));
    await act(async () => {});
    expect(result.current.job?.title).toBe('Fix it');

    // The read the push prompts answers with the row as it was (revision 3).
    await act(async () => {
      receiveThreadChange({
        id: 'live-thread',
        revision: 4,
        title: 'Fix the flaky test',
        title_source: 'generated',
      });
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });

    expect(result.current.job?.title).toBe('Fix the flaky test');
    // One read of the turn's row; the conversation and its events are not
    // fetched again for a field that changed on it.
    expect(getAgentJob).toHaveBeenCalledTimes(2);
    expect(getAgentJobThread).toHaveBeenCalledTimes(1);
    expect(listAllAgentThreadEvents).toHaveBeenCalledTimes(1);
  });

  it('shows a pushed pull request link for a turn that has ended', async () => {
    vi.useFakeTimers();
    serve(settled);
    const { result } = renderHook(() => useAgentJob(settled.id));
    await act(async () => {});

    await act(async () => {
      receiveJobChange({
        id: settled.id,
        thread_id: 'live-thread',
        revision: 5,
        state: 'succeeded',
        conversation_state: 'idle',
        published_pr_url: 'https://example.test/pr/7',
      });
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });

    expect(result.current.job?.prUrl).toBe('https://example.test/pr/7');
    expect(getAgentJob).toHaveBeenCalledTimes(2);
  });

  it("lets a running turn's own stream end it, so its last events still arrive", async () => {
    vi.useFakeTimers();
    const running = { ...settled, state: 'running', conversation_state: 'running' } as AgentJobApi;
    serve(running);
    let stream!: Parameters<typeof streamAgentJob>[1];
    vi.mocked(streamAgentJob).mockImplementation((_id, options) => {
      stream = options;
      return new Promise(() => undefined);
    });
    const { result } = renderHook(() => useAgentJob(settled.id));
    await act(async () => {});
    expect(streamAgentJob).toHaveBeenCalledTimes(1);

    // The change stream reports the end before the chat stream has delivered
    // the turn's last message, and a read of the row agrees with it.
    const ended = { ...running, state: 'succeeded', conversation_state: 'idle', revision: 5 };
    vi.mocked(getAgentJob).mockResolvedValue(ended as AgentJobApi);
    await act(async () => {
      receiveJobChange({
        id: settled.id,
        thread_id: 'live-thread',
        revision: 5,
        state: 'succeeded',
        conversation_state: 'idle',
      });
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });

    expect(stream.signal?.aborted).toBe(false);
    expect(result.current.job?.state).toBe('running');

    // The chat stream delivers the last message and then ends the turn, which
    // reads the conversation again with everything it said.
    vi.mocked(listAllAgentThreadEvents).mockResolvedValue([
      { ...event(9, 'message', { text: 'The last words' }), job_id: settled.id },
    ]);
    await act(async () => {
      stream.onEvent(event(9, 'message', { text: 'The last words' }) as never);
      stream.onFinished?.({ state: 'succeeded', published_pr_url: null });
      await vi.advanceTimersByTimeAsync(0);
    });

    expect(listAllAgentThreadEvents).toHaveBeenCalledTimes(2);
    expect(result.current.job?.state).not.toBe('running');
  });

  it('keeps a turn its stream ended over a pushed read that left while it ran', async () => {
    vi.useFakeTimers();
    const running = { ...settled, state: 'running', conversation_state: 'running' } as AgentJobApi;
    serve(running);
    let stream!: Parameters<typeof streamAgentJob>[1];
    vi.mocked(streamAgentJob).mockImplementation((_id, options) => {
      stream = options;
      return new Promise(() => undefined);
    });
    const { result } = renderHook(() => useAgentJob(settled.id));
    await act(async () => {});
    expect(streamAgentJob).toHaveBeenCalledTimes(1);

    // A change prompts a read of the row while the turn runs. It is slow.
    let answerStale!: () => void;
    vi.mocked(getAgentJob).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          answerStale = () => resolve(running);
        }),
    );
    await act(async () => {
      receiveJobChange({
        id: settled.id,
        thread_id: 'live-thread',
        revision: 3,
        state: 'running',
        conversation_state: 'running',
      });
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    expect(getAgentJob).toHaveBeenCalledTimes(2);

    // Meanwhile the turn's stream ends it, and the reload reads it settled.
    const ended = { ...running, state: 'succeeded', conversation_state: 'idle', revision: 5 };
    vi.mocked(getAgentJob).mockResolvedValue(ended as AgentJobApi);
    await act(async () => {
      stream.onFinished?.({ state: 'succeeded', published_pr_url: null });
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.job?.state).not.toBe('running');

    // The read from before the end answers last, and is not applied over it.
    await act(async () => {
      answerStale();
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.job?.state).not.toBe('running');
    expect(result.current.threadJobs[0].state).toBe('succeeded');
    expect(streamAgentJob).toHaveBeenCalledTimes(1);
  });

  describe('when its stream cannot connect', () => {
    const running = { ...settled, state: 'running', conversation_state: 'running' } as AgentJobApi;
    const ended = { ...running, state: 'succeeded', conversation_state: 'idle', revision: 5 };
    const lastWords = { ...event(9, 'message', { text: 'The last words' }), job_id: settled.id };

    it('reads the conversation again, which ends a turn that has ended with its last events', async () => {
      vi.useFakeTimers();
      serve(running);
      vi.mocked(streamAgentJob).mockResolvedValue(undefined);
      vi.mocked(getAgentJob)
        .mockResolvedValueOnce(running)
        .mockResolvedValue(ended as AgentJobApi);
      vi.mocked(listAllAgentThreadEvents).mockResolvedValueOnce([]).mockResolvedValue([lastWords]);
      const { result } = renderHook(() => useAgentJob(settled.id));
      await act(async () => {});
      await act(async () => vi.advanceTimersByTimeAsync(0));

      expect(listAllAgentThreadEvents).toHaveBeenCalledTimes(2);
      expect(result.current.job?.state).not.toBe('running');
      expect(result.current.events.map((item) => item.payload?.text)).toContain('The last words');
      await act(async () => vi.advanceTimersByTimeAsync(STREAM_RETRY_MAX_MS));
      expect(streamAgentJob).toHaveBeenCalledTimes(1);
    });

    it('ends a turn that ends during the read that recovers it only with its last events', async () => {
      vi.useFakeTimers();
      serve(running);
      vi.mocked(streamAgentJob).mockResolvedValue(undefined);
      // What the database holds: the last message and the end commit together.
      const stored = { ended: false };
      let answerRow!: () => void;
      vi.mocked(getAgentJob)
        .mockResolvedValueOnce(running)
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              answerRow = () => resolve((stored.ended ? ended : running) as AgentJobApi);
            }),
        )
        .mockResolvedValue(ended as AgentJobApi);
      vi.mocked(listAllAgentThreadEvents).mockImplementation(async () =>
        stored.ended ? [lastWords] : [],
      );
      const { result } = renderHook(() => useAgentJob(settled.id));
      await act(async () => {});
      await act(async () => vi.advanceTimersByTimeAsync(0));
      expect(getAgentJob).toHaveBeenCalledTimes(2);

      // The turn stores its last message and ends while that read is under way.
      stored.ended = true;
      await act(async () => answerRow());
      await act(async () => vi.advanceTimersByTimeAsync(0));

      expect(result.current.job?.state).not.toBe('running');
      expect(result.current.events.map((item) => item.payload?.text)).toContain('The last words');
      await act(async () => vi.advanceTimersByTimeAsync(120_000));
      expect(result.current.events.map((item) => item.payload?.text)).toContain('The last words');
    });

    it('shows the last events of a turn that ends as the conversation opens', async () => {
      vi.useFakeTimers();
      serve(running);
      vi.mocked(streamAgentJob).mockImplementation(stayOpen);
      const stored = { ended: false };
      let answerRow!: () => void;
      vi.mocked(getAgentJob).mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            answerRow = () => resolve((stored.ended ? ended : running) as AgentJobApi);
          }),
      );
      vi.mocked(listAllAgentThreadEvents).mockImplementation(async () =>
        stored.ended ? [lastWords] : [],
      );
      const { result } = renderHook(() => useAgentJob(settled.id));
      await act(async () => {});

      stored.ended = true;
      await act(async () => answerRow());
      await act(async () => vi.advanceTimersByTimeAsync(0));

      expect(result.current.job?.state).not.toBe('running');
      expect(result.current.events.map((item) => item.payload?.text)).toContain('The last words');
      expect(streamAgentJob).not.toHaveBeenCalled();
    });

    it('opens it again after a growing pause while the turn runs', async () => {
      vi.useFakeTimers();
      serve(running);
      vi.mocked(streamAgentJob)
        .mockResolvedValueOnce(undefined)
        .mockResolvedValueOnce(undefined)
        .mockImplementation(stayOpen);
      const { result } = renderHook(() => useAgentJob(settled.id));
      await act(async () => {});
      expect(streamAgentJob).toHaveBeenCalledTimes(1);
      expect(listAllAgentThreadEvents).toHaveBeenCalledTimes(2);
      expect(result.current.job?.state).toBe('running');

      await act(async () => vi.advanceTimersByTimeAsync(STREAM_RETRY_MIN_MS));
      expect(streamAgentJob).toHaveBeenCalledTimes(2);
      // It failed again: twice the pause before the next try.
      await act(async () => vi.advanceTimersByTimeAsync(STREAM_RETRY_MIN_MS));
      expect(streamAgentJob).toHaveBeenCalledTimes(2);
      await act(async () => vi.advanceTimersByTimeAsync(STREAM_RETRY_MIN_MS));
      expect(streamAgentJob).toHaveBeenCalledTimes(3);
      await act(async () => vi.advanceTimersByTimeAsync(STREAM_RETRY_MAX_MS));
      expect(streamAgentJob).toHaveBeenCalledTimes(3);
    });

    it('reads the conversation again once it reconnects, clearing a read that failed meanwhile', async () => {
      vi.useFakeTimers();
      serve(running);
      let stream!: Parameters<typeof streamAgentJob>[1];
      vi.mocked(streamAgentJob)
        .mockResolvedValueOnce(undefined)
        .mockImplementation((_id, options) => {
          stream = options;
          return new Promise(() => undefined);
        });
      // The read that the failed connection prompts fails as well.
      vi.mocked(listAllAgentThreadEvents)
        .mockResolvedValueOnce([])
        .mockRejectedValueOnce(new Error('Failed to fetch'))
        .mockResolvedValue([]);
      const { result } = renderHook(() => useAgentJob(settled.id));
      await act(async () => {});
      expect(result.current.error).toBe('Failed to fetch');

      await act(async () => vi.advanceTimersByTimeAsync(STREAM_RETRY_MIN_MS));
      expect(streamAgentJob).toHaveBeenCalledTimes(2);
      // Connected, though the turn has said nothing new yet.
      await act(async () => {
        stream.onOpen?.();
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(listAllAgentThreadEvents).toHaveBeenCalledTimes(3);
      expect(result.current.error).toBeNull();
      expect(result.current.job?.state).toBe('running');

      // Later connections of the same stream are its ordinary resumes.
      await act(async () => {
        stream.onOpen?.();
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(listAllAgentThreadEvents).toHaveBeenCalledTimes(3);
      expect(streamAgentJob).toHaveBeenCalledTimes(2);
    });

    it('does not read the conversation again when its stream opens first time', async () => {
      vi.useFakeTimers();
      serve(running);
      let stream!: Parameters<typeof streamAgentJob>[1];
      vi.mocked(streamAgentJob).mockImplementation((_id, options) => {
        stream = options;
        return new Promise(() => undefined);
      });
      renderHook(() => useAgentJob(settled.id));
      await act(async () => {});
      await act(async () => {
        stream.onOpen?.();
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(listAllAgentThreadEvents).toHaveBeenCalledTimes(1);
    });

    it('reads the conversation whole when a pushed end arrives before it is open again', async () => {
      vi.useFakeTimers();
      serve(running);
      vi.mocked(streamAgentJob).mockResolvedValueOnce(undefined).mockImplementation(stayOpen);
      const { result } = renderHook(() => useAgentJob(settled.id));
      await act(async () => {});
      expect(listAllAgentThreadEvents).toHaveBeenCalledTimes(2);

      vi.mocked(getAgentJob).mockResolvedValue(ended as AgentJobApi);
      vi.mocked(listAllAgentThreadEvents).mockResolvedValue([lastWords]);
      await act(async () => {
        receiveJobChange({
          id: settled.id,
          thread_id: 'live-thread',
          revision: 5,
          state: 'succeeded',
          conversation_state: 'idle',
        });
        await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
      });

      expect(listAllAgentThreadEvents).toHaveBeenCalledTimes(3);
      expect(result.current.job?.state).not.toBe('running');
      expect(result.current.events.map((item) => item.payload?.text)).toContain('The last words');
      await act(async () => vi.advanceTimersByTimeAsync(STREAM_RETRY_MAX_MS));
      expect(streamAgentJob).toHaveBeenCalledTimes(1);
    });
  });

  it('reads its turn again when the workspace publication moves', async () => {
    vi.useFakeTimers();
    serve(settled);
    renderHook(() => useAgentJob(settled.id));
    await act(async () => {});

    await act(async () => {
      receiveHint('publication', 'live-thread');
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });

    expect(getAgentJob).toHaveBeenCalledTimes(2);
    expect(getAgentJobThread).toHaveBeenCalledTimes(1);
  });

  it('ignores what changed in other conversations', async () => {
    vi.useFakeTimers();
    serve(settled);
    renderHook(() => useAgentJob(settled.id));
    await act(async () => {});

    await act(async () => {
      receiveThreadChange({ id: 'other-thread', revision: 1, title: 'Elsewhere' });
      receiveJobChange({
        id: 'other-job',
        thread_id: 'other-thread',
        revision: 1,
        state: 'running',
      });
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });

    expect(getAgentJob).toHaveBeenCalledTimes(1);
  });

  it("reads another turn's activity on the slow clock while pushes flow", async () => {
    vi.useFakeTimers();
    // The turn on screen has settled while a later one runs: it used to be
    // read every five seconds to notice that turn ending.
    serve({ ...settled, conversation_state: 'running' });
    act(() => setAgentPushLive(true));
    renderHook(() => useAgentJob(settled.id));
    await act(async () => {});

    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    expect(getAgentJob).toHaveBeenCalledTimes(1);

    await act(async () => vi.advanceTimersByTimeAsync(PUSH_FALLBACK_POLL_MS));
    expect(getAgentJob).toHaveBeenCalledTimes(2);
  });
});

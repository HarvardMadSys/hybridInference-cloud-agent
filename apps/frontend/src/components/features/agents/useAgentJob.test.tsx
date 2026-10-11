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
    expect(vi.mocked(listAllAgentThreadEvents).mock.calls).toEqual([[next.id]]);
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

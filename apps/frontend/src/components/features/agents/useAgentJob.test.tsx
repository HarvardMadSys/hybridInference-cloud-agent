// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

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

import { buildTrajectorySteps, turnContextFromJobs } from './trajectorySteps';
import { useAgentJob } from './useAgentJobs';

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
    vi.mocked(streamAgentJob).mockResolvedValue(undefined);

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
    vi.mocked(streamAgentJob).mockResolvedValue(undefined);

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
    vi.mocked(streamAgentJob).mockImplementation(async (_jobId, options) => {
      stream = options;
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

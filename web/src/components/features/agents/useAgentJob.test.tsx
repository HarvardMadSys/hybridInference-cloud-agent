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

afterEach(cleanup);

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

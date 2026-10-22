// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import {
  getAgentJob,
  getAgentJobGit,
  getAgentJobThread,
  listAllAgentThreadEvents,
  type AgentJobApi,
  type AgentThreadApi,
} from '@/lib/api/agents';
import { useAgentJob } from './useAgentJobs';

vi.mock('@/lib/api/agents', () => ({
  getAgentJob: vi.fn(),
  getAgentJobGit: vi.fn(),
  getAgentJobThread: vi.fn(),
  listAllAgentThreadEvents: vi.fn(),
  streamAgentJob: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const job = (turn: number) =>
  ({
    id: `job-${turn}`,
    thread_id: 'thread',
    turn_no: turn,
    task_prompt: `prompt ${turn}`,
    title: 'History',
    title_source: 'manual',
    runtime: 'codex',
    model: 'model',
    state: 'succeeded',
    current_attempt_id: null,
    repo: 'owner/repo',
  }) as AgentJobApi;
function page(turns: number[], next: string | null): AgentThreadApi {
  return {
    thread_id: 'thread',
    jobs: turns.map(job),
    messages: turns.map((turn) => ({
      id: turn,
      job_id: `job-${turn}`,
      role: 'assistant',
      content: `reply ${turn}`,
      created_at: null,
    })),
    next_cursor: next,
    sync_cursor: 'tail',
  };
}

it('backfills on demand and retains older turns across the real hook reload lifecycle', async () => {
  vi.mocked(getAgentJob).mockResolvedValue(job(2000));
  vi.mocked(getAgentJobGit).mockResolvedValue({ available: false } as Awaited<
    ReturnType<typeof getAgentJobGit>
  >);
  vi.mocked(listAllAgentThreadEvents).mockResolvedValue([]);
  vi.mocked(getAgentJobThread).mockImplementation(async (_id, options) => {
    if (options?.cursor) return page([1997, 1998], 'even-older');
    if (options?.after) return page([1999, 2000], null);
    return page([1999, 2000], 'older');
  });
  const { result } = renderHook(() => useAgentJob('job-2000'));
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.threadJobs).toHaveLength(2);
  expect(getAgentJobThread).toHaveBeenCalledTimes(1);
  act(() => result.current.history?.loadOlder());
  await waitFor(() => expect(result.current.threadJobs).toHaveLength(4));
  expect(result.current.job?.historyTurns?.map((turn) => turn.turnNo)).toEqual([1997, 1998, 1999]);
  act(() => result.current.reload());
  await waitFor(() =>
    expect(getAgentJobThread).toHaveBeenCalledWith('job-2000', { after: 'tail' }),
  );
  await waitFor(() => expect(result.current.loading).toBe(false));
  expect(result.current.threadJobs).toHaveLength(4);
  expect(result.current.history?.hasOlder).toBe(true);
  expect(getAgentJobThread).toHaveBeenCalledTimes(3);
});

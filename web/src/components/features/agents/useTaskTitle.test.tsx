// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
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
describe('automatic task title refresh', () => {
  it.each(['running', 'succeeded'] as const)(
    'shows the generated title for a %s task despite an older thread snapshot',
    async (state) => {
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
    },
  );
  it('bounds polling when generation does not produce a title', async () => {
    const { result } = renderHook(() => useAgentJob(initial.id));
    await act(async () => {});
    await act(async () => vi.advanceTimersByTimeAsync(60_000));
    expect(result.current.job?.title).toBe(initial.title);
    expect(getAgentJob).toHaveBeenCalledTimes(9);
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

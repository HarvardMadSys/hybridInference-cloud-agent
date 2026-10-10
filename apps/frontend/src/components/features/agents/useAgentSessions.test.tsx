// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import {
  getAgentJob,
  getAgentJobGit,
  getAgentJobThread,
  listAllAgentThreadEvents,
  listAgentProjects,
  type AgentJobApi,
} from '@/lib/api/agents';
import { useAgentJob, useAgentProjects } from './useAgentJobs';

vi.mock('@/lib/api/agents', () => ({
  listAgentProjects: vi.fn(),
  getAgentJob: vi.fn(),
  getAgentJobThread: vi.fn(),
  listAllAgentThreadEvents: vi.fn(),
  getAgentJobGit: vi.fn(),
}));

function job(id: string): AgentJobApi {
  return {
    id,
    thread_id: `thread-${id}`,
    task_prompt: id,
    repo: 'owner/repo',
    state: 'succeeded',
  } as AgentJobApi;
}

function pending<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  vi.mocked(getAgentJobThread).mockResolvedValue({
    thread_id: 'thread-first',
    jobs: [],
    messages: [],
  });
  vi.mocked(listAllAgentThreadEvents).mockResolvedValue([]);
  vi.mocked(getAgentJobGit).mockRejectedValue(new Error('No workspace'));
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('session mutations across active history caches', () => {
  it.each([{ title: 'Renamed task' }, { pinnedAt: '2026-09-10T00:00:00Z' }])(
    'refetches the workspace diff after a settled task metadata change: %j',
    async (change) => {
      const settled = job('first');
      vi.mocked(getAgentJob).mockResolvedValue(settled);
      const patch = [
        'diff --git a/example.py b/example.py',
        '--- a/example.py',
        '+++ b/example.py',
        '@@ -1 +1 @@',
        '-before',
        '+after',
      ].join('\n');
      vi.mocked(getAgentJobGit).mockResolvedValue({
        available: true,
        branch: 'agent/first',
        changes: [],
        patch,
        commits: [],
      });
      const { result } = renderHook(() => useAgentJob('first'));
      await waitFor(() => expect(result.current.job?.diffFiles).toEqual(['example.py']));
      const originalDiff = result.current.job?.diffFileDetails;
      act(() => announceAgentSessionsChanged({ threadIds: ['thread-first'], ...change }));
      await waitFor(() => expect(getAgentJob).toHaveBeenCalledTimes(2));
      await waitFor(() => expect(result.current.loading).toBe(false));
      expect(result.current.job?.diffFileDetails).toEqual(originalDiff);
      expect(getAgentJobGit).toHaveBeenCalledTimes(2);
    },
  );

  it('invalidates a deletion before the first detail response establishes the thread ID', async () => {
    const stale = pending<AgentJobApi>();
    vi.mocked(getAgentJob)
      .mockReturnValueOnce(stale.promise)
      .mockRejectedValue(new Error('Session no longer available'));
    const { result } = renderHook(() => useAgentJob('first'));
    act(() => announceAgentSessionsChanged({ threadIds: ['thread-first'], deleted: true }));
    await waitFor(() => expect(result.current.error).toBe('Session no longer available'));
    expect(getAgentJob).toHaveBeenCalledTimes(2);
    await act(async () => stale.resolve(job('first')));
    expect(result.current.job).toBeNull();
    expect(result.current.error).toBe('Session no longer available');
    expect(result.current.loading).toBe(false);
  });

  it.each([true, false])(
    'refreshes an early archive change to %s and ignores the stale response',
    async (archived) => {
      const stale = pending<AgentJobApi>();
      const archivedAt = '2026-09-10T00:00:00Z';
      vi.mocked(getAgentJob)
        .mockReturnValueOnce(stale.promise)
        .mockResolvedValue({ ...job('first'), archived_at: archived ? archivedAt : null });
      const { result } = renderHook(() => useAgentJob('first'));
      act(() => announceAgentSessionsChanged({ threadIds: ['thread-first'], archived }));
      await waitFor(() => expect(result.current.job?.id).toBe('first'));
      await act(async () =>
        stale.resolve({ ...job('first'), archived_at: archived ? null : archivedAt }),
      );
      expect(result.current.job?.archivedAt).toBe(archived ? archivedAt : null);
      expect(result.current.loading).toBe(false);
      act(() => announceAgentSessionsChanged({ threadIds: ['thread-unrelated'], deleted: true }));
      expect(getAgentJob).toHaveBeenCalledTimes(2);
      expect(result.current.job?.id).toBe('first');
    },
  );

  it('refreshes project totals after restoring an older session', async () => {
    vi.mocked(listAgentProjects)
      .mockResolvedValueOnce([
        { repo: 'owner/repo', task_count: 1, active_count: 0, last_activity_at: null },
      ])
      .mockResolvedValue([
        { repo: 'owner/repo', task_count: 2, active_count: 0, last_activity_at: null },
      ]);
    const { result } = renderHook(() => useAgentProjects());
    await waitFor(() => expect(result.current.projects[0]?.task_count).toBe(1));
    act(() => announceAgentSessionsChanged({ threadIds: ['thread-older'], archived: false }));
    await waitFor(() => expect(result.current.projects[0].task_count).toBe(2));
  });
});

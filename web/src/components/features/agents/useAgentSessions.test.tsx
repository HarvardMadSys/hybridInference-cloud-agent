// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { listAgentJobs, listAgentProjects, type AgentJobApi } from '@/lib/api/agents';
import { useAgentJobList, useAgentProjects, useProjectJobs } from './useAgentJobs';

vi.mock('@/lib/api/agents', () => ({ listAgentJobs: vi.fn(), listAgentProjects: vi.fn() }));

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

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('session mutations across active history caches', () => {
  it('refreshes completed history and project totals after restoring an older session', async () => {
    vi.mocked(listAgentJobs)
      .mockResolvedValueOnce([job('recent')])
      .mockResolvedValue([job('recent'), job('older')]);
    vi.mocked(listAgentProjects)
      .mockResolvedValueOnce([
        { repo: 'owner/repo', task_count: 1, active_count: 0, last_activity_at: null },
      ])
      .mockResolvedValue([
        { repo: 'owner/repo', task_count: 2, active_count: 0, last_activity_at: null },
      ]);
    const { result } = renderHook(() => ({
      history: useAgentJobList(),
      projects: useAgentProjects(),
    }));
    await waitFor(() => expect(result.current.history.jobs).toHaveLength(1));
    act(() => announceAgentSessionsChanged({ threadIds: ['thread-older'], archived: false }));
    await waitFor(() => expect(result.current.history.jobs).toHaveLength(2));
    expect(result.current.projects.projects[0].task_count).toBe(2);
  });

  it('keeps an archived row removed when an older global request finishes late', async () => {
    const stale = pending<AgentJobApi[]>();
    vi.mocked(listAgentJobs).mockReturnValueOnce(stale.promise).mockResolvedValue([]);
    const { result } = renderHook(() => useAgentJobList());
    act(() => announceAgentSessionsChanged({ threadIds: ['thread-archived'], archived: true }));
    await waitFor(() => expect(listAgentJobs).toHaveBeenCalledTimes(2));
    await act(async () => stale.resolve([job('archived')]));
    expect(result.current.jobs).toEqual([]);
    expect(result.current.loading).toBe(false);
  });

  it('invalidates cached project pages and ignores their obsolete in-flight response', async () => {
    const stale = pending<AgentJobApi[]>();
    vi.mocked(listAgentJobs)
      .mockResolvedValueOnce([job('old')])
      .mockReturnValueOnce(stale.promise)
      .mockResolvedValue([job('restored')]);
    const { result } = renderHook(() => useProjectJobs());
    act(() => result.current.load('owner/repo'));
    await waitFor(() => expect(result.current.jobsByRepo.get('owner/repo')?.[0].id).toBe('old'));
    act(() => result.current.load('owner/repo'));
    act(() => announceAgentSessionsChanged({ threadIds: ['thread-old'], deleted: true }));
    await waitFor(() =>
      expect(result.current.jobsByRepo.get('owner/repo')?.[0].id).toBe('restored'),
    );
    await act(async () => stale.resolve([job('old')]));
    expect(result.current.jobsByRepo.get('owner/repo')?.map((row) => row.id)).toEqual(['restored']);
    expect(result.current.loadingRepos.size).toBe(0);
  });
});

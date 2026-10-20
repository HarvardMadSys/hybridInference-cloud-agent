// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { type MockInstance, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PUSH_FALLBACK_POLL_MS, resetAgentPush, setAgentPushLive } from './agentPush';
import { useSidebarRefresh } from './useSidebarRefresh';

const projects = vi.hoisted(() => ({
  activeCount: 0,
  reload: vi.fn(),
}));

vi.mock('./useAgentJobs', () => ({
  useAgentProjects: () => ({
    projects: [
      {
        repo: 'owner/repo',
        task_count: 1,
        active_count: projects.activeCount,
        last_activity_at: null,
      },
    ],
    loading: false,
    error: null,
    reload: projects.reload,
  }),
}));

let visibility: MockInstance<() => DocumentVisibilityState>;

beforeEach(() => {
  vi.useFakeTimers();
  visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
  projects.activeCount = 0;
});

afterEach(() => {
  cleanup();
  resetAgentPush();
  vi.useRealTimers();
  vi.restoreAllMocks();
  projects.reload.mockReset();
});

describe('sidebar refresh', () => {
  it('reads again every ten seconds while a project has a turn running', () => {
    projects.activeCount = 1;
    const { result } = renderHook(() => useSidebarRefresh('alice', true));

    act(() => void vi.advanceTimersByTime(10_000));
    expect(result.current.refresh).toBe(1);
    expect(projects.reload).toHaveBeenCalledTimes(1);

    act(() => void vi.advanceTimersByTime(10_000));
    expect(result.current.refresh).toBe(2);
  });

  it('polls only as a fallback while the change stream is live', () => {
    projects.activeCount = 1;
    setAgentPushLive(true);
    const { result } = renderHook(() => useSidebarRefresh('alice', true));

    act(() => void vi.advanceTimersByTime(PUSH_FALLBACK_POLL_MS - 1));
    expect(result.current.refresh).toBe(0);
    act(() => void vi.advanceTimersByTime(1));
    expect(result.current.refresh).toBe(1);
  });

  it('does not poll while nothing is running', () => {
    const { result } = renderHook(() => useSidebarRefresh('alice', true));

    act(() => void vi.advanceTimersByTime(60_000));
    expect(result.current.refresh).toBe(0);
    expect(projects.reload).not.toHaveBeenCalled();
  });

  it('does not poll in a sidebar that is not the active one', () => {
    projects.activeCount = 1;
    const { result } = renderHook(() => useSidebarRefresh('alice', false));

    act(() => void vi.advanceTimersByTime(60_000));
    expect(result.current.refresh).toBe(0);
    expect(projects.reload).not.toHaveBeenCalled();
  });

  it('holds a poll while the tab is hidden and reads once when it is visible again', () => {
    projects.activeCount = 1;
    const { result } = renderHook(() => useSidebarRefresh('alice', true));
    visibility.mockReturnValue('hidden');

    act(() => void vi.advanceTimersByTime(30_000));
    expect(result.current.refresh).toBe(0);

    visibility.mockReturnValue('visible');
    act(() => void document.dispatchEvent(new Event('visibilitychange')));
    expect(result.current.refresh).toBe(1);
    expect(projects.reload).toHaveBeenCalledTimes(1);
  });

  it('reads now on reload, counting each read', () => {
    const { result } = renderHook(() => useSidebarRefresh('alice', true));

    act(() => result.current.reload());
    act(() => result.current.reload());

    expect(result.current.refresh).toBe(2);
    expect(result.current.refreshRef.current).toBe(2);
  });
});

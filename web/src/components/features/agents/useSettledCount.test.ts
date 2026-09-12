// @vitest-environment jsdom

import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useSettledCount } from './WorkspaceFiles';

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('useSettledCount', () => {
  it('collapses a burst of additions into one reload', () => {
    // The diff count is part of the workspace refresh key, and a refresh
    // reloads the root, every expanded directory and the open file — each its
    // own round trip. The agent adds files one at a time, so without this a
    // turn touching twenty files with four directories open issued around a
    // hundred and twenty requests.
    const hook = renderHook(({ count }) => useSettledCount(count, 1500), {
      initialProps: { count: 0 },
    });

    for (const count of [1, 2, 3, 4, 5]) {
      hook.rerender({ count });
      act(() => void vi.advanceTimersByTime(200));
    }

    expect(hook.result.current).toBe(0);

    act(() => void vi.advanceTimersByTime(1500));
    expect(hook.result.current).toBe(5);
  });

  it('settles once the writes stop', () => {
    const hook = renderHook(({ count }) => useSettledCount(count, 1500), {
      initialProps: { count: 0 },
    });

    hook.rerender({ count: 1 });
    act(() => void vi.advanceTimersByTime(1499));
    expect(hook.result.current).toBe(0);

    act(() => void vi.advanceTimersByTime(1));
    expect(hook.result.current).toBe(1);
  });

  it('stops scheduling once it has caught up', () => {
    const hook = renderHook(({ count }) => useSettledCount(count, 1500), {
      initialProps: { count: 3 },
    });

    expect(hook.result.current).toBe(3);
    act(() => void vi.advanceTimersByTime(5000));
    expect(hook.result.current).toBe(3);
  });
});

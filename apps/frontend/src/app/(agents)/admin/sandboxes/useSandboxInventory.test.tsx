// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { getAdminSandboxes } from '@/lib/api/adminSandboxes';

import { useSandboxInventory } from './useSandboxInventory';

vi.mock('@/lib/api/adminSandboxes', () => ({ getAdminSandboxes: vi.fn() }));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it('does not poll a hidden tab and resumes with a delta rather than a full walk', async () => {
  vi.useFakeTimers();
  let hidden = false;
  vi.spyOn(document, 'hidden', 'get').mockImplementation(() => hidden);
  vi.mocked(getAdminSandboxes).mockResolvedValue({
    mode: 'delta',
    sandboxes: [],
    total_count: null,
    observed_at: '2026-10-22T00:00:00Z',
    next_cursor: null,
    change_cursor: 'latest',
  });
  const options = {
    revision: 0,
    stateFilter: 'running' as const,
    includeArchived: false,
    denied: false,
    denyAccess: vi.fn(),
    autoRefreshRef: { current: true },
    batchBusyRef: { current: false },
    setLoading: vi.fn(),
    setError: vi.fn(),
    setSnapshot: vi.fn(),
  };
  renderHook(() => useSandboxInventory(options));
  await act(async () => {
    await Promise.resolve();
  });
  expect(getAdminSandboxes).toHaveBeenCalledTimes(1);
  hidden = true;
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  await act(async () => {
    await vi.advanceTimersByTimeAsync(300_000);
  });
  expect(getAdminSandboxes).toHaveBeenCalledTimes(1);
  hidden = false;
  await act(async () => {
    document.dispatchEvent(new Event('visibilitychange'));
  });
  expect(getAdminSandboxes).toHaveBeenCalledTimes(2);
  expect(getAdminSandboxes).toHaveBeenLastCalledWith(
    { limit: 200, sync: true, cursor: 'latest' },
    expect.any(AbortSignal),
  );
});

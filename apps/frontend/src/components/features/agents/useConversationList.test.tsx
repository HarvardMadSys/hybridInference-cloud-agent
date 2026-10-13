// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AgentConversationApi, AgentJobApi } from '@/lib/api/agents';
import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
const api = vi.hoisted(() => ({ list: vi.fn() }));
vi.mock('@/lib/api/agents', () => ({ listAgentConversations: api.list }));
import { useConversationList } from './useConversationList';
function row(id: string, title = id): AgentConversationApi {
  return {
    job: {
      id,
      thread_id: id,
      turn_no: 1,
      repo: 'owner/repo',
      task_prompt: 'latest follow-up',
      state: 'succeeded',
    } as AgentJobApi,
    title,
    archived: false,
    last_activity_at: '2026-09-01T10:00:00Z',
  };
}
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});
describe('conversation navigation queries', () => {
  it('shows progress for initial loads and pagination while refreshing loaded lists quietly', async () => {
    let resolvePage!: (page: {
      conversations: AgentConversationApi[];
      next_cursor: string | null;
    }) => void;
    api.list.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePage = resolve;
        }),
    );
    const { result, rerender } = renderHook((options) => useConversationList(options), {
      initialProps: { q: '', refresh: 0 },
    });
    expect(result.current.loading).toBe(true);
    expect(result.current.showLoading).toBe(true);
    await act(async () => resolvePage({ conversations: [row('first')], next_cursor: 'next' }));

    act(() => result.current.loadMore());
    expect(result.current.showLoading).toBe(true);
    await act(async () =>
      resolvePage({ conversations: [row('second')], next_cursor: 'after-second' }),
    );
    expect(result.current.rows.map((row) => row.key)).toEqual(['first', 'second']);

    rerender({ q: '', refresh: 1 });
    expect(result.current.loading).toBe(true);
    expect(result.current.showLoading).toBe(false);
    expect(result.current.rows.map((row) => row.key)).toEqual(['first', 'second']);
    // A pagination click cannot start a competing read during a background refresh.
    const requests = api.list.mock.calls.length;
    act(() => result.current.loadMore());
    expect(api.list).toHaveBeenCalledTimes(requests);
    await act(async () => resolvePage({ conversations: [], next_cursor: null }));

    rerender({ q: '', refresh: 2 });
    expect(result.current.loading).toBe(true);
    expect(result.current.showLoading).toBe(false);
    expect(result.current.rows).toEqual([]);
    await act(async () => resolvePage({ conversations: [], next_cursor: null }));

    rerender({ q: 'new search', refresh: 2 });
    expect(result.current.loading).toBe(true);
    expect(result.current.showLoading).toBe(true);
    await act(async () => resolvePage({ conversations: [row('new')], next_cursor: null }));
    expect(result.current.loading).toBe(false);
    expect(result.current.showLoading).toBe(false);
  });

  it('isolates pin filters and stamps only the refresh that loaded the rows', async () => {
    api.list.mockResolvedValueOnce({
      conversations: [row('regular')],
      next_cursor: 'regular-next',
    });
    const { result, rerender } = renderHook(
      ({ pinned, refresh, enabled }) => useConversationList({ pinned, refresh, enabled }),
      { initialProps: { pinned: false, refresh: 0, enabled: true } },
    );
    await waitFor(() => expect(result.current.loadedRefresh).toBe(0));
    rerender({ pinned: false, refresh: 1, enabled: false });
    expect(result.current.loadedRefresh).toBe(0);
    let resolveOld!: (value: unknown) => void;
    api.list.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    rerender({ pinned: false, refresh: 1, enabled: true });
    expect(result.current.loadedRefresh).toBe(0);
    api.list.mockResolvedValueOnce({ conversations: [row('pinned')], next_cursor: null });
    rerender({ pinned: true, refresh: 2, enabled: true });
    expect(result.current.rows).toEqual([]);
    expect(result.current.loadedRefresh).toBeNull();
    await waitFor(() => expect(result.current.loadedRefresh).toBe(2));
    await act(async () => resolveOld({ conversations: [row('stale')], next_cursor: 'stale-next' }));
    expect(result.current.rows.map((row) => row.key)).toEqual(['pinned']);
    expect(result.current.cursor).toBeNull();
    expect(result.current.loadedRefresh).toBe(2);
    expect(api.list).toHaveBeenLastCalledWith(
      expect.objectContaining({ pinned: true, cursor: undefined }),
      expect.any(AbortSignal),
    );
  });

  it('invalidates an in-flight search when a session is deleted', async () => {
    let resolveOld!: (value: unknown) => void;
    api.list.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    api.list.mockResolvedValue({ conversations: [], next_cursor: null });
    const { result } = renderHook(() =>
      useConversationList({ includeArchived: true, q: 'deleted' }),
    );
    const signal = api.list.mock.calls[0][1] as AbortSignal;
    act(() => announceAgentSessionsChanged({ threadIds: ['deleted'], deleted: true }));
    expect(signal.aborted).toBe(true);
    await waitFor(() => expect(api.list).toHaveBeenCalledTimes(2));
    await act(async () =>
      resolveOld({ conversations: [{ ...row('deleted'), archived: true }], next_cursor: null }),
    );
    expect(result.current.rows).toEqual([]);
    expect(result.current.loading).toBe(false);
  });

  it('restores an older conversation into the active list after unarchive', async () => {
    api.list.mockResolvedValueOnce({ conversations: [], next_cursor: null });
    const { result } = renderHook(() => useConversationList());
    await waitFor(() => expect(result.current.loading).toBe(false));
    api.list.mockResolvedValue({ conversations: [row('restored')], next_cursor: null });
    act(() => announceAgentSessionsChanged({ threadIds: ['restored'], archived: false }));
    await waitFor(() => expect(result.current.rows[0]?.key).toBe('restored'));
  });

  it('refreshes four loaded pages in one bounded request and retains the next cursor', async () => {
    const all = Array.from({ length: 260 }, (_, index) => row(String(index)));
    api.list.mockImplementation(async ({ cursor, limit }: { cursor?: string; limit: number }) => {
      const start = Number(cursor ?? 0);
      return {
        conversations: all.slice(start, start + limit),
        next_cursor: start + limit < all.length ? String(start + limit) : null,
      };
    });
    const { result, rerender } = renderHook(({ refresh }) => useConversationList({ refresh }), {
      initialProps: { refresh: 0 },
    });
    await waitFor(() => expect(result.current.rows).toHaveLength(50));
    for (const count of [100, 150, 200]) {
      act(() => result.current.loadMore());
      await waitFor(() => expect(result.current.rows).toHaveLength(count));
    }
    all[160] = row('160', 'Updated historical title');
    api.list.mockClear();
    rerender({ refresh: 1 });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(api.list).toHaveBeenCalledTimes(1);
    expect(api.list).toHaveBeenCalledWith(
      expect.objectContaining({ limit: 200, cursor: undefined }),
      expect.any(AbortSignal),
    );
    expect(result.current.rows).toHaveLength(200);
    expect(result.current.rows[160].job.title).toBe('Updated historical title');
    expect(result.current.cursor).toBe('200');
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.rows).toHaveLength(250));
    expect(api.list).toHaveBeenLastCalledWith(
      expect.objectContaining({ limit: 50, cursor: '200' }),
      expect.any(AbortSignal),
    );
    expect(new Set(result.current.rows.map((item) => item.key)).size).toBe(250);
    api.list.mockClear();
    rerender({ refresh: 2 });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(api.list.mock.calls.map(([options]) => [options.limit, options.cursor])).toEqual([
      [200, undefined],
      [50, '200'],
    ]);
    expect(result.current.rows).toHaveLength(250);
    expect(result.current.cursor).toBe('250');
  });

  it('uses server titles, deduplicates pages, and refreshes every already loaded page', async () => {
    api.list.mockImplementation(async ({ cursor }: { cursor?: string }) =>
      cursor
        ? { conversations: [row('a'), row('b')], next_cursor: null }
        : { conversations: [row('a', 'Canonical title')], next_cursor: 'page-2' },
    );
    const { result, rerender } = renderHook(({ refresh }) => useConversationList({ refresh }), {
      initialProps: { refresh: 0 },
    });
    await waitFor(() => expect(result.current.rows).toHaveLength(1));
    expect(result.current.rows[0].job.title).toBe('Canonical title');
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.rows).toHaveLength(2));
    api.list.mockClear();
    rerender({ refresh: 1 });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.rows.map((row) => row.key)).toEqual(['a', 'b']);
    expect(api.list).toHaveBeenCalledTimes(2);
    expect(api.list).toHaveBeenLastCalledWith(
      expect.objectContaining({ cursor: 'page-2' }),
      expect.any(AbortSignal),
    );
  });
  it('ignores late responses after changing query, archive scope, and account', async () => {
    let resolveOld!: (value: unknown) => void;
    api.list.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    api.list.mockResolvedValue({ conversations: [row('new')], next_cursor: null });
    const { result, rerender } = renderHook(
      ({ q, userId, includeArchived }) =>
        useConversationList({ q, userId, includeArchived, repo: 'owner/repo' }),
      { initialProps: { q: 'old', userId: 'alice', includeArchived: false } },
    );
    const oldSignal = api.list.mock.calls[0][1] as AbortSignal;
    rerender({ q: 'new', userId: 'bob', includeArchived: true });
    expect(oldSignal.aborted).toBe(true);
    await waitFor(() => expect(result.current.rows[0]?.key).toBe('new'));
    await act(async () => resolveOld({ conversations: [row('old')], next_cursor: 'obsolete' }));
    expect(result.current.rows.map((row) => row.key)).toEqual(['new']);
    expect(result.current.cursor).toBeNull();
    expect(api.list).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: 'new', repo: 'owner/repo', includeArchived: true }),
      expect.any(AbortSignal),
    );
  });

  it('aborts pagination on unmount without reporting its cancellation as an error', async () => {
    api.list.mockResolvedValueOnce({ conversations: [row('first')], next_cursor: 'next' });
    let signal!: AbortSignal;
    let rejectPage!: (cause: unknown) => void;
    api.list.mockImplementationOnce(
      (_options, requestSignal: AbortSignal) =>
        new Promise((_resolve, reject) => {
          signal = requestSignal;
          rejectPage = reject;
        }),
    );
    const { result, unmount } = renderHook(() => useConversationList());
    await waitFor(() => expect(result.current.cursor).toBe('next'));
    act(() => result.current.loadMore());
    expect(signal.aborted).toBe(false);
    unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => rejectPage(signal.reason));
    expect(result.current.error).toBeNull();
    expect(api.list).toHaveBeenCalledTimes(2);
  });

  it('aborts a disabled list and waits for it to be enabled before loading more', async () => {
    api.list.mockResolvedValueOnce({ conversations: [row('first')], next_cursor: 'next' });
    api.list.mockImplementationOnce(
      (_options, signal: AbortSignal) =>
        new Promise((_resolve, reject) => {
          signal.addEventListener('abort', () => reject(signal.reason), { once: true });
        }),
    );
    const { result, rerender } = renderHook(({ enabled }) => useConversationList({ enabled }), {
      initialProps: { enabled: true },
    });
    await waitFor(() => expect(result.current.cursor).toBe('next'));
    act(() => result.current.loadMore());
    const pageSignal = api.list.mock.calls[1][1] as AbortSignal;
    rerender({ enabled: false });
    expect(pageSignal.aborted).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeNull();
    act(() => result.current.loadMore());
    expect(api.list).toHaveBeenCalledTimes(2);
    expect(result.current.rows.map((row) => row.key)).toEqual(['first']);
    api.list.mockResolvedValue({ conversations: [row('updated')], next_cursor: null });
    rerender({ enabled: true });
    await waitFor(() => expect(result.current.rows[0]?.key).toBe('updated'));
  });

  it('stops an obsolete multi-page refresh before fetching another page', async () => {
    const all = Array.from({ length: 250 }, (_, index) => row(String(index)));
    api.list.mockImplementation(async ({ cursor, limit }: { cursor?: string; limit: number }) => {
      const start = Number(cursor ?? 0);
      return {
        conversations: all.slice(start, start + limit),
        next_cursor: start + limit < all.length ? String(start + limit) : null,
      };
    });
    const { result, rerender } = renderHook(
      ({ q, refresh }) => useConversationList({ q, refresh }),
      { initialProps: { q: '', refresh: 0 } },
    );
    await waitFor(() => expect(result.current.rows).toHaveLength(50));
    for (const count of [100, 150, 200, 250]) {
      act(() => result.current.loadMore());
      await waitFor(() => expect(result.current.rows).toHaveLength(count));
    }
    api.list.mockClear();
    let resolveRefresh!: (value: unknown) => void;
    api.list.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveRefresh = resolve;
        }),
    );
    rerender({ q: '', refresh: 1 });
    const refreshSignal = api.list.mock.calls[0][1] as AbortSignal;
    api.list.mockResolvedValue({ conversations: [row('new search')], next_cursor: null });
    rerender({ q: 'new search', refresh: 1 });
    expect(refreshSignal.aborted).toBe(true);
    await waitFor(() => expect(result.current.rows[0]?.key).toBe('new search'));
    await act(async () => resolveRefresh({ conversations: all.slice(0, 200), next_cursor: '200' }));
    expect(api.list).toHaveBeenCalledTimes(2);
    expect(result.current.rows.map((row) => row.key)).toEqual(['new search']);
    expect(result.current.loading).toBe(false);
  });

  it('keeps earlier pages after a failed next page and supports retrying it', async () => {
    api.list.mockResolvedValueOnce({ conversations: [row('first')], next_cursor: 'next' });
    api.list.mockRejectedValueOnce(new Error('Offline'));
    const { result } = renderHook(() => useConversationList());
    await waitFor(() => expect(result.current.cursor).toBe('next'));
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.error).toBe('Offline'));
    expect(result.current.rows).toHaveLength(1);
    api.list.mockResolvedValueOnce({ conversations: [row('second')], next_cursor: null });
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.rows).toHaveLength(2));
    expect(result.current.error).toBeNull();
  });
});

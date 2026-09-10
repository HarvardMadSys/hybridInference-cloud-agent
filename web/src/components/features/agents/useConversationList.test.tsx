// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { AgentConversationApi, AgentJobApi } from '@/lib/api/agents';
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
    );
    expect(result.current.rows).toHaveLength(200);
    expect(result.current.rows[160].job.title).toBe('Updated historical title');
    expect(result.current.cursor).toBe('200');
    act(() => result.current.loadMore());
    await waitFor(() => expect(result.current.rows).toHaveLength(250));
    expect(api.list).toHaveBeenLastCalledWith(
      expect.objectContaining({ limit: 50, cursor: '200' }),
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
    expect(api.list).toHaveBeenLastCalledWith(expect.objectContaining({ cursor: 'page-2' }));
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
    rerender({ q: 'new', userId: 'bob', includeArchived: true });
    await waitFor(() => expect(result.current.rows[0]?.key).toBe('new'));
    await act(async () => resolveOld({ conversations: [row('old')], next_cursor: 'obsolete' }));
    expect(result.current.rows.map((row) => row.key)).toEqual(['new']);
    expect(result.current.cursor).toBeNull();
    expect(api.list).toHaveBeenLastCalledWith(
      expect.objectContaining({ q: 'new', repo: 'owner/repo', includeArchived: true }),
    );
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

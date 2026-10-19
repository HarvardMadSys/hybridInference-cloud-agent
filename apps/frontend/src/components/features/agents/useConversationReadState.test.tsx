// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentJob } from './types';
import type { SidebarConversation } from './useConversationList';
import {
  listAgentReadReceipts,
  saveAgentReadReceipts,
  type AgentReadReceiptApi,
  type AgentReadReceiptsResponse,
} from '@/lib/api/agents/readReceipts';
import {
  conversationReadStorageKey,
  markConversationRead,
  useConversationReadState,
} from './useConversationReadState';

vi.mock('@/lib/api/agents/readReceipts', () => ({
  listAgentReadReceipts: vi.fn(),
  saveAgentReadReceipts: vi.fn(),
}));
const list = vi.mocked(listAgentReadReceipts);
const save = vi.mocked(saveAgentReadReceipts);
const cloudReceipt: AgentReadReceiptApi = {
  thread_id: 'thread-1',
  job_id: 'job-1',
  turn_no: 1,
  attempt_id: 1,
};
const page = (
  receipts: AgentReadReceiptApi[] = [],
  userId = 'alice',
): AgentReadReceiptsResponse => ({
  user_id: userId,
  receipts,
  next_cursor: null,
  sync_cursor: null,
});

function row(overrides: Partial<AgentJob> = {}, key = 'thread-1'): SidebarConversation {
  return {
    key,
    job: {
      id: 'job-1',
      state: 'done',
      currentAttemptId: 1,
      turnNo: 1,
      finishedAt: '2026-09-01T10:00:00Z',
      ...overrides,
    } as AgentJob,
    jobIds: ['job-1'],
    archived: false,
    lastActivityAt: '2026-09-01T10:00:00Z',
  };
}
beforeEach(() => {
  localStorage.clear();
  // Local behavior remains testable while the first cloud read is in flight.
  list.mockReset().mockImplementation(() => new Promise(() => {}));
  save.mockReset().mockResolvedValue(page());
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
describe('conversation completion read receipts', () => {
  it.each(['running', 'queued'] as const)(
    'does not mark a cancelled head read while its conversation is %s',
    (conversationState) => {
      const { result } = renderHook(() => useConversationReadState('alice'));
      const active = row({ state: 'cancelled', conversationState });
      expect(result.current.isUnread(active)).toBe(false);
      act(() => result.current.markRead(active));
      expect(localStorage.getItem(conversationReadStorageKey('alice'))).toBeNull();
      const idle = row({ state: 'cancelled', conversationState: 'idle' });
      expect(result.current.isUnread(idle)).toBe(true);
      act(() => result.current.markRead(idle));
      expect(result.current.isUnread(idle)).toBe(false);
    },
  );
  it('leaves historical completions unread and never writes when rows are merely polled', () => {
    const write = vi.spyOn(Storage.prototype, 'setItem');
    const { result, rerender } = renderHook(() => useConversationReadState('alice'));
    for (const state of ['done', 'needs_review', 'failed', 'cancelled'] as const) {
      expect(result.current.isUnread(row({ state }))).toBe(true);
    }
    for (const state of ['running', 'queued'] as const) {
      expect(result.current.isUnread(row({ state }))).toBe(false);
      act(() => result.current.markRead(row({ state })));
    }
    rerender();
    expect(result.current.isUnread(row())).toBe(true);
    expect(write).not.toHaveBeenCalled();
  });
  it('writes once per opened completion and ignores pin, archive, title, and activity changes', () => {
    const write = vi.spyOn(Storage.prototype, 'setItem');
    const { result } = renderHook(() => useConversationReadState('alice'));
    const completed = row();
    act(() => result.current.markRead(completed));
    expect(result.current.isUnread(completed)).toBe(false);
    const updated = {
      ...completed,
      archived: true,
      lastActivityAt: '2026-09-05T00:00:00Z',
      job: { ...completed.job, title: 'Renamed', pinnedAt: '2026-09-04T00:00:00Z' },
    };
    expect(result.current.isUnread(updated)).toBe(false);
    act(() => result.current.markRead(updated));
    expect(write).toHaveBeenCalledOnce();
    const saved = JSON.parse(localStorage.getItem(conversationReadStorageKey('alice'))!);
    expect(Object.keys(saved.receipts)).toEqual(['thread-1']);
  });
  it('marks subsequent turns and retry completions unread again', () => {
    const { result } = renderHook(() => useConversationReadState('alice'));
    act(() => result.current.markRead(row()));
    const nextTurn = row({ id: 'job-2' });
    expect(result.current.isUnread(nextTurn)).toBe(true);
    act(() => result.current.markRead(nextTurn));
    expect(result.current.isUnread(nextTurn)).toBe(false);
    const retry = row({ id: 'job-2', currentAttemptId: 2, finishedAt: '2026-09-01T11:00:00Z' });
    expect(result.current.isUnread(retry)).toBe(true);
    act(() => result.current.markRead(retry));
    expect(result.current.isUnread(retry)).toBe(false);
  });
  it('matches list and detail completion despite missing or different execution timestamps', () => {
    const write = vi.spyOn(Storage.prototype, 'setItem');
    const { result } = renderHook(() => useConversationReadState('alice'));
    act(() => result.current.markRead(row({ finishedAt: undefined })));
    expect(result.current.isUnread(row({ finishedAt: null }))).toBe(false);
    expect(result.current.isUnread(row({ finishedAt: '' }))).toBe(false);
    expect(result.current.isUnread(row({ finishedAt: '2026-09-01T10:00:00Z' }))).toBe(false);
    expect(result.current.isUnread(row({ currentAttemptId: 2 }))).toBe(true);
    act(() => result.current.markRead(row({ finishedAt: null })));
    expect(write).toHaveBeenCalledOnce();
    expect(result.current.isUnread(row({ id: 'next', finishedAt: null }))).toBe(true);
  });
  it('restores read receipts on remount and isolates accounts and stale callbacks', () => {
    const first = renderHook(({ id }) => useConversationReadState(id), {
      initialProps: { id: 'alice' },
    });
    act(() => first.result.current.markRead(row()));
    const aliceMarkRead = first.result.current.markRead;
    first.rerender({ id: 'bob' });
    expect(first.result.current.isUnread(row())).toBe(true);
    act(() => aliceMarkRead(row({ id: 'new' })));
    expect(localStorage.getItem(conversationReadStorageKey('bob'))).toBeNull();
    expect(first.result.current.isUnread(row())).toBe(true);
    first.unmount();
    const second = renderHook(() => useConversationReadState('alice'));
    expect(second.result.current.isUnread(row())).toBe(false);
    expect(second.result.current.isUnread(row({ id: 'new' }))).toBe(true);
  });
  it('merges other-tab reads before writing and responds to owner-scoped storage events', () => {
    const { result } = renderHook(() => useConversationReadState('alice'));
    const another = row({ id: 'job-2' }, 'thread-2');
    const remote = JSON.stringify({
      version: 2,
      receipts: { 'thread-2': { jobId: 'job-2', attemptId: 1, turnNo: 1 } },
    });
    localStorage.setItem(conversationReadStorageKey('alice'), remote);
    act(() => result.current.markRead(row()));
    expect(result.current.isUnread(another)).toBe(false);
    expect(
      Object.keys(JSON.parse(localStorage.getItem(conversationReadStorageKey('alice'))!).receipts),
    ).toEqual(['thread-2', 'thread-1']);
    act(() =>
      window.dispatchEvent(
        new StorageEvent('storage', { key: conversationReadStorageKey('bob'), newValue: null }),
      ),
    );
    expect(result.current.isUnread(row())).toBe(false);
    act(() =>
      window.dispatchEvent(
        new StorageEvent('storage', { key: conversationReadStorageKey('alice'), newValue: remote }),
      ),
    );
    expect(result.current.isUnread(another)).toBe(false);
    // Another tab's stale cache must no longer erase a newer local/cloud read.
    expect(result.current.isUnread(row())).toBe(false);
  });
  it('tolerates malformed and blocked storage while clearing the dot in memory', () => {
    localStorage.setItem(conversationReadStorageKey('alice'), '{broken');
    const first = renderHook(() => useConversationReadState('alice'));
    expect(first.result.current.isUnread(row())).toBe(true);
    first.unmount();
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    const write = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    const { result } = renderHook(() => useConversationReadState('alice'));
    act(() => result.current.markRead(row()));
    expect(result.current.isUnread(row())).toBe(false);
    act(() => result.current.markRead(row()));
    expect(write).toHaveBeenCalledOnce();
  });
  it('does not store anonymous read receipts under a shared account key', () => {
    const write = vi.spyOn(Storage.prototype, 'setItem');
    const { result } = renderHook(() => useConversationReadState());
    act(() => result.current.markRead(row()));
    expect(result.current.isUnread(row())).toBe(false);
    expect(write).not.toHaveBeenCalled();
    expect(list).not.toHaveBeenCalled();
    expect(save).not.toHaveBeenCalled();
  });
  it('marks detail completions without a mounted row and synchronizes the same tab', () => {
    markConversationRead(row(), 'alice');
    const { result } = renderHook(() => useConversationReadState('alice'));
    expect(result.current.isUnread(row())).toBe(false);
    const next = row({ id: 'job-2', turnNo: 2, currentAttemptId: 2 });
    expect(result.current.isUnread(next)).toBe(true);
    act(() => markConversationRead(next, 'alice'));
    expect(result.current.isUnread(next)).toBe(false);
    const write = vi.spyOn(Storage.prototype, 'setItem');
    act(() => markConversationRead(next, 'alice'));
    expect(write).not.toHaveBeenCalled();
  });
  it('does not downgrade a newer read turn or attempt when revisiting old detail', () => {
    const { result } = renderHook(() => useConversationReadState('alice'));
    const latest = row({ id: 'job-3', turnNo: 3, currentAttemptId: 30 });
    act(() => result.current.markRead(latest));
    const write = vi.spyOn(Storage.prototype, 'setItem');
    act(() => markConversationRead(row({ id: 'job-2', turnNo: 2, currentAttemptId: 20 }), 'alice'));
    act(() => result.current.markRead(row({ id: 'job-3', turnNo: 3, currentAttemptId: 29 })));
    expect(result.current.isUnread(latest)).toBe(false);
    expect(result.current.isUnread(row({ id: 'job-2', turnNo: 2, currentAttemptId: 20 }))).toBe(
      false,
    );
    expect(write).not.toHaveBeenCalled();
  });

  it('keeps executed progress when a stale attemptless cancellation arrives', () => {
    const { result } = renderHook(() => useConversationReadState('alice'));
    act(() => result.current.markRead(row()));
    act(() => markConversationRead(row({ state: 'cancelled', currentAttemptId: null }), 'alice'));
    expect(result.current.isUnread(row())).toBe(false);
    expect(result.current.isUnread(row({ currentAttemptId: 2 }))).toBe(true);
  });

  it('restores the cloud receipt in a fresh browser and leaves new results unread', async () => {
    let saved: AgentReadReceiptApi[] = [];
    list.mockImplementation(async () => page(saved));
    save.mockImplementation(async () => {
      saved = [cloudReceipt];
      return page(saved);
    });
    const first = renderHook(() => useConversationReadState('alice'));
    await waitFor(() => expect(first.result.current.ready).toBe(true));
    act(() => first.result.current.markRead(row()));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    first.unmount();
    localStorage.clear();

    const second = renderHook(() => useConversationReadState('alice'));
    expect(second.result.current.ready).toBe(false);
    await waitFor(() => expect(second.result.current.ready).toBe(true));
    expect(second.result.current.isUnread(row())).toBe(false);
    expect(second.result.current.isUnread(row({ currentAttemptId: 2 }))).toBe(true);
    expect(second.result.current.isUnread(row({ id: 'job-2', turnNo: 2 }))).toBe(true);
    expect(save).toHaveBeenCalledOnce();
  });

  it('imports all legacy receipts in bounded batches, including history outside loaded rows', async () => {
    const receipts = Object.fromEntries(
      Array.from({ length: 205 }, (_, index) => [
        `thread-${index}`,
        { jobId: `job-${index}`, attemptId: index + 1, turnNo: 1 },
      ]),
    );
    localStorage.setItem(
      conversationReadStorageKey('alice'),
      JSON.stringify({ version: 2, receipts }),
    );
    list.mockResolvedValue(page());
    const first = renderHook(() => useConversationReadState('alice'));
    await waitFor(() => expect(first.result.current.ready).toBe(true));
    expect(save.mock.calls.map(([batch]) => batch.length)).toEqual([200, 5]);
    expect(save.mock.calls.flatMap(([batch]) => batch).map((receipt) => receipt.job_id)).toEqual(
      Object.values(receipts).map((receipt) => receipt.jobId),
    );
    // Rechecking in this tab does not keep re-uploading obsolete records.
    act(() => window.dispatchEvent(new Event('focus')));
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    expect(save).toHaveBeenCalledTimes(2);
  });

  it('shows confirmed rows while later cloud pages are still loading', async () => {
    let finish!: (value: AgentReadReceiptsResponse) => void;
    list.mockResolvedValueOnce({ ...page([cloudReceipt]), next_cursor: '1', sync_cursor: '1' });
    list.mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)));
    const { result } = renderHook(() => useConversationReadState('alice'));
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2));
    expect(result.current.ready).toBe(false);
    expect(result.current.isReady(row({ currentAttemptId: 2 }))).toBe(true);
    expect(result.current.isUnread(row({ currentAttemptId: 2 }))).toBe(true);
    expect(result.current.isReady(row({ id: 'job-2' }, 'thread-2'))).toBe(false);
    expect(list.mock.calls[1][0]).toBe('1');
    await act(async () =>
      finish(page([{ ...cloudReceipt, thread_id: 'thread-2', job_id: 'job-2' }])),
    );
    expect(result.current.ready).toBe(true);
    expect(result.current.isUnread(row())).toBe(false);
    expect(result.current.isUnread(row({ id: 'job-2' }, 'thread-2'))).toBe(false);
    expect(save).not.toHaveBeenCalled();
  });

  it('retries an offline write on reconnect without losing a read', async () => {
    list.mockResolvedValue(page());
    save.mockRejectedValueOnce(new Error('offline')).mockResolvedValue(page([cloudReceipt]));
    const { result } = renderHook(() => useConversationReadState('alice'));
    await waitFor(() => expect(result.current.ready).toBe(true));
    await act(async () => result.current.markRead(row()));
    expect(save).toHaveBeenCalledOnce();
    expect(result.current.isUnread(row())).toBe(false);
    await act(async () => window.dispatchEvent(new Event('online')));
    expect(save).toHaveBeenCalledTimes(2);
    expect(save.mock.calls[1][0]).toEqual([{ job_id: 'job-1', attempt_id: 1 }]);
  });

  it('does not regress a new read when an older cloud response arrives', async () => {
    let finish!: (value: AgentReadReceiptsResponse) => void;
    list.mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)));
    const { result } = renderHook(() => useConversationReadState('alice'));
    const next = row({ id: 'job-2', turnNo: 2, currentAttemptId: 2 });
    act(() => result.current.markRead(next));
    await act(async () => finish(page([cloudReceipt])));
    expect(result.current.isUnread(next)).toBe(false);
    expect(save.mock.calls[0][0]).toEqual([{ job_id: 'job-2', attempt_id: 2 }]);
  });

  it('uploads a read made during an in-flight upload as the next batch', async () => {
    list.mockResolvedValue(page());
    let finish!: (value: AgentReadReceiptsResponse) => void;
    save.mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)));
    const { result } = renderHook(() => useConversationReadState('alice'));
    await waitFor(() => expect(result.current.ready).toBe(true));
    act(() => result.current.markRead(row()));
    const next = row({ id: 'job-2', turnNo: 2, currentAttemptId: 2 });
    act(() => result.current.markRead(next));
    await act(async () => finish(page([cloudReceipt])));
    expect(save.mock.calls.map(([batch]) => batch)).toEqual([
      [{ job_id: 'job-1', attempt_id: 1 }],
      [{ job_id: 'job-2', attempt_id: 2 }],
    ]);
    expect(result.current.isUnread(next)).toBe(false);
  });

  it('ignores responses from a previous account, including an old cookie session', async () => {
    let finish!: (value: AgentReadReceiptsResponse) => void;
    list.mockImplementationOnce(() => new Promise((resolve) => (finish = resolve)));
    list.mockResolvedValue(page([cloudReceipt], 'alice'));
    const first = renderHook(({ id }) => useConversationReadState(id), {
      initialProps: { id: 'alice' },
    });
    first.rerender({ id: 'bob' });
    await act(async () => finish(page([cloudReceipt])));
    expect(first.result.current.isUnread(row())).toBe(true);
    expect(localStorage.getItem(conversationReadStorageKey('bob'))).toBeNull();
    expect(save).not.toHaveBeenCalled();
    expect(list.mock.calls[0][1]?.aborted).toBe(true);
  });

  it('refreshes another device’s reads on focus and every 30 seconds', async () => {
    vi.useFakeTimers();
    try {
      list.mockResolvedValue(page());
      const { result } = renderHook(() => useConversationReadState('alice'));
      await act(async () => {});
      expect(result.current.isUnread(row())).toBe(true);
      list.mockResolvedValue(page([cloudReceipt]));
      await act(async () => vi.advanceTimersByTimeAsync(30_000));
      expect(result.current.isUnread(row())).toBe(false);
      list.mockResolvedValue(page([{ ...cloudReceipt, attempt_id: 2 }]));
      await act(async () => window.dispatchEvent(new Event('focus')));
      expect(result.current.isUnread(row({ currentAttemptId: 2 }))).toBe(false);
      expect(save).not.toHaveBeenCalled();
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });

  it('defers hidden-tab polling and refreshes when the tab becomes visible', async () => {
    vi.useFakeTimers();
    const visibility = vi.spyOn(document, 'visibilityState', 'get');
    try {
      list.mockResolvedValue(page());
      const { result } = renderHook(() => useConversationReadState('alice'));
      await act(async () => {});
      visibility.mockReturnValue('hidden');
      await act(async () => vi.advanceTimersByTimeAsync(60_000));
      expect(list).toHaveBeenCalledOnce();
      list.mockResolvedValue(page([cloudReceipt]));
      visibility.mockReturnValue('visible');
      await act(async () => document.dispatchEvent(new Event('visibilitychange')));
      expect(result.current.isUnread(row())).toBe(false);
      expect(list).toHaveBeenCalledTimes(2);
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });

  it('aborts a stuck request and retries without marking unviewed history read', async () => {
    vi.useFakeTimers();
    try {
      list.mockImplementationOnce(
        (_, signal) =>
          new Promise((_, reject) => {
            signal?.addEventListener('abort', () => reject(new Error('timeout')));
          }),
      );
      const { result } = renderHook(() => useConversationReadState('alice'));
      expect(result.current.ready).toBe(false);
      await act(async () => vi.advanceTimersByTimeAsync(15_000));
      expect(result.current.ready).toBe(true);
      expect(result.current.isUnread(row())).toBe(true);
      list.mockResolvedValue(page([cloudReceipt]));
      await act(async () => window.dispatchEvent(new Event('online')));
      expect(result.current.isUnread(row())).toBe(false);
      expect(save).not.toHaveBeenCalled();
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });

  it('persists to the cloud even when browser storage is blocked', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    list.mockResolvedValue(page());
    const { result } = renderHook(() => useConversationReadState('alice'));
    await waitFor(() => expect(result.current.ready).toBe(true));
    await act(async () => markConversationRead(row(), 'alice'));
    expect(save.mock.calls[0][0]).toEqual([{ job_id: 'job-1', attempt_id: 1 }]);
    expect(result.current.isUnread(row())).toBe(false);
  });

  it('queues reconnection during a failing write and retries even after the tab is hidden', async () => {
    list.mockResolvedValue(page());
    let fail!: (error: Error) => void;
    save.mockImplementationOnce(() => new Promise((_, reject) => (fail = reject)));
    const { result } = renderHook(() => useConversationReadState('alice'));
    await waitFor(() => expect(result.current.ready).toBe(true));
    act(() => result.current.markRead(row()));
    vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    act(() => {
      window.dispatchEvent(new Event('online'));
      window.dispatchEvent(new Event('online'));
    });
    await act(async () => fail(new Error('old request failed')));
    expect(list).toHaveBeenCalledTimes(2);
    expect(save).toHaveBeenCalledTimes(2);
    expect(save.mock.calls[1][0]).toEqual([{ job_id: 'job-1', attempt_id: 1 }]);
    expect(result.current.isUnread(row())).toBe(false);
  });

  it('does not run queued retries after an account switch', async () => {
    list.mockResolvedValue(page());
    let fail!: (error: Error) => void;
    save.mockImplementationOnce(() => new Promise((_, reject) => (fail = reject)));
    const first = renderHook(({ id }) => useConversationReadState(id), {
      initialProps: { id: 'alice' },
    });
    await waitFor(() => expect(first.result.current.ready).toBe(true));
    act(() => first.result.current.markRead(row()));
    act(() => window.dispatchEvent(new Event('online')));
    list.mockResolvedValue(page([], 'bob'));
    first.rerender({ id: 'bob' });
    await act(async () => fail(new Error('old request failed')));
    expect(save).toHaveBeenCalledOnce();
    expect(localStorage.getItem(conversationReadStorageKey('bob'))).toBeNull();
  });

  it('remembers ignored uploads across remounts but still sends newer reads', async () => {
    list.mockResolvedValue(page());
    markConversationRead(row(), 'alice');
    const first = renderHook(() => useConversationReadState('alice'));
    await waitFor(() => expect(save).toHaveBeenCalledOnce());
    await act(async () => {});
    first.unmount();
    const second = renderHook(() => useConversationReadState('alice'));
    await waitFor(() => expect(second.result.current.ready).toBe(true));
    expect(save).toHaveBeenCalledOnce();
    await act(async () => second.result.current.markRead(row({ currentAttemptId: 2 })));
    expect(save).toHaveBeenCalledTimes(2);
    expect(save.mock.calls[1][0]).toEqual([{ job_id: 'job-1', attempt_id: 2 }]);
  });

  it('does not POST cloud receipts or acknowledged imports received from another tab', async () => {
    list.mockResolvedValue(page());
    const { result } = renderHook(() => useConversationReadState('alice'));
    await waitFor(() => expect(result.current.ready).toBe(true));
    const receipts = { 'thread-1': { jobId: 'job-1', attemptId: 1, turnNo: 1 } };
    const cache = JSON.stringify({ version: 2, receipts, acknowledged: receipts });
    localStorage.setItem(conversationReadStorageKey('alice'), cache);
    await act(async () =>
      window.dispatchEvent(
        new StorageEvent('storage', {
          key: conversationReadStorageKey('alice'),
          newValue: cache,
        }),
      ),
    );
    expect(result.current.isUnread(row())).toBe(false);
    expect(save).not.toHaveBeenCalled();
    await act(async () => result.current.markRead(row({ currentAttemptId: 2 })));
    expect(save).toHaveBeenCalledOnce();
  });

  it('resumes incremental paging after failure without jumping past another device on upload', async () => {
    list.mockResolvedValueOnce({ ...page([cloudReceipt]), next_cursor: '10', sync_cursor: '10' });
    list.mockRejectedValueOnce(new Error('second page offline'));
    const { result } = renderHook(() => useConversationReadState('alice'));
    await waitFor(() => expect(result.current.ready).toBe(true));
    save.mockResolvedValue({ ...page(), sync_cursor: '99' });
    await act(async () => result.current.markRead(row({ currentAttemptId: 2 })));
    list.mockResolvedValue({
      ...page([{ ...cloudReceipt, thread_id: 'thread-2', job_id: 'job-2' }]),
      sync_cursor: '11',
    });
    await act(async () => window.dispatchEvent(new Event('focus')));
    expect(list.mock.calls[2][0]).toBe('10');
    expect(result.current.isUnread(row({ id: 'job-2' }, 'thread-2'))).toBe(false);
    list.mockResolvedValue({ ...page(), sync_cursor: '11' });
    await act(async () => window.dispatchEvent(new Event('focus')));
    expect(list.mock.calls[3][0]).toBe('11');
    expect(save).toHaveBeenCalledOnce();
  });
});

// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentJob } from './types';
import type { SidebarConversation } from './useConversationList';
import {
  conversationReadStorageKey,
  markConversationRead,
  useConversationReadState,
} from './useConversationReadState';

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
beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
describe('conversation completion read receipts', () => {
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
    expect(result.current.isUnread(row())).toBe(true);
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
});

// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it } from 'vitest';
import type { AgentJobApi } from '@/lib/api/agents';
import type { AgentJob } from './types';
import { useWorkspaceTabs } from './useWorkspaceTabs';

const source = { id: 'main-1', threadId: 'main', sandboxId: 'group' } as AgentJob;
const row = { id: 'main-1', thread_id: 'main', turn_no: 1, state: 'succeeded' } as AgentJobApi;
beforeEach(() => sessionStorage.clear());
afterEach(cleanup);

it('opens with no side chat and numbers the first one the + button creates', () => {
  const { result } = renderHook(() => useWorkspaceTabs('owner', source, [row]));
  expect(result.current.chats).toEqual([]);
  expect(result.current.selected).toBe('git');
  act(() => {
    result.current.add();
  });
  // The original locator is reused so a receipt from the earlier UI reopens.
  expect(result.current.chats).toEqual([
    { id: 'side-chat', number: 1, anchor: { jobId: 'main-1', sandboxId: 'group' } },
  ]);
  expect(result.current.selected).toBe('side-chat');
  act(() => {
    result.current.add();
  });
  expect(result.current.chats.at(-1)?.number).toBe(2);
  expect(result.current.chats.at(-1)?.id).not.toBe('side-chat');
});

it('pins each new tab to its creation boundary while later main turns advance', () => {
  const { result, rerender } = renderHook(({ jobs }) => useWorkspaceTabs('owner', source, jobs), {
    initialProps: { jobs: [row] },
  });
  act(() => {
    result.current.add();
  });
  const firstId = result.current.selected;
  const running = { ...row, id: 'main-2', turn_no: 2, state: 'running' as const };
  const queued = { ...row, id: 'main-3', turn_no: 3, state: 'queued' as const };
  rerender({ jobs: [row, running, queued] });
  act(() => {
    result.current.add();
  });
  expect(result.current.chats.find((chat) => chat.id === firstId)?.anchor?.jobId).toBe('main-1');
  expect(result.current.chats.at(-1)?.anchor).toEqual({ jobId: 'main-2', sandboxId: 'group' });
});

it('isolates open tabs and selection by account and main conversation, restoring them on return', () => {
  const { result, rerender } = renderHook(({ userId, job }) => useWorkspaceTabs(userId, job, []), {
    initialProps: { userId: 'owner', job: source },
  });
  act(() => {
    result.current.add();
  });
  const created = result.current.selected;
  rerender({ userId: 'other', job: source });
  expect(result.current.chats).toEqual([]);
  expect(result.current.selected).toBe('git');
  rerender({ userId: 'owner', job: { ...source, threadId: 'different' } });
  expect(result.current.chats).toEqual([]);
  rerender({ userId: 'owner', job: source });
  expect(result.current.chats).toHaveLength(1);
  expect(result.current.selected).toBe(created);
});

// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { forkAgentJob } from '@/lib/api/agents';
import type { AgentJob } from '../types';
import { useJobRunActions } from './useJobRunActions';

const navigation = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock('next/navigation', () => ({ useRouter: () => navigation }));
vi.mock('@/components/providers', () => ({ useT: () => (key: string) => key }));
vi.mock('react-hot-toast', () => ({ default: { success: vi.fn() } }));
vi.mock('@/lib/api/agents', () => ({
  forkAgentJob: vi.fn(),
  cancelAgentJob: vi.fn(),
  restoreAgentJob: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(globalThis, 'confirm').mockReturnValue(true);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('fork completion and page lifetime', () => {
  it.each([false, true])('respects a user leaving while Fork is pending: %s', async (leave) => {
    let finish!: (value: Awaited<ReturnType<typeof forkAgentJob>>) => void;
    vi.mocked(forkAgentJob).mockReturnValue(new Promise((resolve) => (finish = resolve)));
    const job = {
      id: 'source-turn',
      threadId: 'source-chat',
      state: 'done',
      workspaceShared: true,
      sandboxId: 'sandbox',
      nativeForkAvailable: true,
    } as AgentJob;
    const { result, unmount } = renderHook(() =>
      useJobRunActions({ job, userId: 'owner', onError: vi.fn() }),
    );
    let pending!: Promise<void>;
    act(() => {
      pending = result.current.forkFrom(job.id);
    });
    expect(forkAgentJob).toHaveBeenCalledWith(job.id);
    if (leave) unmount();
    await act(async () => {
      finish({ id: 'fork-result' } as Awaited<ReturnType<typeof forkAgentJob>>);
      await pending;
    });
    if (leave) expect(navigation.push).not.toHaveBeenCalled();
    else expect(navigation.push).toHaveBeenCalledWith('/fork-result');
  });

  it.each(['leave', 'switch chat', 'switch account'] as const)(
    'ignores a late failure after the user chooses to %s',
    async (action) => {
      let fail!: (cause: Error) => void;
      vi.mocked(forkAgentJob).mockReturnValue(new Promise((_resolve, reject) => (fail = reject)));
      const job = {
        id: 'source-turn',
        threadId: 'source-chat',
        state: 'done',
        workspaceShared: true,
        sandboxId: 'sandbox',
        nativeForkAvailable: true,
      } as AgentJob;
      const onError = vi.fn();
      const { result, rerender, unmount } = renderHook(
        (props) => useJobRunActions({ ...props, onError }),
        { initialProps: { job, userId: 'owner' } },
      );
      let pending!: Promise<void>;
      act(() => {
        pending = result.current.forkFrom(job.id);
      });
      if (action === 'leave') unmount();
      else if (action === 'switch chat') {
        rerender({ job: { ...job, id: 'other-turn', threadId: 'other-chat' }, userId: 'owner' });
      } else rerender({ job, userId: 'other-owner' });
      onError.mockClear();
      await act(async () => {
        fail(new Error('Late Fork failure'));
        await pending;
      });
      expect(onError).not.toHaveBeenCalled();
      expect(navigation.push).not.toHaveBeenCalled();
    },
  );
});

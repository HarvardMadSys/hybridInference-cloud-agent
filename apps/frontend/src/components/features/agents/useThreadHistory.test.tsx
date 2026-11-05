// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, expect, it, vi } from 'vitest';
import type { AgentThreadApi } from '@/lib/api/agents';
import type { AgentReadScope } from '@/lib/api/agentReadScope';
import { useThreadHistory } from './useThreadHistory';

afterEach(cleanup);
const page = (thread: string, next: string | null): AgentThreadApi => ({
  thread_id: thread,
  jobs: [],
  messages: [],
  next_cursor: next,
});
function owner(thread: string) {
  let finish!: (page: AgentThreadApi) => void;
  const reads: AgentReadScope = {
    getAgentJob: vi.fn(),
    getAgentJobThread: vi
      .fn()
      .mockResolvedValueOnce(page(thread, 'older'))
      .mockImplementationOnce(
        () =>
          new Promise<AgentThreadApi>((resolve) => {
            finish = resolve;
          }),
      ),
    getUsage: vi.fn(),
    setUsage: vi.fn(),
    invalidate: vi.fn(),
  };
  return { reads, finish: (value: AgentThreadApi) => finish(value) };
}

it('lets a new owner backfill immediately and ignores the previous owner settling during it', async () => {
  const alice = owner('alice');
  const bob = owner('bob');
  const { result, rerender } = renderHook(
    ({ reads }) => {
      const [thread, setThread] = useState<AgentThreadApi | null>(null);
      return { thread, setThread, ...useThreadHistory('job', reads, thread, setThread, false) };
    },
    { initialProps: { reads: alice.reads } },
  );
  await act(async () => result.current.setThread(await result.current.readThread('job')));
  act(() => result.current.history.loadOlder());
  expect(result.current.history.loading).toBe(true);
  rerender({ reads: bob.reads });
  await act(async () => result.current.setThread(await result.current.readThread('job')));
  expect(result.current.history.loading).toBe(false);
  act(() => result.current.history.loadOlder());
  expect(bob.reads.getAgentJobThread).toHaveBeenCalledTimes(2);
  await act(async () => alice.finish(page('alice', null)));
  expect(result.current.thread?.thread_id).toBe('bob');
  expect(result.current.history.loading).toBe(true);
  await act(async () => bob.finish(page('bob', null)));
  expect(result.current.thread?.thread_id).toBe('bob');
  expect(result.current.history.loading).toBe(false);
  expect(result.current.history.hasOlder).toBe(false);
});

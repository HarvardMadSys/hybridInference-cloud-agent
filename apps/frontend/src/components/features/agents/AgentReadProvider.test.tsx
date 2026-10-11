// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { getAgentJob, type AgentJobApi } from '@/lib/api/agents';
import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { AgentReadProvider, useAgentReads } from './AgentReadProvider';
import { PUSH_BATCH_MS, receiveSync, resetAgentPush } from './agentPush';

vi.mock('@/lib/api/agents', () => ({ getAgentJob: vi.fn(), getAgentJobThread: vi.fn() }));

beforeEach(() => {
  vi.resetAllMocks();
  resetAgentPush();
});
afterEach(() => {
  cleanup();
  resetAgentPush();
  vi.useRealTimers();
});

describe('the shell read scope', () => {
  it('creates another scope when the signed-in owner changes, even while reads are pending', () => {
    vi.mocked(getAgentJob).mockImplementation(() => new Promise<AgentJobApi>(() => {}));
    let userId = 'alice';
    const wrapper = ({ children }: { children: ReactNode }) => (
      <AgentReadProvider userId={userId}>{children}</AgentReadProvider>
    );
    const hook = renderHook(() => useAgentReads(), { wrapper });
    const alice = hook.result.current;
    alice.getAgentJob('job-a');
    userId = 'bob';
    hook.rerender();
    expect(hook.result.current).not.toBe(alice);
    hook.result.current.getAgentJob('job-a');
    expect(getAgentJob).toHaveBeenCalledTimes(2);
  });

  it('invalidates pending reads after session changes and owner-stream resync', () => {
    vi.useFakeTimers();
    vi.mocked(getAgentJob).mockImplementation(() => new Promise<AgentJobApi>(() => {}));
    const wrapper = ({ children }: { children: ReactNode }) => (
      <AgentReadProvider userId="alice">{children}</AgentReadProvider>
    );
    const hook = renderHook(() => useAgentReads(), { wrapper });
    hook.result.current.getAgentJob('job-a');
    act(() => announceAgentSessionsChanged({ threadIds: ['thread-a'], archived: true }));
    hook.result.current.getAgentJob('job-a');
    expect(getAgentJob).toHaveBeenCalledTimes(2);
    act(() => {
      receiveSync();
      vi.advanceTimersByTime(PUSH_BATCH_MS);
    });
    hook.result.current.getAgentJob('job-a');
    expect(getAgentJob).toHaveBeenCalledTimes(3);
  });
});

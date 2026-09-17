// @vitest-environment jsdom
import { StrictMode, type ReactNode } from 'react';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { forkAgentJob, getAgentJob, getAgentJobThread, type AgentJobApi } from '@/lib/api/agents';
import { APIError } from '@/lib/utils/errors';
import type { AgentJob } from './types';
import { sideChatStorageKey, useSideChatSession } from './useSideChatSession';

vi.mock('@/lib/api/agents', () => ({
  forkAgentJob: vi.fn(),
  getAgentJob: vi.fn(),
  getAgentJobThread: vi.fn(),
}));

const source = {
  id: 'source-2',
  threadId: 'main',
  sandboxId: 'sandbox',
  workspaceShared: true,
  state: 'done',
  runtime: 'codex',
  model: 'test',
} as AgentJob;
const anchor = {
  id: 'source-2',
  thread_id: 'main',
  sandbox_id: 'sandbox',
  state: 'succeeded',
  conversation_state: 'idle',
  native_state: 'ready',
  native_fork_available: true,
  turn_no: 2,
} as AgentJobApi;
const copied = {
  ...anchor,
  id: 'side-copy',
  thread_id: 'side',
  forked_from_job_id: anchor.id,
  fork_anchor_job_id: anchor.id,
} as AgentJobApi;
let userId: string;
let sequence = 0;
beforeEach(() => {
  userId = `side-chat-hook-${++sequence}`;
  vi.clearAllMocks();
  sessionStorage.clear();
  vi.mocked(getAgentJob).mockResolvedValue(anchor);
  vi.mocked(forkAgentJob).mockResolvedValue(copied);
  vi.mocked(getAgentJobThread).mockResolvedValue({
    thread_id: 'side',
    jobs: [copied],
    messages: [],
  });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

async function flush() {
  await act(async () => {});
}

describe('native side chat session', () => {
  it('does nothing until selected and coalesces StrictMode creation', async () => {
    const { result, rerender } = renderHook(
      ({ active }) => useSideChatSession(source, userId, active, [anchor]),
      {
        initialProps: { active: false },
        wrapper: ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>,
      },
    );
    expect(getAgentJob).not.toHaveBeenCalled();
    rerender({ active: true });
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(forkAgentJob).toHaveBeenCalledExactlyOnceWith(anchor.id, {
      requestScope: sideChatStorageKey(userId, 'main'),
    });
    rerender({ active: false });
    rerender({ active: true });
    await flush();
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
  });

  it('fixes the latest visible main boundary before an asynchronous lookup finishes', async () => {
    let resolve!: (job: AgentJobApi) => void;
    vi.mocked(getAgentJob).mockReturnValueOnce(
      new Promise((r) => {
        resolve = r;
      }),
    );
    const { result, rerender } = renderHook(
      ({ jobs }) => useSideChatSession({ ...source, id: 'source-1' }, userId, true, jobs),
      { initialProps: { jobs: [anchor] } },
    );
    const later = { ...anchor, id: 'source-3', turn_no: 3 };
    rerender({ jobs: [anchor, later] });
    await act(async () => {
      resolve(anchor);
    });
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(forkAgentJob).toHaveBeenCalledWith('source-2', expect.anything());
    expect(forkAgentJob).not.toHaveBeenCalledWith('source-3', expect.anything());
  });

  it('waits for the fixed active turn, pauses when hidden, and does not adopt later work', async () => {
    vi.useFakeTimers();
    const running: AgentJobApi = {
      ...anchor,
      state: 'running' as const,
      conversation_state: 'running' as const,
      native_fork_available: false,
    };
    vi.mocked(getAgentJob).mockResolvedValue(running);
    const { result, rerender } = renderHook(
      ({ active, jobs }) => useSideChatSession(source, userId, active, jobs),
      { initialProps: { active: true, jobs: [running] } },
    );
    await flush();
    expect(result.current.status).toBe('waiting');
    expect(forkAgentJob).not.toHaveBeenCalled();
    rerender({ active: false, jobs: [running] });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000);
    });
    expect(getAgentJob).toHaveBeenCalledTimes(1);
    vi.mocked(getAgentJob).mockResolvedValue({
      ...anchor,
      conversation_state: 'running',
      native_fork_available: false,
    });
    const later = { ...running, id: 'source-3', turn_no: 3 };
    rerender({ active: true, jobs: [anchor, later] });
    await flush();
    expect(result.current.status).toBe('waiting');
    vi.mocked(getAgentJob).mockResolvedValue(anchor);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(result.current.status).toBe('ready');
    expect(forkAgentJob).toHaveBeenCalledWith(anchor.id, expect.anything());
  });

  it('does not issue a new fork if the panel closes during the source lookup', async () => {
    let resolve!: (job: AgentJobApi) => void;
    vi.mocked(getAgentJob).mockReturnValueOnce(
      new Promise((r) => {
        resolve = r;
      }),
    );
    const { rerender } = renderHook(({ active }) => useSideChatSession(source, userId, active), {
      initialProps: { active: true },
    });
    rerender({ active: false });
    await act(async () => {
      resolve(anchor);
    });
    expect(forkAgentJob).not.toHaveBeenCalled();
  });

  it('fails closed if the chosen running turn fails, without falling back to an older success', async () => {
    vi.mocked(getAgentJob).mockResolvedValue({
      ...anchor,
      state: 'failed',
      native_fork_available: false,
    });
    const { result } = renderHook(() => useSideChatSession(source, userId, true));
    await waitFor(() => expect(result.current.status).toBe('error'));
    expect(result.current.error).toMatch(/did not complete/);
    expect(forkAgentJob).not.toHaveBeenCalled();
  });

  it('recovers an ambiguous issued fork even after the source becomes busy', async () => {
    vi.mocked(forkAgentJob).mockRejectedValueOnce(new Error('Response lost'));
    const { result } = renderHook(() => useSideChatSession(source, userId, true));
    await waitFor(() => expect(result.current.status).toBe('error'));
    vi.mocked(getAgentJob).mockResolvedValue({
      ...anchor,
      conversation_state: 'running',
      native_fork_available: false,
    });
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(vi.mocked(forkAgentJob).mock.calls[1]).toEqual(vi.mocked(forkAgentJob).mock.calls[0]);
  });

  it('retries the same request after a server-side source-busy race', async () => {
    vi.useFakeTimers();
    vi.mocked(forkAgentJob).mockRejectedValueOnce(new APIError('sandbox_busy', 'Busy', 409));
    const { result } = renderHook(() => useSideChatSession(source, userId, true));
    await flush();
    expect(result.current.status).toBe('waiting');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(result.current.status).toBe('ready');
    expect(forkAgentJob).toHaveBeenCalledTimes(2);
  });

  it('retains an in-flight child across unmount and restores its newest turn', async () => {
    let resolve!: (job: AgentJobApi) => void;
    vi.mocked(forkAgentJob).mockReturnValueOnce(
      new Promise((r) => {
        resolve = r;
      }),
    );
    const first = renderHook(() => useSideChatSession(source, userId, true));
    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledTimes(1));
    first.unmount();
    await act(async () => {
      resolve(copied);
    });
    const next = { ...copied, id: 'side-3', turn_no: 3, forked_from_job_id: null };
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: 'side',
      jobs: [copied, next],
      messages: [],
    });
    const second = renderHook(() => useSideChatSession(source, userId, true));
    await waitFor(() => expect(second.result.current.status).toBe('ready'));
    expect(second.result.current.session?.jobId).toBe('side-3');
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
  });

  it.each(['workspace', 'parent', 'thread'])(
    'rejects a saved child with the wrong %s',
    async (mismatch) => {
      sessionStorage.setItem(
        sideChatStorageKey(userId, 'main'),
        JSON.stringify({
          anchorJobId: anchor.id,
          sandboxId: 'sandbox',
          jobId: copied.id,
          threadId: 'side',
          requestSent: true,
        }),
      );
      const foreign = {
        ...copied,
        ...(mismatch === 'workspace' ? { sandbox_id: 'elsewhere' } : {}),
        ...(mismatch === 'parent' ? { fork_anchor_job_id: 'other-anchor' } : {}),
        ...(mismatch === 'thread' ? { thread_id: 'other-thread' } : {}),
      };
      vi.mocked(getAgentJobThread).mockResolvedValue({
        thread_id: 'side',
        jobs: [foreign],
        messages: [],
      });
      const { result } = renderHook(() => useSideChatSession(source, userId, true));
      await waitFor(() => expect(result.current.status).toBe('error'));
      expect(result.current.session).toBeNull();
      expect(forkAgentJob).not.toHaveBeenCalled();
    },
  );
});

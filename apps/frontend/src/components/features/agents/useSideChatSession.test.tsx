// @vitest-environment jsdom
import { installSideChatLocks } from './sideChatTestSupport';
import { StrictMode, type ReactNode } from 'react';
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { forkAgentJob, getAgentJob, getAgentJobThread, type AgentJobApi } from '@/lib/api/agents';
import { submitThreadMessage } from '@/lib/api/pendingMessages';
import { APIError } from '@/lib/utils/errors';
import type { AgentJob } from './types';
import { sideChatStorageKey, useSideChatSession } from './useSideChatSession';
import { sideChatKeys } from './sideChatStorage';

vi.mock('@/lib/api/agents', () => ({
  forkAgentJob: vi.fn(),
  getAgentJob: vi.fn(),
  getAgentJobThread: vi.fn(),
}));
vi.mock('@/lib/api/pendingMessages', () => ({ submitThreadMessage: vi.fn() }));

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
const userId = 'side-test-user';
beforeEach(() => {
  vi.resetAllMocks();
  sessionStorage.clear();
  localStorage.clear();
  installSideChatLocks();
  vi.mocked(getAgentJob).mockResolvedValue(anchor);
  vi.mocked(forkAgentJob).mockResolvedValue(copied);
  vi.mocked(getAgentJobThread).mockResolvedValue({
    thread_id: 'side',
    jobs: [copied],
    messages: [],
  });
  vi.mocked(submitThreadMessage).mockResolvedValue({ pending: null, job: copied });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

async function flush() {
  await act(async () => {});
}

describe('native side chat session', () => {
  it('uses a new tab’s pinned boundary and never migrates the original tab’s receipt', async () => {
    sessionStorage.setItem(
      `agent-side-chat:${userId}:main`,
      JSON.stringify({
        anchorJobId: anchor.id,
        sandboxId: 'sandbox',
        jobId: 'legacy-copy',
        threadId: 'legacy-thread',
      }),
    );
    const pinned = { ...anchor, id: 'source-1', turn_no: 1 };
    vi.mocked(getAgentJob).mockResolvedValue(pinned);
    vi.mocked(forkAgentJob).mockResolvedValue({ ...copied, fork_anchor_job_id: pinned.id });
    const { result } = renderHook(() =>
      useSideChatSession(source, userId, true, [anchor], {
        id: 'side-chat-new',
        number: 2,
        anchor: { jobId: pinned.id, sandboxId: 'sandbox' },
      }),
    );
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(forkAgentJob).toHaveBeenCalledExactlyOnceWith(pinned.id, {
      requestId: expect.any(String),
    });
    expect(sessionStorage.getItem(`agent-side-chat:${userId}:main`)).toContain('legacy-copy');
    expect(localStorage.getItem(sideChatKeys(userId, 'main', 'side-chat-new').session)).toContain(
      'side-copy',
    );
    expect(localStorage.getItem(sideChatStorageKey(userId, 'main'))).toBeNull();
  });

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
      requestId: expect.any(String),
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
    await waitFor(() => expect(getAgentJob).toHaveBeenCalledTimes(1));
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
    expect(result.current.error).toBe('sideChat.unfinished');
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
      localStorage.setItem(
        sideChatStorageKey(userId, 'main'),
        JSON.stringify({
          anchorJobId: anchor.id,
          sandboxId: 'sandbox',
          jobId: copied.id,
          threadId: 'side',
          operation: { id: 'request-1', state: 'unknown' },
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

describe('side chat durable recovery', () => {
  const record = () => ({
    anchorJobId: anchor.id,
    sandboxId: 'sandbox',
    jobId: copied.id,
    threadId: 'side',
    operation: { id: 'durable-id', state: 'unknown' },
  });
  it('restores a valid localStorage receipt without any in-memory state', async () => {
    localStorage.setItem(sideChatStorageKey(userId, 'main'), JSON.stringify(record()));
    const { result } = renderHook(() => useSideChatSession(source, userId, true));
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(result.current.session?.jobId).toBe(copied.id);
    expect(forkAgentJob).not.toHaveBeenCalled();
  });
  it('serializes two independent windows and shares the same durable child', async () => {
    const first = renderHook(() => useSideChatSession(source, userId, true));
    const second = renderHook(() => useSideChatSession(source, userId, true));
    await waitFor(() => expect(second.result.current.status).toBe('ready'));
    expect(first.result.current.session?.jobId).toBe(second.result.current.session?.jobId);
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
  });
  it('retains the receipt when validation fails, then recovers without another POST', async () => {
    vi.mocked(forkAgentJob).mockResolvedValueOnce({ ...copied, sandbox_id: 'wrong' });
    const { result } = renderHook(() => useSideChatSession(source, userId, true));
    await waitFor(() => expect(result.current.error).toBe('sideChat.invalidChild'));
    expect(JSON.parse(localStorage.getItem(sideChatStorageKey(userId, 'main'))!).jobId).toBe(
      copied.id,
    );
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
  });
  it('rechecks archival after a durable failed saga before rotating its request id', async () => {
    vi.mocked(forkAgentJob).mockRejectedValueOnce(
      new APIError('native_fork_failed', 'Failed', 409),
    );
    const { result } = renderHook(() => useSideChatSession(source, userId, true));
    await waitFor(() => expect(result.current.status).toBe('error'));
    vi.mocked(getAgentJob).mockResolvedValue({ ...anchor, archived_at: '2026-09-17' });
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.error).toBe('sideChat.unfinished'));
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
    vi.mocked(getAgentJob).mockResolvedValue(anchor);
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(vi.mocked(forkAgentJob).mock.calls[1][1]?.requestId).not.toBe(
      vi.mocked(forkAgentJob).mock.calls[0][1]?.requestId,
    );
  });
  it('GET-polls a known busy response with backoff and keeps the waiting status stable', async () => {
    vi.useFakeTimers();
    vi.mocked(forkAgentJob).mockRejectedValueOnce(new APIError('sandbox_busy', 'Busy', 409));
    const { result } = renderHook(() => useSideChatSession(source, userId, true));
    await flush();
    vi.mocked(getAgentJob).mockResolvedValue({ ...anchor, conversation_state: 'running' });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(result.current.status).toBe('waiting');
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(getAgentJob).toHaveBeenCalledTimes(2);
    let resolve!: (row: AgentJobApi) => void;
    vi.mocked(getAgentJob).mockReturnValueOnce(
      new Promise((r) => {
        resolve = r;
      }),
    );
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
    });
    expect(result.current.status).toBe('waiting');
    await act(async () => {
      resolve(anchor);
    });
    expect(result.current.status).toBe('ready');
  });
  it('recovers a deleted child on retry instead of keeping a dead locator', async () => {
    localStorage.setItem(sideChatStorageKey(userId, 'main'), JSON.stringify(record()));
    vi.mocked(getAgentJobThread).mockResolvedValueOnce(null);
    const { result } = renderHook(() => useSideChatSession(source, userId, true));
    await waitFor(() => expect(result.current.error).toBe('sideChat.deleted'));
    expect(localStorage.getItem(sideChatStorageKey(userId, 'main'))).toBeNull();
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.status).toBe('ready'));
  });
  it('offers explicit recovery for corrupt storage and does not leak JSON parser errors', async () => {
    localStorage.setItem(sideChatStorageKey(userId, 'main'), '{bad');
    const { result } = renderHook(() => useSideChatSession(source, userId, true));
    await waitFor(() => expect(result.current.error).toBe('sideChat.invalidRecord'));
    expect(result.current.recoverable).toBe(true);
    expect(forkAgentJob).not.toHaveBeenCalled();
    await act(async () => {
      await result.current.restart();
    });
    await waitFor(() => expect(result.current.status).toBe('ready'));
  });
  it('migrates an in-flight legacy key without changing operation identity', async () => {
    const legacy = `agent-side-chat:${userId}:main`;
    sessionStorage.setItem(
      legacy,
      JSON.stringify({ anchorJobId: anchor.id, sandboxId: 'sandbox', requestSent: true }),
    );
    sessionStorage.setItem(
      `agent-native-fork-request-${legacy}:${anchor.id}`,
      'original-operation',
    );
    const { result } = renderHook(() => useSideChatSession(source, userId, true));
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(forkAgentJob).toHaveBeenCalledWith(anchor.id, { requestId: 'original-operation' });
    expect(sessionStorage.getItem(legacy)).toBeNull();
  });
});

describe('automatic code review', () => {
  const tab = {
    id: 'side-chat-review' as const,
    number: 1,
    anchor: { jobId: anchor.id, sandboxId: 'sandbox' },
    reviewPrompt: 'Review these changes without editing files.',
  };
  const key = sideChatKeys(userId, 'main', tab.id).session;
  const reviewJob = { ...copied, id: 'review-turn', forked_from_job_id: null, turn_no: 3 };

  it('starts one child review across concurrent consumers and restores it without resending', async () => {
    vi.mocked(submitThreadMessage).mockResolvedValue({ pending: null, job: reviewJob });
    const first = renderHook(() => useSideChatSession(source, userId, true, [anchor], tab), {
      wrapper: ({ children }: { children: ReactNode }) => <StrictMode>{children}</StrictMode>,
    });
    const second = renderHook(() => useSideChatSession(source, userId, true, [anchor], tab));
    await waitFor(() => expect(second.result.current.status).toBe('ready'));
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
    const operation = vi.mocked(forkAgentJob).mock.calls[0][1]?.requestId;
    expect(submitThreadMessage).toHaveBeenCalledExactlyOnceWith(
      'side',
      { prompt: tab.reviewPrompt, skill_ids: [] },
      { requestId: `review-${operation}` },
    );
    expect(first.result.current.session?.jobId).toBe(reviewJob.id);
    expect(JSON.parse(localStorage.getItem(key)!)).toMatchObject({ reviewSent: true });
    first.unmount();
    second.unmount();
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: 'side',
      jobs: [copied, reviewJob],
      messages: [],
    });
    const restored = renderHook(() => useSideChatSession(source, userId, true, [anchor], tab));
    await waitFor(() => expect(restored.result.current.status).toBe('ready'));
    expect(restored.result.current.session?.jobId).toBe(reviewJob.id);
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
    expect(submitThreadMessage).toHaveBeenCalledTimes(1);
  });

  it('recovers a lost review reply using the same child and message key after remount', async () => {
    vi.mocked(submitThreadMessage).mockRejectedValueOnce(new Error('Response lost'));
    const first = renderHook(() => useSideChatSession(source, userId, true, [anchor], tab));
    await waitFor(() => expect(first.result.current.error).toBe('Response lost'));
    const request = vi.mocked(submitThreadMessage).mock.calls[0];
    expect(JSON.parse(localStorage.getItem(key)!)).toMatchObject({ jobId: copied.id });
    first.unmount();
    // The server committed the first message even though its reply was lost.
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: 'side',
      jobs: [copied, reviewJob],
      messages: [],
    });
    vi.mocked(submitThreadMessage).mockResolvedValue({ pending: null, job: reviewJob });
    const retry = renderHook(() => useSideChatSession(source, userId, true, [anchor], tab));
    await waitFor(() => expect(retry.result.current.status).toBe('ready'));
    expect(submitThreadMessage).toHaveBeenCalledTimes(2);
    expect(vi.mocked(submitThreadMessage).mock.calls[1]).toEqual(request);
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
    expect(retry.result.current.session?.jobId).toBe(reviewJob.id);
  });

  it('replays the same send if persisting the successful review receipt fails', async () => {
    vi.mocked(submitThreadMessage).mockResolvedValue({ pending: null, job: reviewJob });
    const write = Storage.prototype.setItem;
    let fail = true;
    const spy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
      this: Storage,
      storedKey: string,
      value: string,
    ) {
      if (fail && storedKey === key && JSON.parse(value).reviewSent) {
        fail = false;
        throw new Error('Receipt write failed');
      }
      return write.call(this, storedKey, value);
    });
    try {
      const { result } = renderHook(() => useSideChatSession(source, userId, true, [anchor], tab));
      await waitFor(() => expect(result.current.error).toBe('Receipt write failed'));
      act(() => result.current.retry());
      await waitFor(() => expect(result.current.status).toBe('ready'));
      expect(forkAgentJob).toHaveBeenCalledTimes(1);
      expect(vi.mocked(submitThreadMessage).mock.calls[1]).toEqual(
        vi.mocked(submitThreadMessage).mock.calls[0],
      );
      expect(JSON.parse(localStorage.getItem(key)!)).toMatchObject({ reviewSent: true });
    } finally {
      spy.mockRestore();
    }
  });

  it('marks a committed review before validating its job receipt, preventing a second send', async () => {
    vi.mocked(submitThreadMessage).mockResolvedValueOnce({
      pending: null,
      job: { ...reviewJob, sandbox_id: 'wrong-workspace' },
    });
    const { result } = renderHook(() => useSideChatSession(source, userId, true, [anchor], tab));
    await waitFor(() => expect(result.current.error).toBe('sideChat.invalidChild'));
    expect(JSON.parse(localStorage.getItem(key)!)).toMatchObject({ reviewSent: true });
    vi.mocked(getAgentJobThread).mockResolvedValue({
      thread_id: 'side',
      jobs: [copied, reviewJob],
      messages: [],
    });
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(result.current.session?.jobId).toBe(reviewJob.id);
    expect(submitThreadMessage).toHaveBeenCalledTimes(1);
  });

  it('waits to send a review if its tab closes while the fork is being created', async () => {
    let resolve!: (job: AgentJobApi) => void;
    vi.mocked(forkAgentJob).mockReturnValueOnce(new Promise((r) => (resolve = r)));
    const { result, rerender } = renderHook(
      ({ active }) => useSideChatSession(source, userId, active, [anchor], tab),
      { initialProps: { active: true } },
    );
    await waitFor(() => expect(forkAgentJob).toHaveBeenCalledTimes(1));
    rerender({ active: false });
    await act(async () => resolve(copied));
    expect(submitThreadMessage).not.toHaveBeenCalled();
    rerender({ active: true });
    await waitFor(() => expect(result.current.status).toBe('ready'));
    expect(submitThreadMessage).toHaveBeenCalledTimes(1);
    expect(forkAgentJob).toHaveBeenCalledTimes(1);
  });
});

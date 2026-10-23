// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';
import { getAgentJob, type AgentJobApi } from '@/lib/api/agents';
import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { listThreadQuestions, type AgentQuestionsApi } from '@/lib/api/questions';
import { AgentReadProvider, useAgentReads, useQuestionReads } from './AgentReadProvider';
import {
  PUSH_BATCH_MS,
  receiveHint,
  receiveJobChange,
  receiveSync,
  receiveThreadChange,
  resetAgentPush,
} from './agentPush';
import { clearWorkspaceGitCache } from './workspaceGitCache';

vi.mock('@/lib/api/agents', () => ({ getAgentJob: vi.fn(), getAgentJobThread: vi.fn() }));
vi.mock('@/lib/api/questions', () => ({ listThreadQuestions: vi.fn() }));
vi.mock('./workspaceGitCache', () => ({ clearWorkspaceGitCache: vi.fn() }));

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
  it('aborts the previous owner question reads and starts an independent scope', async () => {
    vi.mocked(listThreadQuestions).mockImplementation(
      () => new Promise<AgentQuestionsApi>(() => {}),
    );
    let userId = 'alice';
    const wrapper = ({ children }: { children: ReactNode }) => (
      <AgentReadProvider userId={userId}>{children}</AgentReadProvider>
    );
    const hook = renderHook(() => useQuestionReads(), { wrapper });
    const alice = hook.result.current;
    const abandoned = expect(alice.listThreadQuestions('thread')).rejects.toMatchObject({
      name: 'AbortError',
    });
    userId = 'bob';
    hook.rerender();
    await abandoned;
    expect(hook.result.current).not.toBe(alice);
    expect(vi.mocked(listThreadQuestions).mock.calls[0][1]?.aborted).toBe(true);
    const bob = expect(hook.result.current.listThreadQuestions('thread')).rejects.toMatchObject({
      name: 'AbortError',
    });
    expect(listThreadQuestions).toHaveBeenCalledTimes(2);
    hook.unmount();
    await bob;
  });

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
    expect(clearWorkspaceGitCache).toHaveBeenCalledTimes(1);
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

  it('cancels question reads only for a change to their own turn, or a gap in pushes', () => {
    vi.useFakeTimers();
    const reads: Record<string, AbortSignal[]> = {};
    vi.mocked(listThreadQuestions).mockImplementation((threadId, signal) => {
      (reads[threadId] ??= []).push(signal!);
      return new Promise<AgentQuestionsApi>(() => {});
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <AgentReadProvider userId="alice">{children}</AgentReadProvider>
    );
    const questions = renderHook(() => useQuestionReads(), { wrapper }).result.current;
    const read = (threadId: string) => void questions.listThreadQuestions(threadId).catch(() => {});
    const pending = (threadId: string) => !reads[threadId].at(-1)!.aborted;
    read('thread-a');
    read('thread-b');
    // A new message moves the conversation, a publication its workspace, and a
    // rename is the owner's own: none of them changes the turn's questions.
    act(() => {
      receiveThreadChange({ id: 'thread-a', revision: 2, title: 'Fix login' });
      receiveHint('publication', 'thread-a');
      receiveJobChange({ id: 'job-b', thread_id: 'thread-b', state: 'succeeded', revision: 2 });
      announceAgentSessionsChanged({ threadIds: ['thread-b'], title: 'Renamed' });
      vi.advanceTimersByTime(PUSH_BATCH_MS);
    });
    expect(pending('thread-a')).toBe(true);
    expect(pending('thread-b')).toBe(false);
    act(() => {
      receiveJobChange({ id: 'job-a', thread_id: 'thread-a', state: 'succeeded', revision: 2 });
      vi.advanceTimersByTime(PUSH_BATCH_MS);
    });
    expect(pending('thread-a')).toBe(false);
    read('thread-a');
    read('thread-b');
    act(() => {
      receiveThreadChange({ id: 'thread-b', deleted: true });
      vi.advanceTimersByTime(PUSH_BATCH_MS);
    });
    expect(pending('thread-a')).toBe(true);
    expect(pending('thread-b')).toBe(false);
    read('thread-b');
    // A deleted job is sent without its conversation, and a resync means
    // pushes were missed: either way every read is cancelled.
    act(() => {
      receiveJobChange({ id: 'job-gone', deleted: true });
      vi.advanceTimersByTime(PUSH_BATCH_MS);
    });
    expect(pending('thread-a')).toBe(false);
    expect(pending('thread-b')).toBe(false);
    read('thread-a');
    act(() => {
      receiveSync();
      vi.advanceTimersByTime(PUSH_BATCH_MS);
    });
    expect(pending('thread-a')).toBe(false);
    expect(listThreadQuestions).toHaveBeenCalledTimes(6);
  });

  it('rereads only the thread named by a native-question hint', () => {
    vi.useFakeTimers();
    vi.mocked(listThreadQuestions).mockResolvedValue({
      job_id: 'job',
      attempt_id: 1,
      questions: [],
    });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <AgentReadProvider userId="alice">{children}</AgentReadProvider>
    );
    const reads = renderHook(() => useQuestionReads(), { wrapper }).result.current;
    const readA = vi.fn(() => void reads.listThreadQuestions('thread-a'));
    const readB = vi.fn(() => void reads.listThreadQuestions('thread-b'));
    const leaveA = reads.watch('thread-a', { intervalMs: 60_000 }, readA);
    const leaveB = reads.watch('thread-b', { intervalMs: 60_000 }, readB);
    expect(listThreadQuestions).toHaveBeenCalledTimes(2);

    act(() => {
      receiveHint('question', 'thread-a');
      vi.advanceTimersByTime(PUSH_BATCH_MS);
    });

    expect(readA).toHaveBeenCalledTimes(2);
    expect(readB).toHaveBeenCalledOnce();
    expect(listThreadQuestions).toHaveBeenCalledTimes(3);
    leaveA();
    leaveB();
  });
});

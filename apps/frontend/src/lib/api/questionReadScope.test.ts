import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { listThreadQuestions, type AgentQuestionsApi } from './questions';
import { createQuestionReadScope, QUESTION_READ_TTL_MS } from './questionReadScope';

vi.mock('./questions', () => ({ listThreadQuestions: vi.fn() }));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (cause: Error) => void;
  const promise = new Promise<T>((done, failed) => {
    resolve = done;
    reject = failed;
  });
  return { promise, resolve, reject };
}

const SNAPSHOT: AgentQuestionsApi = { job_id: 'job', attempt_id: 1, questions: [] };

beforeEach(() => vi.resetAllMocks());
afterEach(() => vi.useRealTimers());

describe('shared question reads', () => {
  it('joins pending readers and staggered polls, then refreshes after the brief cache expires', async () => {
    vi.useFakeTimers();
    const held = deferred<AgentQuestionsApi>();
    vi.mocked(listThreadQuestions).mockReturnValueOnce(held.promise).mockResolvedValue(SNAPSHOT);
    const reads = createQuestionReadScope();
    const sidebar = reads.listThreadQuestions('thread');
    const detail = reads.listThreadQuestions('thread');
    expect(listThreadQuestions).toHaveBeenCalledOnce();
    held.resolve(SNAPSHOT);
    await Promise.all([sidebar, detail]);
    await vi.advanceTimersByTimeAsync(QUESTION_READ_TTL_MS - 1);
    expect(await reads.listThreadQuestions('thread')).toBe(SNAPSHOT);
    expect(listThreadQuestions).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    await reads.listThreadQuestions('thread');
    expect(listThreadQuestions).toHaveBeenCalledTimes(2);
  });

  it('lets a sidebar reader leave without cancelling the detail reader', async () => {
    const held = deferred<AgentQuestionsApi>();
    vi.mocked(listThreadQuestions).mockReturnValue(held.promise);
    const reads = createQuestionReadScope();
    const sidebarController = new AbortController();
    const sidebar = expect(
      reads.listThreadQuestions('thread', sidebarController.signal),
    ).rejects.toMatchObject({
      name: 'AbortError',
    });
    const detail = reads.listThreadQuestions('thread');
    sidebarController.abort();
    await sidebar;
    expect(vi.mocked(listThreadQuestions).mock.calls[0][1]?.aborted).toBe(false);
    held.resolve(SNAPSHOT);
    expect(await detail).toBe(SNAPSHOT);
    expect(listThreadQuestions).toHaveBeenCalledOnce();
  });

  it('fences an invalidated poll even if the adapter resolves it after an accepted answer', async () => {
    const old = deferred<AgentQuestionsApi>();
    const fresh = deferred<AgentQuestionsApi>();
    vi.mocked(listThreadQuestions)
      .mockReturnValueOnce(old.promise)
      .mockReturnValueOnce(fresh.promise);
    const reads = createQuestionReadScope();
    const stale = expect(reads.listThreadQuestions('thread')).rejects.toMatchObject({
      name: 'AbortError',
    });
    reads.invalidate('thread');
    const current = reads.listThreadQuestions('thread');
    old.resolve({ ...SNAPSHOT, questions: [{ request_id: 'answered', questions: [] }] });
    await stale;
    expect(reads.listThreadQuestions('thread')).toBe(current);
    fresh.resolve(SNAPSHOT);
    expect(await current).toBe(SNAPSHOT);
    expect(await reads.listThreadQuestions('thread')).toBe(SNAPSHOT);
    expect(listThreadQuestions).toHaveBeenCalledTimes(2);
  });

  it('keeps owner scopes independent and retries failed reads without caching the failure', async () => {
    vi.mocked(listThreadQuestions)
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValue(SNAPSHOT);
    const alice = createQuestionReadScope();
    const bob = createQuestionReadScope();
    await expect(alice.listThreadQuestions('thread')).rejects.toThrow('offline');
    await alice.listThreadQuestions('thread');
    await bob.listThreadQuestions('thread');
    expect(listThreadQuestions).toHaveBeenCalledTimes(3);
  });
});

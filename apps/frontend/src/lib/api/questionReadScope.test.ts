// @vitest-environment jsdom
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
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

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

describe('one question clock per conversation', () => {
  it('reads every consumer on one tick, as often as the most frequent one needs', async () => {
    vi.useFakeTimers();
    vi.mocked(listThreadQuestions).mockResolvedValue(SNAPSHOT);
    const reads = createQuestionReadScope();
    const page = vi.fn(() => void reads.listThreadQuestions('thread'));
    const row = vi.fn(() => void reads.listThreadQuestions('thread'));
    const leavePage = reads.watch('thread', { intervalMs: 2_000 }, page);
    const leaveRow = reads.watch('thread', { intervalMs: 12_000 }, row);
    expect(page).toHaveBeenCalledOnce();
    expect(row).toHaveBeenCalledOnce();
    expect(listThreadQuestions).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(page).toHaveBeenCalledTimes(2);
    expect(row).toHaveBeenCalledTimes(2);
    expect(listThreadQuestions).toHaveBeenCalledTimes(2);
    // Without the page, the row's own pace is enough.
    leavePage();
    await vi.advanceTimersByTimeAsync(11_999);
    expect(row).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(row).toHaveBeenCalledTimes(3);
    expect(page).toHaveBeenCalledTimes(2);
    leaveRow();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(row).toHaveBeenCalledTimes(3);
    expect(vi.getTimerCount()).toBe(0);
  });

  it('keeps separate conversations on separate clocks', async () => {
    vi.useFakeTimers();
    const reads = createQuestionReadScope();
    const open = vi.fn();
    const other = vi.fn();
    reads.watch('open', { intervalMs: 2_000 }, open);
    reads.watch('other', { intervalMs: 12_000 }, other);
    await vi.advanceTimersByTimeAsync(12_000);
    expect(open).toHaveBeenCalledTimes(7);
    expect(other).toHaveBeenCalledTimes(2);
  });

  it('pauses what the page shows while the tab is hidden, but not a notification', async () => {
    vi.useFakeTimers();
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('visible');
    const reads = createQuestionReadScope();
    const page = vi.fn();
    const notification = vi.fn();
    reads.watch('thread', { intervalMs: 2_000 }, page);
    reads.watch('thread', { intervalMs: 12_000, whileHidden: true }, notification);
    visibility.mockReturnValue('hidden');
    document.dispatchEvent(new Event('visibilitychange'));
    await vi.advanceTimersByTimeAsync(11_999);
    expect(page).toHaveBeenCalledOnce();
    expect(notification).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(1);
    expect(page).toHaveBeenCalledOnce();
    expect(notification).toHaveBeenCalledTimes(2);
    // Shown again, the page catches up at once and returns to its own pace.
    visibility.mockReturnValue('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    expect(page).toHaveBeenCalledTimes(2);
    expect(notification).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(2_000);
    expect(page).toHaveBeenCalledTimes(3);
    expect(notification).toHaveBeenCalledTimes(3);
  });

  it('starts a consumer that joins while the tab is hidden when the tab is shown', async () => {
    vi.useFakeTimers();
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    const reads = createQuestionReadScope();
    const page = vi.fn();
    reads.watch('thread', { intervalMs: 2_000 }, page);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(page).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    visibility.mockReturnValue('visible');
    document.dispatchEvent(new Event('visibilitychange'));
    expect(page).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(page).toHaveBeenCalledTimes(2);
  });

  it('listens for visibility only while a clock runs', () => {
    const add = vi.spyOn(document, 'addEventListener');
    const remove = vi.spyOn(document, 'removeEventListener');
    const visibilityListeners = (spy: typeof add) =>
      spy.mock.calls.filter(([type]) => type === 'visibilitychange');
    const reads = createQuestionReadScope();
    const leaveOne = reads.watch('one', { intervalMs: 2_000 }, () => {});
    const leaveOther = reads.watch('other', { intervalMs: 12_000 }, () => {});
    expect(visibilityListeners(add)).toHaveLength(1);
    leaveOne();
    leaveOne();
    expect(visibilityListeners(remove)).toHaveLength(0);
    leaveOther();
    expect(visibilityListeners(remove)).toEqual(visibilityListeners(add));
  });
});

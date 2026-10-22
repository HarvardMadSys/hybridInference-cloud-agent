// @vitest-environment jsdom
import { act, cleanup, render, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  getAgentJob,
  getAgentNotificationState,
  listAgentConversations,
  type AgentJobApi,
  type AgentNotificationStateApi,
} from '@/lib/api/agents';
import { listThreadQuestions, type AgentQuestionsApi } from '@/lib/api/questions';
import { APIError } from '@/lib/utils/errors';
import {
  receiveJobChange,
  receiveThreadChange,
  receiveSync,
  resetAgentPush,
  setAgentPushLive,
  PUSH_BATCH_MS,
} from './agentPush';
import {
  AgentNotifications,
  useAgentNotifications,
  NOTIFICATION_QUESTION_POLL_MS,
  NOTIFICATION_STATE_POLL_MS,
  type AgentNotice,
} from './AgentNotifications';
import { NOTIFICATION_PERMISSION_EVENT } from './notificationPreferences';

vi.mock('@/lib/api/agents', () => ({
  getAgentJob: vi.fn(),
  getAgentNotificationState: vi.fn(),
  listAgentConversations: vi.fn(),
}));
vi.mock('@/lib/api/questions', () => ({ listThreadQuestions: vi.fn() }));
const push = vi.fn();
let userId: string | undefined = 'owner';
const preferences = { enabled: true, questions: true, turns: true };
vi.mock('next/navigation', () => ({ useRouter: () => ({ push }) }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: userId ? { id: userId } : null } }),
  useT: () => (key: string) => key,
}));
vi.mock('./notificationPreferences', () => ({
  NOTIFICATION_PERMISSION_EVENT: 'agent:notification-permission',
  useNotificationPreferences: () => [preferences],
}));

const list = vi.mocked(getAgentNotificationState);
const get = vi.mocked(getAgentJob);
const questions = vi.mocked(listThreadQuestions);
const notify = vi.fn<(notice: AgentNotice) => boolean>(() => true);
function job(overrides: Partial<AgentJobApi> = {}): AgentJobApi {
  return {
    id: 'job-1',
    thread_id: 'thread-1',
    title: 'Fix login',
    repo: 'owner/repo',
    state: 'running',
    current_attempt_id: 10,
    turn_no: 1,
    revision: 1,
    cancel_requested: false,
    finished_at: null,
    ...overrides,
  } as AgentJobApi;
}
function page(
  jobs: AgentJobApi[],
  next_cursor: string | null = null,
  overrides: Partial<AgentNotificationStateApi> = {},
): AgentNotificationStateApi {
  return {
    mode: 'snapshot',
    jobs: jobs.map((job) => ({
      id: job.id,
      thread_id: job.thread_id!,
      title: job.title || job.repo || 'Agent task',
      repo: job.repo ?? null,
      state: job.state as AgentNotificationStateApi['jobs'][number]['state'],
      current_attempt_id: job.current_attempt_id,
      turn_no: job.turn_no ?? 1,
      revision: job.revision ?? 0,
      cancel_requested: job.cancel_requested,
      finished_at: job.finished_at ?? null,
      updated_at: job.updated_at ?? null,
    })),
    threads: [],
    deleted_job_ids: [],
    deleted_thread_ids: [],
    observed_at: new Date().toISOString(),
    next_cursor,
    change_cursor: next_cursor ? null : 'changes',
    reset_required: false,
    ...overrides,
  };
}
function snapshot(jobId = 'job-1', attemptId = 10, requestId = 'request-1'): AgentQuestionsApi {
  return {
    job_id: jobId,
    attempt_id: attemptId,
    questions: [{ request_id: requestId, questions: [] }],
  };
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((yes) => {
    resolve = yes;
  });
  return { promise, resolve };
}
async function mount(options = preferences) {
  let result!: ReturnType<typeof renderHook>;
  await act(async () => {
    result = renderHook(() => useAgentNotifications(userId, options, notify));
  });
  return result;
}
async function changed(next: AgentJobApi) {
  get.mockResolvedValueOnce(next);
  await act(async () => {
    receiveJobChange({
      id: next.id,
      thread_id: next.thread_id ?? undefined,
      state: next.state,
      revision: next.revision,
    });
    await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-11T12:00:00Z'));
  resetAgentPush();
  localStorage.clear();
  userId = 'owner';
  list.mockReset().mockResolvedValue(page([job()]));
  get.mockReset();
  vi.mocked(listAgentConversations).mockReset();
  questions.mockReset().mockResolvedValue({ job_id: 'job-1', attempt_id: 10, questions: [] });
  notify.mockClear();
  push.mockClear();
});
afterEach(() => {
  cleanup();
  resetAgentPush();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('owner-wide agent notifications', () => {
  it('uses the server request baseline when the browser is behind and the response is delayed', async () => {
    const pending = deferred<AgentNotificationStateApi>();
    list.mockReturnValueOnce(pending.promise);
    await mount({ ...preferences, questions: false });
    const older = job({
      id: 'older',
      state: 'succeeded',
      // This predates enablement but falls inside the delayed response window.
      finished_at: '2026-10-11T12:59:58Z',
    });
    await act(async () => vi.advanceTimersByTimeAsync(5_000));
    const terminal = job({ state: 'succeeded', finished_at: '2026-10-11T13:00:00.500Z' });
    await act(async () =>
      pending.resolve(
        page([older, terminal], null, {
          observed_at: '2026-10-11T13:00:00Z',
          change_cursor: 'bootstrap-tail',
        }),
      ),
    );
    expect(notify).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ jobId: 'job-1', kind: 'succeeded' }),
    );
    list.mockResolvedValue(page([older, terminal], null, { mode: 'delta' }));
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS * 2));
    expect(list).toHaveBeenLastCalledWith('bootstrap-tail');
    expect(notify).toHaveBeenCalledTimes(1);
    expect(get).not.toHaveBeenCalled();
  });

  it('holds an unseen pushed completion until the server epoch is known', async () => {
    const pending = deferred<AgentNotificationStateApi>();
    list.mockReturnValueOnce(pending.promise);
    await mount({ ...preferences, questions: false });
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    const terminal = job({
      state: 'succeeded',
      revision: 2,
      finished_at: '2026-10-11T13:00:00.500Z',
    });

    await changed(terminal);
    expect(notify).not.toHaveBeenCalled();

    await act(async () => pending.resolve(page([], null, { observed_at: '2026-10-11T13:00:00Z' })));
    expect(notify).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ jobId: 'job-1', kind: 'succeeded' }),
    );
  });

  it('never scans 1,000 archived conversations and uses one state request per steady-state refresh', async () => {
    const archived = Array.from({ length: 1_000 }, (_, i) => ({
      job: job({
        id: `archived-${i}`,
        thread_id: `archived-thread-${i}`,
        state: 'succeeded',
        finished_at: '2026-10-10T00:00:00Z',
      }),
      title: `Archived ${i}`,
      archived: true,
      last_activity_at: '2026-10-10T00:00:00Z',
    }));
    vi.mocked(listAgentConversations).mockImplementation(async (query) => {
      const start = Number(query?.cursor ?? 0);
      const end = start + (query?.limit ?? 50);
      return {
        conversations: archived.slice(start, end),
        next_cursor: end < archived.length ? String(end) : null,
      };
    });
    list.mockResolvedValueOnce(page([job()], null, { change_cursor: 'tail-0' }));
    await mount({ ...preferences, questions: false });
    expect(list).toHaveBeenCalledExactlyOnceWith(undefined);
    for (let n = 1; n <= 3; n += 1) {
      list.mockResolvedValueOnce(page([], null, { mode: 'delta', change_cursor: `tail-${n}` }));
      await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
      expect(list).toHaveBeenCalledTimes(n + 1);
      expect(list).toHaveBeenLastCalledWith(`tail-${n - 1}`);
    }
    expect(listAgentConversations).not.toHaveBeenCalled();
    expect(get).not.toHaveBeenCalled();
    expect(notify).not.toHaveBeenCalled();
  });

  it('deduplicates a queued-to-terminal delta across stream-gap resyncs and repeated pushes', async () => {
    list.mockResolvedValueOnce(
      page([job({ state: 'queued', current_attempt_id: null })], null, {
        change_cursor: 'queued-tail',
      }),
    );
    await mount({ ...preferences, questions: false });
    const terminal = job({
      state: 'succeeded',
      revision: 2,
      finished_at: new Date().toISOString(),
    });
    list.mockResolvedValueOnce(
      page([terminal], null, { mode: 'delta', change_cursor: 'finished-tail' }),
    );
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
    expect(list).toHaveBeenLastCalledWith('queued-tail');
    expect(notify).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ kind: 'succeeded', attemptId: 10 }),
    );
    list.mockResolvedValueOnce(
      page([terminal], null, { mode: 'delta', change_cursor: 'resynced-tail' }),
    );
    await act(async () => {
      receiveSync();
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    expect(list).toHaveBeenLastCalledWith('finished-tail');
    await changed({ ...terminal, revision: 3 });
    expect(notify).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(3);
  });

  it.each(['job', 'thread'])(
    'honors a %s deletion delta before terminal and pending-question responses',
    async (kind) => {
      const pending = deferred<AgentQuestionsApi>();
      questions.mockReturnValue(pending.promise);
      await mount();
      list.mockResolvedValueOnce(
        page(
          [job({ state: 'succeeded', revision: 2, finished_at: new Date().toISOString() })],
          null,
          {
            mode: 'delta',
            deleted_job_ids: kind === 'job' ? ['job-1'] : [],
            deleted_thread_ids: kind === 'thread' ? ['thread-1'] : [],
          },
        ),
      );
      await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
      await act(async () => pending.resolve(snapshot()));
      await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_QUESTION_POLL_MS));
      expect(notify).not.toHaveBeenCalled();
      expect(questions).toHaveBeenCalledTimes(1);
    },
  );

  it('fences an old in-flight job read when an expired cursor rebuild excludes that job', async () => {
    await mount({ ...preferences, questions: false });
    const pending = deferred<AgentJobApi>();
    get.mockReturnValueOnce(pending.promise);
    await act(async () => {
      receiveJobChange({ id: 'job-1', state: 'succeeded', revision: 2 });
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    get.mockRejectedValueOnce(new APIError('not_found', 'Deleted', 404));
    list
      .mockResolvedValueOnce(
        page([], null, { mode: 'delta', reset_required: true, change_cursor: null }),
      )
      .mockResolvedValueOnce(page([], null, { change_cursor: 'rebuilt-tail' }));
    await act(async () => {
      receiveSync();
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    expect(list).toHaveBeenNthCalledWith(3, undefined);
    await act(async () =>
      pending.resolve(
        job({ state: 'succeeded', revision: 2, finished_at: new Date().toISOString() }),
      ),
    );
    expect(notify).not.toHaveBeenCalled();
  });

  it('isolates cursors and receipts across owners while ignoring the previous owner state response', async () => {
    list.mockResolvedValueOnce(page([job()], null, { change_cursor: 'owner-tail' }));
    const mounted = await mount({ ...preferences, questions: false });
    await changed(job({ state: 'succeeded', revision: 2 }));
    const pending = deferred<AgentNotificationStateApi>();
    list.mockReturnValueOnce(pending.promise);
    await act(async () => {
      receiveSync();
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    expect(list).toHaveBeenLastCalledWith('owner-tail');
    act(() => resetAgentPush());
    userId = 'another-owner';
    list.mockResolvedValueOnce(page([job()], null, { change_cursor: 'other-tail' }));
    await act(async () => mounted.rerender());
    expect(list).toHaveBeenLastCalledWith(undefined);
    await changed(job({ state: 'succeeded', revision: 2 }));
    expect(notify).toHaveBeenCalledTimes(2);
    await act(async () =>
      pending.resolve(
        page(
          [
            job({
              id: 'late-old-owner',
              state: 'succeeded',
              finished_at: new Date().toISOString(),
            }),
          ],
          null,
          { mode: 'delta', change_cursor: 'stale-owner-tail' },
        ),
      ),
    );
    list.mockResolvedValueOnce(page([], null, { mode: 'delta', change_cursor: 'other-next' }));
    await act(async () => {
      receiveSync();
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    expect(list).toHaveBeenLastCalledWith('other-tail');
    expect(notify).toHaveBeenCalledTimes(2);
    const key = JSON.stringify(['turn', 'job-1', 10]);
    expect(JSON.parse(localStorage.getItem('agents.notifications.receipts.owner')!)).toContain(key);
    expect(
      JSON.parse(localStorage.getItem('agents.notifications.receipts.another-owner')!),
    ).toContain(key);
  });

  it('reconciles only previously active jobs across a cursor gap longer than terminal retention', async () => {
    const queued = job({
      id: 'queued',
      thread_id: 'queued-thread',
      state: 'queued',
      current_attempt_id: null,
    });
    const old = job({
      id: 'old-terminal',
      state: 'succeeded',
      finished_at: '2026-10-11T11:59:00Z',
    });
    list.mockResolvedValueOnce(page([job(), queued, old]));
    await mount({ ...preferences, questions: false });
    vi.setSystemTime(new Date('2026-10-13T12:00:00Z'));
    get.mockImplementation(async (id) =>
      id === 'job-1'
        ? job({ state: 'succeeded', revision: 2, finished_at: '2026-10-11T13:00:00Z' })
        : { ...queued, state: 'cancelled', revision: 2 },
    );
    list
      .mockResolvedValueOnce(
        page([], null, { mode: 'delta', reset_required: true, change_cursor: null }),
      )
      .mockResolvedValueOnce(page([], null, { change_cursor: 'after-reset' }));
    await act(async () => {
      receiveSync();
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    expect(get.mock.calls.map(([id]) => id).sort()).toEqual(['job-1', 'queued']);
    expect(notify.mock.calls.map(([notice]) => [notice.jobId, notice.kind])).toEqual([
      ['job-1', 'succeeded'],
      ['queued', 'cancelled'],
    ]);
    list.mockResolvedValue(page([], null, { mode: 'delta', change_cursor: 'after-reset' }));
    await act(async () => {
      receiveSync();
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    expect(list).toHaveBeenLastCalledWith('after-reset');
    expect(get).toHaveBeenCalledTimes(2);
    expect(notify).toHaveBeenCalledTimes(2);
  });

  it('retries an unconfirmed metadata-only push after an empty delta so questions resume', async () => {
    await mount();
    get.mockRejectedValueOnce(new Error('temporary job read failure'));
    await act(async () => {
      receiveJobChange({ id: 'job-1', state: 'running', revision: 2 });
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    questions.mockResolvedValue(snapshot());
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_QUESTION_POLL_MS));
    expect(notify).not.toHaveBeenCalled();
    expect(questions).toHaveBeenCalledTimes(1);
    get.mockResolvedValueOnce(job({ revision: 2 }));
    list.mockResolvedValueOnce(page([], null, { mode: 'delta' }));
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
    expect(get).toHaveBeenCalledTimes(2);
    expect(notify).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ kind: 'question', jobId: 'job-1' }),
    );
  });

  it.each(['job', 'thread'])(
    'clears failed-read retries after a %s deletion delta',
    async (kind) => {
      await mount({ ...preferences, questions: false });
      get.mockRejectedValueOnce(new Error('temporary job read failure'));
      await act(async () => {
        receiveJobChange({ id: 'job-1', state: 'running', revision: 2 });
        await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
      });
      list.mockResolvedValueOnce(
        page([], null, {
          mode: 'delta',
          deleted_job_ids: kind === 'job' ? ['job-1'] : [],
          deleted_thread_ids: kind === 'thread' ? ['thread-1'] : [],
        }),
      );
      await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
      list.mockResolvedValue(page([], null, { mode: 'delta' }));
      await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
      expect(get).toHaveBeenCalledTimes(1);
      expect(notify).not.toHaveBeenCalled();
    },
  );

  it('keeps a newer pushed title when an older delta arrives later', async () => {
    list.mockResolvedValueOnce(page([job({ title: 'Initial title' })]));
    await mount({ ...preferences, questions: false });
    const pending = deferred<AgentNotificationStateApi>();
    list.mockReturnValueOnce(pending.promise);
    await act(async () => {
      receiveSync();
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    await act(async () => {
      receiveThreadChange({ id: 'thread-1', title: 'Newest title', revision: 3 });
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    await act(async () =>
      pending.resolve(
        page(
          [
            job({
              title: 'Stale job title',
              state: 'succeeded',
              revision: 2,
              thread_revision: 2,
              finished_at: new Date().toISOString(),
            }),
          ],
          null,
          {
            mode: 'delta',
            threads: [{ id: 'thread-1', title: 'Stale delta title', revision: 2 }],
            change_cursor: 'new-tail',
          },
        ),
      ),
    );
    expect(notify).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ kind: 'succeeded', title: 'Newest title' }),
    );
  });

  it('does not revive a delivered terminal from an older in-flight delta', async () => {
    await mount();
    const pending = deferred<AgentNotificationStateApi>();
    list.mockReturnValueOnce(pending.promise);
    await act(async () => {
      receiveSync();
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    await changed(job({ state: 'succeeded', revision: 3 }));
    questions.mockResolvedValue(snapshot());
    await act(async () => pending.resolve(page([job({ revision: 2 })], null, { mode: 'delta' })));
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_QUESTION_POLL_MS));
    expect(notify).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ kind: 'succeeded' }));
    expect(questions).toHaveBeenCalledTimes(1);
  });

  it.each([false, true])(
    'reconciles a cancellation waiting for a Web Lock across reset (deleted=%s)',
    async (deleted) => {
      const lock = deferred<void>();
      const request = vi.fn((_name: string, send: () => void) => lock.promise.then(send));
      vi.stubGlobal('navigator', { locks: { request } });
      const queued = job({ state: 'queued', current_attempt_id: null });
      list.mockResolvedValueOnce(page([queued]));
      await mount({ ...preferences, questions: false });
      const terminal = {
        ...queued,
        state: 'cancelled' as const,
        revision: 2,
        updated_at: new Date().toISOString(),
      };
      await changed(terminal);
      expect(request).toHaveBeenCalledTimes(1);
      expect(notify).not.toHaveBeenCalled();

      // A suspended tab can retain a queued delivery beyond the feed's
      // recovery window. Rebuilding no longer includes this cancellation.
      vi.setSystemTime(new Date('2026-10-13T12:00:00Z'));
      if (deleted) get.mockRejectedValueOnce(new APIError('not_found', 'Deleted', 404));
      else get.mockResolvedValueOnce(terminal);
      list
        .mockResolvedValueOnce(
          page([], null, { mode: 'delta', reset_required: true, change_cursor: null }),
        )
        .mockResolvedValueOnce(page([], null, { change_cursor: 'rebuilt-tail' }));
      await act(async () => {
        receiveSync();
        await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
      });
      expect(list).toHaveBeenNthCalledWith(3, undefined);
      expect(get).toHaveBeenCalledTimes(2);
      expect(request).toHaveBeenCalledTimes(1);
      await act(async () => lock.resolve());
      if (deleted) {
        expect(notify).not.toHaveBeenCalled();
        expect(localStorage.getItem('agents.notifications.receipts.owner')).toBeNull();
      } else {
        expect(notify).toHaveBeenCalledExactlyOnceWith(
          expect.objectContaining({ kind: 'cancelled', jobId: 'job-1', attemptId: null }),
        );
      }

      list.mockResolvedValue(page([], null, { mode: 'delta', change_cursor: 'next-tail' }));
      await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS * 2));
      expect(list).toHaveBeenLastCalledWith('rebuilt-tail');
      expect(get).toHaveBeenCalledTimes(2);
      expect(request).toHaveBeenCalledTimes(1);
      expect(notify).toHaveBeenCalledTimes(deleted ? 0 : 1);
    },
  );

  it('does not poll an older active turn after the newer terminal row is discarded', async () => {
    list.mockResolvedValueOnce(
      page([
        job(),
        job({
          id: 'newer-terminal',
          turn_no: 2,
          state: 'cancelled',
          finished_at: '2026-10-11T11:59:00Z',
        }),
      ]),
    );
    questions.mockResolvedValue(snapshot());
    await mount();
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_QUESTION_POLL_MS * 2));
    expect(questions).not.toHaveBeenCalled();
    expect(notify).not.toHaveBeenCalled();
  });

  it('seeds history quietly and alerts once when an observed turn finishes, after publishing', async () => {
    list.mockResolvedValue(
      page([job(), job({ id: 'old', thread_id: 'old-thread', state: 'succeeded' })]),
    );
    await mount();
    expect(notify).not.toHaveBeenCalled();
    await changed(job({ state: 'publishing', revision: 2 }));
    expect(notify).not.toHaveBeenCalled();
    await changed(job({ state: 'succeeded', revision: 3 }));
    expect(notify).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ kind: 'succeeded', jobId: 'job-1', title: 'Fix login' }),
    );
    await changed(job({ state: 'succeeded', revision: 4 }));
    expect(notify).toHaveBeenCalledTimes(1);
  });

  it('finds questions on later pages, including a side chat that is not mounted', async () => {
    const side = job({ id: 'side-job', thread_id: 'side-thread', title: 'Review patch' });
    list.mockResolvedValueOnce(page([job()], 'next')).mockResolvedValueOnce(page([side]));
    questions.mockImplementation(async (threadId) =>
      snapshot(threadId === 'side-thread' ? 'side-job' : 'job-1'),
    );
    await mount();
    expect(list).toHaveBeenNthCalledWith(2, 'next');
    expect(notify.mock.calls.map(([notice]) => notice.jobId)).toEqual(['job-1', 'side-job']);
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_QUESTION_POLL_MS * 2));
    expect(notify).toHaveBeenCalledTimes(2);
  });

  it('scopes repeated native request IDs to the attempt and suppresses stale question snapshots', async () => {
    questions.mockResolvedValue(snapshot());
    await mount();
    expect(notify).toHaveBeenCalledTimes(1);
    const retry = job({ current_attempt_id: 11, revision: 2 });
    await changed(retry);
    // A response still belonging to attempt 10 cannot alert for attempt 11.
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_QUESTION_POLL_MS));
    expect(notify).toHaveBeenCalledTimes(1);
    questions.mockResolvedValue(snapshot('job-1', 11));
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_QUESTION_POLL_MS));
    expect(notify).toHaveBeenCalledTimes(2);
  });

  it('fences in-flight questions as soon as a state push starts its job read', async () => {
    const pending = deferred<AgentQuestionsApi>();
    questions.mockReturnValue(pending.promise);
    await mount();
    const pendingJob = deferred<AgentJobApi>();
    get.mockReturnValue(pendingJob.promise);
    await act(async () => {
      receiveJobChange({ id: 'job-1', state: 'succeeded', revision: 2 });
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
      pending.resolve(snapshot());
    });
    expect(notify).not.toHaveBeenCalled();
    questions.mockResolvedValue(snapshot());
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_QUESTION_POLL_MS));
    expect(questions).toHaveBeenCalledTimes(1);
    await act(async () => pendingJob.resolve(job({ state: 'succeeded', revision: 2 })));
    expect(notify).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ kind: 'succeeded' }));
  });

  it('does not revive a deleted chat from a late job read', async () => {
    await mount();
    const pending = deferred<AgentJobApi>();
    get.mockReturnValue(pending.promise);
    await act(async () => {
      receiveJobChange({ id: 'job-1', state: 'succeeded', revision: 2 });
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
      receiveThreadChange({ id: 'thread-1', deleted: true });
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
      pending.resolve(job({ state: 'succeeded', revision: 2 }));
    });
    expect(notify).not.toHaveBeenCalled();
  });

  it('waits for a read that includes the pushed revision before polling questions again', async () => {
    await mount();
    // Send a newer push while the HTTP read is still behind it.
    get.mockResolvedValue(job({ revision: 1 }));
    await act(async () => {
      receiveJobChange({ id: 'job-1', state: 'succeeded', revision: 3 });
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    questions.mockResolvedValue(snapshot());
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_QUESTION_POLL_MS));
    expect(notify).not.toHaveBeenCalled();
    expect(questions).toHaveBeenCalledTimes(1);
  });

  it('polls one delta without pushes and keeps a prior turn when a new one is queued', async () => {
    await mount();
    list.mockResolvedValue(
      page(
        [job({ id: 'job-2', state: 'queued', turn_no: 2 }), job({ state: 'failed', revision: 2 })],
        null,
        { mode: 'delta' },
      ),
    );
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
    expect(list).toHaveBeenLastCalledWith('changes');
    expect(get).not.toHaveBeenCalled();
    expect(notify).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ kind: 'failed', jobId: 'job-1' }),
    );
  });

  it('uses a slower state backstop while pushes are live and refreshes on reconnect', async () => {
    setAgentPushLive(true);
    await mount();
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
    expect(list).toHaveBeenCalledTimes(1);
    list.mockResolvedValue(page([job({ state: 'cancelled', revision: 2 })]));
    await act(async () => {
      receiveSync();
      await vi.advanceTimersByTimeAsync(PUSH_BATCH_MS);
    });
    expect(notify).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ kind: 'cancelled' }));
  });

  it('detects a fast newly completed turn but ignores an old terminal metadata update', async () => {
    list.mockResolvedValue(page([]));
    await mount();
    await changed(job({ id: 'old', state: 'succeeded', finished_at: '2026-10-10T12:00:00Z' }));
    expect(notify).not.toHaveBeenCalled();
    await changed(job({ state: 'succeeded', finished_at: new Date().toISOString() }));
    expect(notify).toHaveBeenCalledTimes(1);
  });

  it('catches a short turn after bootstrap from one telemetry-complete delta', async () => {
    list.mockResolvedValueOnce(page([]));
    await mount();
    const terminal = job({
      state: 'succeeded',
      revision: 2,
      finished_at: new Date().toISOString(),
    });
    list.mockResolvedValue(page([terminal], null, { mode: 'delta' }));
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
    expect(get).not.toHaveBeenCalled();
    expect(notify).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ kind: 'succeeded' }));
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
    expect(notify).toHaveBeenCalledTimes(1);
  });

  it('ignores copied fork history from a full job push but still reports an observed turn', async () => {
    list.mockResolvedValue(page([]));
    await mount({ ...preferences, questions: false });
    const inherited = job({
      id: 'fork-copy',
      thread_id: 'fork-thread',
      forked_from_job_id: 'source-job',
      state: 'failed',
      current_attempt_id: null,
      finished_at: null,
      updated_at: new Date().toISOString(),
    });
    await changed(inherited);
    expect(get).toHaveBeenCalledExactlyOnceWith('fork-copy');
    expect(notify).not.toHaveBeenCalled();

    await changed({ ...inherited, state: 'running', current_attempt_id: 20, revision: 2 });
    await changed({
      ...inherited,
      current_attempt_id: 20,
      revision: 3,
      finished_at: new Date().toISOString(),
    });
    expect(notify).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ kind: 'failed', jobId: 'fork-copy', attemptId: 20 }),
    );
  });

  it('uses the server request baseline when the browser clock is ahead', async () => {
    const pending = deferred<AgentNotificationStateApi>();
    list.mockReturnValueOnce(pending.promise);
    await mount({ ...preferences, questions: false });
    await act(async () => vi.advanceTimersByTimeAsync(1_000));
    const terminal = job({
      state: 'cancelled',
      current_attempt_id: null,
      finished_at: null,
      updated_at: '2026-10-11T11:00:00.500Z',
      revision: 2,
    });
    const older = {
      ...terminal,
      id: 'older-cancellation',
      thread_id: 'older-thread',
      updated_at: '2026-10-11T10:59:00Z',
    };
    await act(async () =>
      pending.resolve(page([older, terminal], null, { observed_at: '2026-10-11T11:00:00Z' })),
    );
    expect(notify).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ kind: 'cancelled', jobId: 'job-1', attemptId: null }),
    );
    list.mockResolvedValue(page([older, terminal], null, { mode: 'delta' }));
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS * 2));
    expect(notify).toHaveBeenCalledTimes(1);
    expect(get).not.toHaveBeenCalled();
  });

  it('notifies a previously unseen queued cancellation with no attempt exactly once', async () => {
    list.mockResolvedValueOnce(page([]));
    await mount({ ...preferences, questions: false });
    const terminal = job({
      state: 'cancelled',
      current_attempt_id: null,
      finished_at: null,
      updated_at: new Date().toISOString(),
      revision: 2,
    });
    list.mockResolvedValue(page([terminal], null, { mode: 'delta' }));
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
    expect(get).not.toHaveBeenCalled();
    expect(notify).toHaveBeenCalledExactlyOnceWith(
      expect.objectContaining({ kind: 'cancelled', jobId: 'job-1', attemptId: null }),
    );
    await changed({ ...terminal, revision: 3 });
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
    expect(notify).toHaveBeenCalledTimes(1);
  });

  it('rebuilds an expired cursor from active jobs without replaying history', async () => {
    await mount();
    list
      .mockResolvedValueOnce(
        page([], null, {
          mode: 'delta',
          change_cursor: null,
          reset_required: true,
        }),
      )
      .mockResolvedValueOnce(page([job({ id: 'job-2', thread_id: 'thread-2' })]));
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
    expect(list).toHaveBeenNthCalledWith(2, 'changes');
    expect(list).toHaveBeenNthCalledWith(3, undefined);
    expect(notify).not.toHaveBeenCalled();
  });

  it('deduplicates across watcher remounts while allowing a completed retry', async () => {
    questions.mockResolvedValue(snapshot());
    const mounted = await mount();
    await changed(job({ state: 'succeeded', revision: 2 }));
    mounted.unmount();
    await mount();
    await changed(job({ state: 'succeeded', revision: 3 }));
    expect(notify).toHaveBeenCalledTimes(2);
    await changed(job({ state: 'running', current_attempt_id: 11, revision: 4 }));
    await changed(job({ state: 'succeeded', current_attempt_id: 11, revision: 5 }));
    expect(notify).toHaveBeenCalledTimes(3);
  });

  it('ignores older reads and late responses after account changes', async () => {
    const pending = deferred<AgentQuestionsApi>();
    questions.mockReturnValue(pending.promise);
    const mounted = await mount();
    await changed(job({ state: 'succeeded', revision: 3 }));
    await changed(job({ state: 'running', revision: 2 }));
    expect(notify).toHaveBeenCalledTimes(1);
    userId = 'another-owner';
    list.mockResolvedValue(page([]));
    await act(async () => mounted.rerender());
    await act(async () => pending.resolve(snapshot()));
    expect(notify).toHaveBeenCalledTimes(1);
  });

  it('does no work when disabled and honors the two notification categories', async () => {
    const disabled = await mount({ ...preferences, enabled: false });
    expect(list).not.toHaveBeenCalled();
    disabled.unmount();
    questions.mockResolvedValue(snapshot());
    await mount({ ...preferences, questions: false });
    expect(questions).not.toHaveBeenCalled();
    await changed(job({ state: 'succeeded', revision: 2 }));
    expect(notify).toHaveBeenCalledTimes(1);
  });

  it('does not record a failed delivery so the pending question can be retried', async () => {
    notify.mockReturnValueOnce(false);
    questions.mockResolvedValue(snapshot());
    await mount();
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_QUESTION_POLL_MS));
    expect(notify).toHaveBeenCalledTimes(2);
  });
});

it('resumes already-enabled notifications after permission is granted without a focus event', async () => {
  const notification = vi.fn(
    class {
      close = vi.fn();
    },
  );
  Object.assign(notification, { permission: 'default' });
  vi.stubGlobal('Notification', notification);
  questions.mockResolvedValue(snapshot());
  await act(async () => {
    render(<AgentNotifications />);
  });
  expect(list).not.toHaveBeenCalled();
  await act(async () => {
    Object.assign(notification, { permission: 'granted' });
    window.dispatchEvent(new Event(NOTIFICATION_PERMISSION_EVENT));
  });
  expect(notification).toHaveBeenCalledWith(
    'notifications.question',
    expect.objectContaining({ body: 'Fix login' }),
  );
});

it('opens the notified job on click without requesting permission automatically', async () => {
  const close = vi.fn();
  const focus = vi.spyOn(window, 'focus').mockImplementation(() => {});
  const constructed: { onclick?: () => void }[] = [];
  const requestPermission = vi.fn();
  const notification = vi.fn(
    class {
      close = close;
      onclick?: () => void;
      constructor() {
        constructed.push(this);
      }
    },
  );
  Object.assign(notification, { permission: 'granted', requestPermission });
  vi.stubGlobal('Notification', notification);
  questions.mockResolvedValue(snapshot());
  await act(async () => {
    render(<AgentNotifications />);
  });
  expect(notification).toHaveBeenCalledWith(
    'notifications.question',
    expect.objectContaining({ body: 'Fix login' }),
  );
  constructed[0].onclick?.();
  expect(close).toHaveBeenCalled();
  expect(focus).toHaveBeenCalled();
  expect(push).toHaveBeenCalledWith('/job-1');
  expect(requestPermission).not.toHaveBeenCalled();
  userId = undefined;
  await act(async () => cleanup());
  // An already displayed desktop alert cannot navigate into the previous account.
  expect(close).toHaveBeenCalledTimes(2);
  constructed[0].onclick?.();
  expect(push).toHaveBeenCalledTimes(1);
  focus.mockRestore();
});

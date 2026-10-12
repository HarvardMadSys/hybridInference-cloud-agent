// @vitest-environment jsdom
import { act, cleanup, render, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getAgentJob, listAgentConversations, type AgentJobApi } from '@/lib/api/agents';
import { listThreadQuestions, type AgentQuestionsApi } from '@/lib/api/questions';
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

vi.mock('@/lib/api/agents', () => ({ getAgentJob: vi.fn(), listAgentConversations: vi.fn() }));
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

const list = vi.mocked(listAgentConversations);
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
function page(jobs: AgentJobApi[], next_cursor: string | null = null) {
  return {
    conversations: jobs.map((job) => ({
      job,
      title: job.title || job.repo,
      archived: false,
      last_activity_at: null,
    })),
    next_cursor,
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
    expect(list).toHaveBeenNthCalledWith(2, { includeArchived: true, limit: 200, cursor: 'next' });
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

  it('polls state without pushes and keeps watching a prior turn after a new one is queued', async () => {
    await mount();
    list.mockResolvedValue(page([job({ id: 'job-2', state: 'queued', turn_no: 2 })]));
    get.mockResolvedValue(job({ state: 'failed', revision: 2 }));
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
    expect(get).toHaveBeenCalledWith('job-1');
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

  it('notifies a turn that finishes while the initial list is in flight', async () => {
    const pending = deferred<Awaited<ReturnType<typeof listAgentConversations>>>();
    list.mockReturnValue(pending.promise);
    await mount();
    let finished!: AgentJobApi;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
      finished = job({ state: 'succeeded', revision: 2, finished_at: new Date().toISOString() });
      pending.resolve(page([finished]));
    });
    expect(notify).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ kind: 'succeeded' }));
    await changed(finished);
    expect(notify).toHaveBeenCalledTimes(1);
  });

  it('confirms lightweight list rows that omit finished-at telemetry before notifying', async () => {
    const terminal = job({ state: 'succeeded', revision: 2, updated_at: new Date().toISOString() });
    list.mockResolvedValue(page([terminal]));
    get.mockResolvedValue({ ...terminal, finished_at: new Date().toISOString() });
    await mount();
    expect(get).toHaveBeenCalledWith('job-1');
    expect(notify).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({ kind: 'succeeded' }));
    await act(async () => vi.advanceTimersByTimeAsync(NOTIFICATION_STATE_POLL_MS));
    expect(get).toHaveBeenCalledTimes(1);
    await changed({ ...terminal, revision: 3, finished_at: new Date().toISOString() });
    expect(notify).toHaveBeenCalledTimes(1);
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

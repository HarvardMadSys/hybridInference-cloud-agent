'use client';

import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth, useT } from '@/components/providers';
import {
  getAgentJob,
  getAgentNotificationState,
  type AgentJobApi,
  type AgentNotificationJobApi,
  type AgentNotificationStateApi,
} from '@/lib/api/agents';
import { useQuestionReads } from './AgentReadProvider';
import { onAgentRecordsChanged, isAgentPushLive, PUSH_FALLBACK_POLL_MS } from './agentPush';
import {
  NOTIFICATION_PERMISSION_EVENT,
  useNotificationPreferences,
  type NotificationPreferences,
} from './notificationPreferences';
import { isActiveJobState } from './conversationJob';
import { QUESTION_BACKGROUND_POLL_MS } from './useNativeQuestions';
import { APIError } from '@/lib/utils/errors';

/**
 * How often a running conversation's questions are read for a notification.
 * It shares the tab's clock for that conversation, so one on screen is read
 * at the page's pace anyway.
 */
export const NOTIFICATION_QUESTION_POLL_MS = QUESTION_BACKGROUND_POLL_MS;
export const NOTIFICATION_STATE_POLL_MS = 10_000;
const TERMINAL_STATES = new Set(['succeeded', 'failed', 'cancelled']);
const RECEIPT_LIMIT = 200;

function isRejectedChangeCursor(cause: unknown, cursor: string | undefined): boolean {
  return !!cursor && cause instanceof APIError && cause.statusCode === 422;
}

export interface AgentNotice {
  key: string;
  kind: 'question' | 'succeeded' | 'failed' | 'cancelled';
  jobId: string;
  attemptId: number | null;
  title: string;
}

const receiptStorageKey = (userId: string) =>
  `agents.notifications.receipts.${encodeURIComponent(userId)}`;

/**
 * A title for a conversation the notice could not look up: its own, else its
 * repository, else — a temporary workspace has none — its first line.
 */
type WatchedJob = AgentJobApi | AgentNotificationJobApi;

function fallbackTitle(job: WatchedJob): string {
  return (
    job.title ||
    job.repo ||
    ('task_prompt' in job ? job.task_prompt.split('\n')[0].slice(0, 80) : 'Agent task')
  );
}

/** A turn that can be waiting on its owner's answer: running, not stopping, with an attempt. */
function asking(job: WatchedJob | undefined): job is WatchedJob {
  return job?.state === 'running' && !job.cancel_requested && job.current_attempt_id !== null;
}

function readReceipts(userId: string): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(receiptStorageKey(userId)) ?? '[]');
    return Array.isArray(value)
      ? value.filter((key): key is string => typeof key === 'string').slice(-RECEIPT_LIMIT)
      : [];
  } catch {
    return [];
  }
}

/** Watch the owner's chats even when their sidebar folders or detail pages are closed. */
export function useAgentNotifications(
  userId: string | undefined,
  preferences: NotificationPreferences,
  notify: (notice: AgentNotice) => boolean,
): void {
  const questionReads = useQuestionReads();
  const notifyRef = useRef(notify);
  notifyRef.current = notify;
  const owner = useRef(userId);
  owner.current = userId;

  useEffect(() => {
    if (!userId || !preferences.enabled || (!preferences.questions && !preferences.turns)) return;
    let disposed = false;
    const current = () => !disposed && owner.current === userId;
    const controller = new AbortController();
    const jobs = new Map<string, WatchedJob>();
    const titles = new Map<string, string>();
    const threadRevisions = new Map<string, number>();
    const latestTurns = new Map<string, number>();
    const revisions = new Map<string, number>();
    const reads = new Map<string, number>();
    const pendingReads = new Map<string, number>();
    const unconfirmedChanges = new Map<string, number | undefined>();
    const deletedJobs = new Set<string>();
    const deletedThreads = new Set<string>();
    const receipts = new Set(readReceipts(userId));
    const pendingNotices = new Set<string>();
    const readingQuestions = new Set<string>();
    /** The conversations whose questions this watcher reads, and how to stop. */
    const watchedQuestions = new Map<string, () => void>();
    let refreshing = false;
    let refreshAgain = false;
    let lastRefresh = 0;
    let changeCursor: string | undefined;
    let notificationEpoch: number | undefined;

    function deliver(notice: AgentNotice): void {
      if (!current() || receipts.has(notice.key) || pendingNotices.has(notice.key)) return;
      pendingNotices.add(notice.key);
      const send = () => {
        if (!current()) return;
        const job = jobs.get(notice.jobId);
        if (!job || job.current_attempt_id !== notice.attemptId) return;
        if (
          notice.kind === 'question' &&
          (job.state !== 'running' ||
            job.cancel_requested ||
            unconfirmedChanges.has(job.id) ||
            !job.thread_id ||
            latestForThread(job.thread_id)?.id !== job.id)
        )
          return;
        if (notice.kind !== 'question' && !TERMINAL_STATES.has(job.state)) return;
        // Other tabs share receipts. Web Locks make claiming one atomic where supported.
        for (const key of readReceipts(userId!)) receipts.add(key);
        if (receipts.has(notice.key)) {
          if (notice.kind !== 'question') jobs.delete(notice.jobId);
          return;
        }
        if (!notifyRef.current(notice)) return;
        receipts.add(notice.key);
        if (notice.kind !== 'question' && jobs.get(notice.jobId) === job) jobs.delete(notice.jobId);
        try {
          localStorage.setItem(
            receiptStorageKey(userId!),
            JSON.stringify([...receipts].slice(-RECEIPT_LIMIT)),
          );
        } catch {
          // This tab still deduplicates when browser storage is unavailable.
        }
      };
      const locks = globalThis.navigator?.locks;
      const sent = locks
        ? locks.request(receiptStorageKey(userId!), send)
        : Promise.resolve().then(send);
      void sent.catch(() => {}).finally(() => pendingNotices.delete(notice.key));
    }

    function acceptTitle(threadId: string, title: string, revision?: number): void {
      if (deletedThreads.has(threadId)) return;
      const held = threadRevisions.get(threadId);
      if (revision === undefined) {
        if (held !== undefined) return;
      } else {
        if (held !== undefined && revision < held) return;
        threadRevisions.set(threadId, revision);
      }
      titles.set(threadId, title);
    }

    function accept(job: WatchedJob): void {
      if (!current()) return;
      if (deletedJobs.has(job.id) || (job.thread_id && deletedThreads.has(job.thread_id))) {
        unconfirmedChanges.delete(job.id);
        return;
      }
      const pushedRevision = unconfirmedChanges.get(job.id);
      if (pushedRevision !== undefined && (job.revision ?? -1) < pushedRevision) return;
      const previous = jobs.get(job.id);
      if (job.revision !== undefined) {
        if (job.revision < (revisions.get(job.id) ?? -1)) return;
        revisions.set(job.id, job.revision);
      }
      jobs.set(job.id, {
        ...job,
        // Conversation lists omit this telemetry even after a detail read.
        finished_at:
          job.finished_at ??
          (previous?.current_attempt_id === job.current_attempt_id
            ? (previous.finished_at ?? null)
            : null),
      });
      unconfirmedChanges.delete(job.id);
      if (job.thread_id && job.title)
        acceptTitle(
          job.thread_id,
          job.title,
          'thread_revision' in job ? job.thread_revision : undefined,
        );
      if (job.thread_id)
        latestTurns.set(
          job.thread_id,
          Math.max(latestTurns.get(job.thread_id) ?? 0, job.turn_no ?? 1),
        );
      if (TERMINAL_STATES.has(job.state)) acceptTerminal(job, previous);
    }

    function acceptTerminal(job: WatchedJob, previous: WatchedJob | undefined): void {
      const key = JSON.stringify(['turn', job.id, job.current_attempt_id]);
      // An asynchronous Web Lock delivery still needs its row until it sends.
      if (pendingNotices.has(key)) return;
      const transitioned = previous && isActiveJobState(previous.state);
      const inherited = 'forked_from_job_id' in job && job.forked_from_job_id;
      if (!preferences.turns || receipts.has(key) || (inherited && !transitioned)) {
        jobs.delete(job.id);
        return;
      }
      if (!transitioned && notificationEpoch === undefined) return;
      const completedAt = Date.parse(job.finished_at ?? job.updated_at ?? '');
      const newlyFinished =
        (!previous ||
          previous.current_attempt_id !== job.current_attempt_id ||
          !previous.finished_at) &&
        notificationEpoch !== undefined &&
        Number.isFinite(completedAt) &&
        completedAt >= notificationEpoch;
      if (!transitioned && !newlyFinished) {
        jobs.delete(job.id);
        return;
      }
      deliver({
        key,
        kind: job.state as AgentNotice['kind'],
        jobId: job.id,
        attemptId: job.current_attempt_id,
        title: (job.thread_id && titles.get(job.thread_id)) || fallbackTitle(job),
      });
    }

    function establishNotificationEpoch(observedAt: string): void {
      if (notificationEpoch !== undefined) return;
      const observed = Date.parse(observedAt);
      if (!Number.isFinite(observed)) throw new Error('Notification state omitted its clock');
      // The server samples this baseline when it receives the bootstrap request.
      // Completion timestamps share that clock, so neither browser skew nor a
      // delayed response can move old work into the post-enable window.
      notificationEpoch = observed;
      for (const job of [...jobs.values()])
        if (TERMINAL_STATES.has(job.state)) acceptTerminal(job, undefined);
    }

    function forgetJob(id: string): void {
      deletedJobs.add(id);
      reads.set(id, (reads.get(id) ?? 0) + 1);
      pendingReads.delete(id);
      unconfirmedChanges.delete(id);
      jobs.delete(id);
    }

    function forgetThread(id: string): void {
      deletedThreads.add(id);
      for (const job of jobs.values()) if (job.thread_id === id) forgetJob(job.id);
      titles.delete(id);
      latestTurns.delete(id);
    }

    async function readJob(id: string): Promise<void> {
      const version = (reads.get(id) ?? 0) + 1;
      reads.set(id, version);
      pendingReads.set(id, version);
      let settled = false;
      try {
        const job = await getAgentJob(id);
        if (current() && reads.get(id) === version) {
          accept(job);
          settled = true;
        }
      } catch (cause) {
        if (
          current() &&
          reads.get(id) === version &&
          cause instanceof APIError &&
          cause.statusCode === 404
        ) {
          forgetJob(id);
          settled = true;
        }
        // Other failures stay unconfirmed for the next successful reconciliation.
      } finally {
        if (pendingReads.get(id) === version) pendingReads.delete(id);
      }
      // Outside the fence above. A turn this read found running in a
      // conversation not yet watched has its questions read at once; one
      // already watched is read on its clock's next tick. A turn it found
      // deleted no longer keeps its conversation watched.
      if (settled) watchQuestions();
    }

    async function retryUnconfirmed(retried: Set<string>): Promise<void> {
      const ids = [...unconfirmedChanges.keys()].filter(
        (id) => !retried.has(id) && !pendingReads.has(id),
      );
      // Only unresolved pushes / previously active reset jobs need full reads.
      for (let offset = 0; current() && offset < ids.length; offset += 4) {
        await Promise.all(
          ids.slice(offset, offset + 4).map((id) => {
            retried.add(id);
            return readJob(id);
          }),
        );
      }
    }

    function prepareReset(): void {
      const retained = [...jobs.values()].filter(
        (job) =>
          isActiveJobState(job.state) ||
          pendingNotices.has(JSON.stringify(['turn', job.id, job.current_attempt_id])),
      );
      for (const id of new Set([...jobs.keys(), ...reads.keys()]))
        reads.set(id, (reads.get(id) ?? 0) + 1);
      pendingReads.clear();
      jobs.clear();
      titles.clear();
      threadRevisions.clear();
      revisions.clear();
      for (const job of retained) {
        jobs.set(job.id, job);
        if (job.revision !== undefined) revisions.set(job.id, job.revision);
        unconfirmedChanges.set(job.id, job.revision);
      }
    }

    /**
     * Whether a question read begun for `job` at `readVersion` may still alert:
     * no newer turn, job read or push has replaced what it was read for.
     */
    function stillAsking(threadId: string, job: WatchedJob, readVersion: number): boolean {
      const held = jobs.get(job.id);
      return (
        current() &&
        latestForThread(threadId)?.id === job.id &&
        (reads.get(job.id) ?? 0) === readVersion &&
        !unconfirmedChanges.has(job.id) &&
        asking(held) &&
        held.current_attempt_id === job.current_attempt_id
      );
    }

    async function readQuestions(threadId: string): Promise<void> {
      const job = latestForThread(threadId);
      if (
        !preferences.questions ||
        !current() ||
        !asking(job) ||
        readingQuestions.has(threadId) ||
        pendingReads.has(job.id) ||
        unconfirmedChanges.has(job.id)
      )
        return;
      readingQuestions.add(threadId);
      const readVersion = reads.get(job.id) ?? 0;
      try {
        const result = await questionReads.listThreadQuestions(threadId, controller.signal);
        if (
          !stillAsking(threadId, job, readVersion) ||
          result.job_id !== job.id ||
          result.attempt_id !== job.current_attempt_id
        )
          return;
        for (const request of result.questions) {
          deliver({
            key: JSON.stringify([
              'question',
              threadId,
              job.id,
              result.attempt_id,
              request.request_id,
            ]),
            kind: 'question',
            jobId: job.id,
            attemptId: job.current_attempt_id,
            title: titles.get(threadId) || fallbackTitle(job),
          });
        }
      } catch {
        // Hosts can be temporarily unavailable; keep the next poll alive.
      } finally {
        readingQuestions.delete(threadId);
      }
    }

    /**
     * Watch the questions of each conversation whose newest turn is running,
     * on the tab's clock for that conversation, and stop watching the rest.
     * A notification is for an owner who is not looking, so these reads go on
     * while the tab is hidden.
     */
    function watchQuestions(): void {
      const running = new Set<string>();
      if (preferences.questions && current()) {
        for (const job of jobs.values()) {
          const threadId = job.thread_id;
          if (threadId && asking(job) && latestForThread(threadId)?.id === job.id)
            running.add(threadId);
        }
      }
      for (const [threadId, leave] of watchedQuestions) {
        if (running.has(threadId)) continue;
        watchedQuestions.delete(threadId);
        leave();
      }
      for (const threadId of running) {
        if (watchedQuestions.has(threadId)) continue;
        watchedQuestions.set(
          threadId,
          questionReads.watch(
            threadId,
            { intervalMs: NOTIFICATION_QUESTION_POLL_MS, whileHidden: true },
            () => void readQuestions(threadId),
          ),
        );
      }
    }

    function latestForThread(threadId: string): WatchedJob | undefined {
      let latest: WatchedJob | undefined;
      for (const job of jobs.values()) {
        if (job.thread_id === threadId && (!latest || (job.turn_no ?? 1) > (latest.turn_no ?? 1)))
          latest = job;
      }
      return latest && (latest.turn_no ?? 1) === latestTurns.get(threadId) ? latest : undefined;
    }

    async function refresh(): Promise<void> {
      if (!current()) return;
      if (refreshing) {
        refreshAgain = true;
        return;
      }
      refreshing = true;
      try {
        let cursor = changeCursor;
        let resetOnce = false;
        const retried = new Set<string>();
        while (current()) {
          let result: AgentNotificationStateApi;
          try {
            result = await getAgentNotificationState(cursor);
          } catch (cause) {
            if (!isRejectedChangeCursor(cause, cursor) || resetOnce) throw cause;
            resetOnce = true;
            prepareReset();
            await retryUnconfirmed(retried);
            changeCursor = undefined;
            cursor = undefined;
            continue;
          }
          if (!current()) return;
          establishNotificationEpoch(result.observed_at);
          if (result.reset_required) {
            if (resetOnce) throw new Error('Notification state could not be rebuilt');
            resetOnce = true;
            prepareReset();
            await retryUnconfirmed(retried);
            changeCursor = undefined;
            cursor = undefined;
            continue;
          }
          for (const id of result.deleted_job_ids) forgetJob(id);
          for (const id of result.deleted_thread_ids) forgetThread(id);
          for (const thread of result.threads)
            acceptTitle(thread.id, thread.title, thread.revision);
          for (const job of result.jobs) accept(job);
          cursor = result.next_cursor ?? undefined;
          if (cursor) continue;
          if (!result.change_cursor) throw new Error('Notification state omitted its cursor');
          changeCursor = result.change_cursor;
          await retryUnconfirmed(retried);
          if (!current()) return;
          lastRefresh = Date.now();
          watchQuestions();
          break;
        }
      } catch {
        // A failed bootstrap must not turn old history into completion alerts.
      } finally {
        refreshing = false;
        if (refreshAgain && current()) {
          refreshAgain = false;
          void refresh();
        }
      }
    }

    const unsubscribe = onAgentRecordsChanged((change) => {
      if (!current()) return;
      for (const thread of change.threads) {
        if (thread.deleted) {
          forgetThread(thread.id);
        } else if (thread.title) acceptTitle(thread.id, thread.title, thread.revision);
      }
      for (const job of change.jobs) {
        if (job.deleted) {
          forgetJob(job.id);
        } else {
          unconfirmedChanges.set(job.id, job.revision);
          // readJob advances the generation immediately, fencing pending questions
          // before the full job read (which includes the attempt ID) returns.
          void readJob(job.id);
        }
      }
      watchQuestions();
      if (change.resync) void refresh();
    });
    void refresh();
    const stateTimer = setInterval(() => {
      const interval = isAgentPushLive() ? PUSH_FALLBACK_POLL_MS : NOTIFICATION_STATE_POLL_MS;
      if (Date.now() - lastRefresh >= interval) void refresh();
    }, NOTIFICATION_STATE_POLL_MS);
    return () => {
      disposed = true;
      controller.abort();
      unsubscribe();
      for (const leave of watchedQuestions.values()) leave();
      watchedQuestions.clear();
      clearInterval(stateTimer);
    };
  }, [userId, preferences.enabled, preferences.questions, preferences.turns, questionReads]);
}

export function AgentNotifications() {
  const { state } = useAuth();
  const [preferences] = useNotificationPreferences(state.user?.id);
  const [allowed, setAllowed] = useState(false);
  useEffect(() => {
    const sync = () =>
      setAllowed(typeof Notification === 'function' && Notification.permission === 'granted');
    sync();
    window.addEventListener('focus', sync);
    window.addEventListener(NOTIFICATION_PERMISSION_EVENT, sync);
    document.addEventListener('visibilitychange', sync);
    return () => {
      window.removeEventListener('focus', sync);
      window.removeEventListener(NOTIFICATION_PERMISSION_EVENT, sync);
      document.removeEventListener('visibilitychange', sync);
    };
  }, [preferences.enabled]);
  const router = useRouter();
  const t = useT();
  const currentOwner = useRef(state.user?.id);
  currentOwner.current = state.user?.id;
  const activeNotifications = useRef(new Set<Notification>());
  useEffect(() => {
    const active = activeNotifications.current;
    return () => {
      for (const notification of active) notification.close();
      active.clear();
    };
  }, [state.user?.id, preferences.enabled, allowed]);
  useAgentNotifications(
    state.user?.id,
    { ...preferences, enabled: preferences.enabled && allowed },
    (notice) => {
      if (typeof Notification === 'undefined' || Notification.permission !== 'granted')
        return false;
      try {
        const notification = new Notification(t(`notifications.${notice.kind}`), {
          body: notice.title,
          tag: notice.key,
        });
        const recipient = state.user?.id;
        activeNotifications.current.add(notification);
        notification.onclose = () => activeNotifications.current.delete(notification);
        notification.onclick = () => {
          const canNavigate =
            recipient &&
            currentOwner.current === recipient &&
            activeNotifications.current.has(notification);
          notification.close();
          if (!canNavigate) return;
          window.focus();
          router.push(`/${encodeURIComponent(notice.jobId)}`);
        };
        return true;
      } catch {
        // Some browsers expose the API but cannot construct desktop notifications.
        return false;
      }
    },
  );
  return null;
}

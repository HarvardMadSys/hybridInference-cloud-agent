'use client';

import { useEffect, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import { useAuth, useT } from '@/components/providers';
import { getAgentJob, listAgentConversations, type AgentJobApi } from '@/lib/api/agents';
import { useQuestionReads } from './AgentReadProvider';
import { onAgentRecordsChanged, isAgentPushLive, PUSH_FALLBACK_POLL_MS } from './agentPush';
import {
  NOTIFICATION_PERMISSION_EVENT,
  useNotificationPreferences,
  type NotificationPreferences,
} from './notificationPreferences';
import { isActiveJobState } from './conversationJob';

export const NOTIFICATION_QUESTION_POLL_MS = 2_000;
export const NOTIFICATION_STATE_POLL_MS = 10_000;
const TERMINAL_STATES = new Set(['succeeded', 'failed', 'cancelled']);
const RECEIPT_LIMIT = 200;

export interface AgentNotice {
  key: string;
  kind: 'question' | 'succeeded' | 'failed' | 'cancelled';
  jobId: string;
  attemptId: number | null;
  title: string;
}

const receiptStorageKey = (userId: string) =>
  `agents.notifications.receipts.${encodeURIComponent(userId)}`;

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
    const startedAt = Date.now();
    const jobs = new Map<string, AgentJobApi>();
    const titles = new Map<string, string>();
    const reads = new Map<string, number>();
    const pendingReads = new Map<string, number>();
    const unconfirmedChanges = new Map<string, number | undefined>();
    const deletedJobs = new Set<string>();
    const deletedThreads = new Set<string>();
    const receipts = new Set(readReceipts(userId));
    const pendingNotices = new Set<string>();
    const readingQuestions = new Set<string>();
    let refreshing = false;
    let refreshAgain = false;
    let lastRefresh = 0;

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
        if (receipts.has(notice.key) || !notifyRef.current(notice)) return;
        receipts.add(notice.key);
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

    function accept(job: AgentJobApi): void {
      if (
        !current() ||
        deletedJobs.has(job.id) ||
        (job.thread_id && deletedThreads.has(job.thread_id))
      )
        return;
      const pushedRevision = unconfirmedChanges.get(job.id);
      if (pushedRevision !== undefined && (job.revision ?? -1) < pushedRevision) return;
      const previous = jobs.get(job.id);
      if (
        previous?.revision !== undefined &&
        job.revision !== undefined &&
        job.revision < previous.revision
      )
        return;
      jobs.set(job.id, {
        ...job,
        // Conversation lists omit this telemetry even after a detail read.
        finished_at:
          job.finished_at ??
          (previous?.current_attempt_id === job.current_attempt_id ? previous.finished_at : null),
      });
      unconfirmedChanges.delete(job.id);
      if (job.thread_id && job.title) titles.set(job.thread_id, job.title);
      if (!preferences.turns || !TERMINAL_STATES.has(job.state)) return;
      const transitioned = previous && isActiveJobState(previous.state);
      // A short turn may finish between list reads or between batched pushes.
      // Only a newly finished attempt can alert without an observed active state.
      const newlyFinished =
        (!previous ||
          previous.current_attempt_id !== job.current_attempt_id ||
          !previous.finished_at) &&
        Date.parse(job.finished_at ?? '') >= startedAt;
      if (!transitioned && !newlyFinished) return;
      deliver({
        key: JSON.stringify(['turn', job.id, job.current_attempt_id]),
        kind: job.state as AgentNotice['kind'],
        jobId: job.id,
        attemptId: job.current_attempt_id,
        title: (job.thread_id && titles.get(job.thread_id)) || job.title || job.repo,
      });
    }

    async function readJob(id: string): Promise<void> {
      const version = (reads.get(id) ?? 0) + 1;
      reads.set(id, version);
      pendingReads.set(id, version);
      try {
        const job = await getAgentJob(id);
        if (current() && reads.get(id) === version) accept(job);
      } catch {
        // The next resync/poll repairs a missed read; auth expiry unmounts the watcher.
      } finally {
        if (pendingReads.get(id) === version) pendingReads.delete(id);
      }
    }

    async function questions(): Promise<void> {
      if (!preferences.questions || !current()) return;
      const latest = new Map<string, AgentJobApi>();
      for (const job of jobs.values()) {
        if (!job.thread_id) continue;
        const held = latest.get(job.thread_id);
        if (!held || (job.turn_no ?? 1) > (held.turn_no ?? 1)) latest.set(job.thread_id, job);
      }
      await Promise.allSettled(
        [...latest.values()].map(async (job) => {
          const threadId = job.thread_id!;
          if (
            job.state !== 'running' ||
            job.cancel_requested ||
            job.current_attempt_id === null ||
            readingQuestions.has(threadId) ||
            pendingReads.has(job.id) ||
            unconfirmedChanges.has(job.id)
          )
            return;
          readingQuestions.add(threadId);
          const readVersion = reads.get(job.id) ?? 0;
          try {
            const result = await questionReads.listThreadQuestions(threadId, controller.signal);
            const held = jobs.get(job.id);
            if (
              !current() ||
              latestForThread(threadId)?.id !== job.id ||
              (reads.get(job.id) ?? 0) !== readVersion ||
              unconfirmedChanges.has(job.id) ||
              held?.state !== 'running' ||
              held.cancel_requested ||
              held.current_attempt_id !== job.current_attempt_id ||
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
                title: titles.get(threadId) || job.title || job.repo,
              });
            }
          } catch {
            // Hosts can be temporarily unavailable; keep the next poll alive.
          } finally {
            readingQuestions.delete(threadId);
          }
        }),
      );
    }

    function latestForThread(threadId: string): AgentJobApi | undefined {
      let latest: AgentJobApi | undefined;
      for (const job of jobs.values()) {
        if (job.thread_id === threadId && (!latest || (job.turn_no ?? 1) > (latest.turn_no ?? 1)))
          latest = job;
      }
      return latest;
    }

    async function refresh(): Promise<void> {
      if (!current()) return;
      if (refreshing) {
        refreshAgain = true;
        return;
      }
      refreshing = true;
      try {
        const listed = new Set<string>();
        let cursor: string | undefined;
        do {
          const result = await listAgentConversations({
            includeArchived: true,
            limit: 200,
            cursor,
          });
          if (!current()) return;
          for (const row of result.conversations) {
            listed.add(row.job.id);
            if (row.job.thread_id) titles.set(row.job.thread_id, row.title);
            accept({ ...row.job, title: row.title });
            // Lists omit attempt telemetry. Confirm a recently changed terminal
            // row to catch a short turn that never appeared in an active list.
            if (
              TERMINAL_STATES.has(row.job.state) &&
              !jobs.get(row.job.id)?.finished_at &&
              Date.parse(row.job.updated_at ?? '') >= startedAt
            )
              void readJob(row.job.id);
          }
          cursor = result.next_cursor ?? undefined;
        } while (cursor);
        // A new queued turn can replace a still-finishing one in the conversation list.
        await Promise.allSettled(
          [...jobs.values()]
            .filter((job) => isActiveJobState(job.state) && !listed.has(job.id))
            .map((job) => readJob(job.id)),
        );
        if (current()) {
          lastRefresh = Date.now();
          void questions();
        }
      } catch {
        // A failed first scan must not turn old history into completion alerts.
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
          deletedThreads.add(thread.id);
          for (const job of jobs.values()) if (job.thread_id === thread.id) jobs.delete(job.id);
          titles.delete(thread.id);
        } else if (thread.title) titles.set(thread.id, thread.title);
      }
      for (const job of change.jobs) {
        if (job.deleted) {
          deletedJobs.add(job.id);
          reads.set(job.id, (reads.get(job.id) ?? 0) + 1);
          jobs.delete(job.id);
        } else {
          unconfirmedChanges.set(job.id, job.revision);
          // readJob advances the generation immediately, fencing pending questions
          // before the full job read (which includes the attempt ID) returns.
          void readJob(job.id);
        }
      }
      if (change.resync) void refresh();
    });
    void refresh();
    const questionTimer = setInterval(() => void questions(), NOTIFICATION_QUESTION_POLL_MS);
    const stateTimer = setInterval(() => {
      const interval = isAgentPushLive() ? PUSH_FALLBACK_POLL_MS : NOTIFICATION_STATE_POLL_MS;
      if (Date.now() - lastRefresh >= interval) void refresh();
    }, NOTIFICATION_STATE_POLL_MS);
    return () => {
      disposed = true;
      controller.abort();
      unsubscribe();
      clearInterval(questionTimer);
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

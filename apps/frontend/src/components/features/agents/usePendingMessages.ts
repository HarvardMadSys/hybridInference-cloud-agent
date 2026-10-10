import { useCallback, useEffect, useRef, useState } from 'react';

import {
  conflictCurrent,
  deletePendingMessage,
  editPendingMessage,
  getThreadQueue,
  resumeThreadQueue,
  sendPendingMessageNow,
  type PendingMessageApi,
  type QueuePausedReason,
  type ThreadMessageResultApi,
  type ThreadQueueApi,
} from '@/lib/api/pendingMessages';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import { onAgentRecordsChanged, PUSH_FALLBACK_POLL_MS, useAgentPushLive } from './agentPush';

/**
 * How often a page with messages waiting reads the queue again.
 *
 * The queue is where a message becomes a turn without the browser asking, so
 * this read is how the page learns it happened.
 */
export const PENDING_QUEUE_POLL_MS = 2000;

/**
 * How often a page with a live turn and nothing waiting reads it.
 *
 * Only another tab can add a message then, so it is read less often; a
 * conversation with nothing running and nothing waiting is not read at all.
 */
export const IDLE_QUEUE_POLL_MS = 5000;

/** What one queue action answered, or why it did not. */
export type QueueActionResult =
  | { ok: true; result: ThreadMessageResultApi }
  | { ok: false; code: string; error: string; current: PendingMessageApi | null };

/**
 * How a message came to be a turn, for the page deciding whether to follow it.
 *
 * ``requested`` is a turn this page's own action started — a send, a save, a
 * continue — which the page moves to as it always has. Otherwise the page
 * learned it from a read; ``after`` is the turn that was newest before it, so
 * the page can follow only when the reader was looking at the conversation's
 * tip rather than at an older turn they chose.
 */
export interface SentTurn {
  requested: boolean;
  after: string | null;
}

export interface PendingQueue {
  /** Waiting messages, in the order they will be sent. */
  messages: PendingMessageApi[];
  /** Why the queue is not moving by itself; only the owner's continue clears it. */
  pausedReason: QueuePausedReason | null;
  /** True once the first read has answered. */
  loaded: boolean;
  /** Messages with a write in flight, so their card can wait. */
  busyIds: ReadonlySet<string>;
  resuming: boolean;
  /** The live turn can take a waiting message before its next tool call. */
  canSendNow: boolean;
  /** Fold a send's answer in, so its card appears without waiting for a read. */
  apply: (result: ThreadMessageResultApi) => void;
  /** Stop the line at this message while it is rewritten. */
  hold: (message: PendingMessageApi) => Promise<QueueActionResult>;
  /** Put a held message back in line, unchanged. */
  release: (message: PendingMessageApi) => Promise<QueueActionResult>;
  /** Rewrite a message and put it back in line. */
  save: (message: PendingMessageApi, prompt: string) => Promise<QueueActionResult>;
  remove: (message: PendingMessageApi) => Promise<QueueActionResult>;
  /**
   * Hand a waiting message to the running turn. Taken, it leaves the queue for
   * that turn's transcript; not taken, it stays in line and the result says why.
   */
  sendNow: (message: PendingMessageApi) => Promise<QueueActionResult>;
  resume: () => Promise<QueueActionResult>;
  refresh: () => Promise<void>;
}

function failure(cause: unknown): Extract<QueueActionResult, { ok: false }> {
  return {
    ok: false,
    code: cause instanceof APIError ? cause.code : 'unknown_error',
    error: getErrorMessage(cause),
    current: conflictCurrent(cause),
  };
}

function upsert(messages: PendingMessageApi[], message: PendingMessageApi): PendingMessageApi[] {
  const index = messages.findIndex((row) => row.id === message.id);
  const next =
    index < 0 ? [...messages, message] : messages.map((row, at) => (at === index ? message : row));
  return next.sort((a, b) => a.position - b.position);
}

/**
 * A conversation's waiting messages.
 *
 * Three rules, each a way the owner could otherwise lose or misread what they
 * wrote:
 *
 * * **A slow read never overwrites a newer one.** Reads are numbered; an answer
 *   to an older request than the one already applied is dropped.
 * * **A conflict is never retried.** A write made against a revision somebody
 *   else moved past is refused by the server with the message as it is now,
 *   and that is what the card shows; the caller keeps the owner's draft.
 * * **The page is told when a message became a turn.** Nothing in the browser
 *   sends a waiting message — a turn ending does, on the server — so a message
 *   leaving the queue while the conversation's newest turn changes is reported
 *   through ``onSent``, with the turn it became.
 */
export function usePendingMessages({
  threadId,
  active,
  onSent,
}: {
  threadId: string | null | undefined;
  /** A turn of this conversation is queued or running, so the queue can move. */
  active: boolean;
  onSent?: (jobId: string, how: SentTurn) => void;
}): PendingQueue {
  const [messages, setMessages] = useState<PendingMessageApi[]>([]);
  const [pausedReason, setPausedReason] = useState<QueuePausedReason | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busyIds, setBusyIds] = useState<ReadonlySet<string>>(() => new Set<string>());
  const [resuming, setResuming] = useState(false);
  const [canSendNow, setCanSendNow] = useState(false);

  const mounted = useRef(true);
  const threadRef = useRef(threadId);
  const readSeq = useRef(0);
  const appliedSeq = useRef(0);
  /** The last snapshot applied from a read, for noticing a message that became a turn. */
  const snapshot = useRef<{ ids: Set<string>; latestJobId: string | null } | null>(null);
  /** Messages this page removed itself, which did not become turns. */
  const removedHere = useRef(new Set<string>());
  /** Turns already reported, so a send and the read after it report once. */
  const announced = useRef(new Set<string>());
  const onSentRef = useRef(onSent);
  onSentRef.current = onSent;
  const announce = useCallback((jobId: string, how: SentTurn) => {
    if (announced.current.has(jobId)) return;
    announced.current.add(jobId);
    onSentRef.current?.(jobId, how);
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const applyRead = useCallback(
    (queue: ThreadQueueApi, seq: number) => {
      if (queue.thread_id !== threadRef.current) return;
      if (seq < appliedSeq.current) return;
      appliedSeq.current = seq;
      const ids = new Set(queue.pending_messages.map((row) => row.id));
      const previous = snapshot.current;
      snapshot.current = { ids, latestJobId: queue.latest_job_id };
      setMessages([...queue.pending_messages].sort((a, b) => a.position - b.position));
      setPausedReason(queue.queue_paused_reason ?? null);
      // A turn still waiting for a host can take one too: it is given the
      // message when it starts.
      setCanSendNow(
        queue.mid_turn_input_supported === true &&
          (queue.latest_job_state === 'running' || queue.latest_job_state === 'queued'),
      );
      setLoaded(true);
      if (
        previous &&
        queue.latest_job_id &&
        queue.latest_job_id !== previous.latestJobId &&
        [...previous.ids].some((id) => !ids.has(id) && !removedHere.current.has(id))
      ) {
        announce(queue.latest_job_id, { requested: false, after: previous.latestJobId });
      }
    },
    [announce],
  );

  const refresh = useCallback(async () => {
    const target = threadRef.current;
    if (!target) return;
    const seq = ++readSeq.current;
    try {
      const queue = await getThreadQueue(target);
      if (mounted.current) applyRead(queue, seq);
    } catch {
      // A failed read keeps what is on screen; the next one tries again.
    }
  }, [applyRead]);

  // Another conversation's queue is not this one's.
  useEffect(() => {
    threadRef.current = threadId;
    snapshot.current = null;
    removedHere.current = new Set();
    announced.current = new Set();
    readSeq.current += 1;
    appliedSeq.current = readSeq.current;
    setMessages([]);
    setPausedReason(null);
    setCanSendNow(false);
    setLoaded(false);
    setBusyIds(new Set<string>());
    if (threadId) void refresh();
  }, [refresh, threadId]);

  const [inFlight, setInFlight] = useState(0);
  const waiting = messages.length > 0 || inFlight > 0;
  // While the owner's change stream flows, a change to this queue, to the
  // conversation (its pause), or to one of its turns is pushed and read at
  // once; the clock is then only a backstop.
  const pushLive = useAgentPushLive();
  const interval = !threadId
    ? null
    : waiting || active
      ? pushLive
        ? PUSH_FALLBACK_POLL_MS
        : waiting
          ? PENDING_QUEUE_POLL_MS
          : IDLE_QUEUE_POLL_MS
      : null;
  useEffect(() => {
    if (!threadId) return undefined;
    return onAgentRecordsChanged((change) => {
      if (
        change.resync ||
        change.queues.includes(threadId) ||
        change.threads.some((thread) => thread.id === threadId) ||
        change.jobs.some((job) => job.thread_id === threadId)
      )
        void refresh();
    });
  }, [refresh, threadId]);
  useEffect(() => {
    if (interval === null) return undefined;
    const timer = setInterval(() => void refresh(), interval);
    return () => clearInterval(timer);
  }, [interval, refresh]);

  // A hidden tab's timer may have been frozen; coming back is a reason to read.
  useEffect(() => {
    if (typeof document === 'undefined') return undefined;
    const onVisible = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, [refresh]);

  // A turn starting or ending is when the queue moves; read it then too.
  useEffect(() => {
    if (threadId) void refresh();
  }, [active, refresh, threadId]);

  const apply = useCallback(
    (result: ThreadMessageResultApi) => {
      // Newer than any read already in flight: those were asked before this
      // write happened, and must not put the old queue back.
      appliedSeq.current = ++readSeq.current;
      if (result.pending) {
        const pending = result.pending;
        setMessages((current) => upsert(current, pending));
        // Waiting from now on, as far as the next read is concerned. The turn
        // in front of it can end before any read sees it here, and the read
        // that finds it gone must still know it was one that could be sent.
        snapshot.current = {
          ids: new Set([...(snapshot.current?.ids ?? []), pending.id]),
          latestJobId: snapshot.current?.latestJobId ?? null,
        };
      }
      if (result.queue_paused_reason !== undefined) {
        setPausedReason(result.queue_paused_reason ?? null);
      }
      if (result.job) {
        announce(result.job.id, { requested: true, after: snapshot.current?.latestJobId ?? null });
      }
    },
    [announce],
  );

  const track = useCallback((id: string, busy: boolean) => {
    setBusyIds((current) => {
      if (busy === current.has(id)) return current;
      const next = new Set(current);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  const mutate = useCallback(
    async (
      message: PendingMessageApi,
      send: () => Promise<ThreadMessageResultApi>,
      {
        removing = false,
        optimistic,
      }: { removing?: boolean; optimistic?: Partial<PendingMessageApi> } = {},
    ): Promise<QueueActionResult> => {
      track(message.id, true);
      setInFlight((count) => count + 1);
      if (optimistic) {
        setMessages((current) =>
          current.map((row) => (row.id === message.id ? { ...row, ...optimistic } : row)),
        );
      }
      try {
        const result = await send();
        if (!mounted.current) return { ok: true, result };
        appliedSeq.current = ++readSeq.current;
        if (removing) {
          // Out of line without becoming a turn of its own: withdrawn.
          removedHere.current.add(message.id);
          setMessages((current) => current.filter((row) => row.id !== message.id));
        } else if (result.pending) {
          const pending = result.pending;
          setMessages((current) => upsert(current, pending));
        } else {
          // Saved and sent at once: it is a turn now, not a card.
          setMessages((current) => current.filter((row) => row.id !== message.id));
        }
        if (result.queue_paused_reason !== undefined) {
          setPausedReason(result.queue_paused_reason ?? null);
        }
        if (result.job) {
          announce(result.job.id, {
            requested: true,
            after: snapshot.current?.latestJobId ?? null,
          });
        }
        void refresh();
        return { ok: true, result };
      } catch (cause) {
        const refused = failure(cause);
        if (mounted.current) {
          const current = refused.current;
          if (current) {
            // What won the race is what the card shows now.
            appliedSeq.current = ++readSeq.current;
            setMessages((rows) => upsert(rows, current));
          }
          void refresh();
        }
        return refused;
      } finally {
        if (mounted.current) {
          track(message.id, false);
          setInFlight((count) => Math.max(0, count - 1));
        }
      }
    },
    [announce, refresh, track],
  );

  const hold = useCallback(
    (message: PendingMessageApi) =>
      mutate(message, () =>
        editPendingMessage(String(threadRef.current), message.id, message.revision, {
          state: 'held',
        }),
      ),
    [mutate],
  );
  const release = useCallback(
    (message: PendingMessageApi) =>
      mutate(message, () =>
        editPendingMessage(String(threadRef.current), message.id, message.revision, {
          state: 'queued',
        }),
      ),
    [mutate],
  );
  const save = useCallback(
    (message: PendingMessageApi, prompt: string) =>
      mutate(message, () =>
        editPendingMessage(String(threadRef.current), message.id, message.revision, {
          prompt,
          state: 'queued',
        }),
      ),
    [mutate],
  );
  const remove = useCallback(
    (message: PendingMessageApi) =>
      mutate(
        message,
        () => deletePendingMessage(String(threadRef.current), message.id, message.revision),
        { removing: true },
      ),
    [mutate],
  );
  const sendNow = useCallback(
    (message: PendingMessageApi) =>
      mutate(
        message,
        () => sendPendingMessageNow(String(threadRef.current), message.id, message.revision),
        { optimistic: { state: 'sending' } },
      ),
    [mutate],
  );
  const resume = useCallback(async (): Promise<QueueActionResult> => {
    setResuming(true);
    setInFlight((count) => count + 1);
    try {
      const result = await resumeThreadQueue(String(threadRef.current));
      if (mounted.current) {
        appliedSeq.current = ++readSeq.current;
        setPausedReason(result.queue_paused_reason ?? null);
        if (result.job) {
          announce(result.job.id, {
            requested: true,
            after: snapshot.current?.latestJobId ?? null,
          });
        }
        void refresh();
      }
      return { ok: true, result };
    } catch (cause) {
      if (mounted.current) void refresh();
      return failure(cause);
    } finally {
      if (mounted.current) {
        setResuming(false);
        setInFlight((count) => Math.max(0, count - 1));
      }
    }
  }, [announce, refresh]);

  return {
    messages,
    pausedReason,
    loaded,
    busyIds,
    resuming,
    canSendNow,
    apply,
    hold,
    release,
    save,
    remove,
    sendNow,
    resume,
    refresh,
  };
}

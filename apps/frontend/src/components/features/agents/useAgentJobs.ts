'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  getAgentJob,
  getAgentJobFiles,
  getAgentJobGit,
  listAllAgentThreadEvents,
  listAgentProjects,
  streamAgentJob,
} from '@/lib/api/agents';
import type {
  AgentJobApi,
  AgentJobEventApi,
  AgentJobFileApi,
  AgentJobFileEntryApi,
  AgentJobSymlinkApi,
  AgentProjectApi,
  AgentThreadApi,
  AgentThreadEventApi,
} from '@/lib/api/agents';
import { createDisplayJobAdapter } from './adapt';
import { useAgentReads } from './AgentReadProvider';
import { announceAgentSessionsChanged, onAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { projectKey } from '@/lib/temporaryWorkspace';
import { APIError } from '@/lib/utils/errors';
import type { AgentJob } from './types';
import { isActiveJobState, isActiveConversationState } from './conversationJob';
import { hasActiveGoal } from './goal/liveWork';
import { preserveResolvedTaskTitle, taskTitleNeedsGeneration } from './taskTitles';
import {
  onAgentRecordsChanged,
  overlayPushed,
  PUSH_FALLBACK_POLL_MS,
  useAgentPushLive,
} from './agentPush';

/** First and largest pause before a turn's event stream is opened again. */
export const STREAM_RETRY_MIN_MS = 1_000;
export const STREAM_RETRY_MAX_MS = 30_000;

const ACTIVE_ATTEMPT_PHASES = new Set([
  'started',
  'checked_out',
  'context_restored',
  'setup',
  'workspace_preparing',
  'workspace_ready',
  'workspace_finalizing',
]);

const ACTIVE_WORKSPACE_PUBLICATION_STATES = new Set([
  'pending',
  'capturing',
  'captured',
  'publishing',
]);

/** Whether one shared-workspace publication still changes owner-visible data. */
export function isActiveWorkspacePublicationState(state: unknown): boolean {
  return typeof state === 'string' && ACTIVE_WORKSPACE_PUBLICATION_STATES.has(state);
}

/** Whether a finished shared turn still has owner-visible state to publish. */
export function hasActiveWorkspacePublication(job: AgentJobApi | null): boolean {
  return (
    job?.workspace_shared === true &&
    isActiveWorkspacePublicationState(job.workspace_publication_state)
  );
}

/** Apply one live attempt-state event to the cached owner-facing job row. */
export function applyAgentLifecycleEvent(
  current: AgentJobApi,
  event: AgentJobEventApi,
): AgentJobApi {
  const telemetryAttemptId =
    current.telemetry_attempt_id === undefined
      ? current.current_attempt_id
      : current.telemetry_attempt_id;
  if (event.event_type === 'attempt_superseded') {
    if (current.current_attempt_id !== event.attempt_id) return current;
    return {
      ...current,
      telemetry_attempt_id: telemetryAttemptId,
      state: 'queued',
      conversation_state: 'queued',
      current_attempt_id: null,
    };
  }
  if (event.event_type !== 'lifecycle') return current;
  const phase = event.payload?.phase;
  if (typeof phase !== 'string') return current;
  if (ACTIVE_ATTEMPT_PHASES.has(phase)) {
    return {
      ...current,
      telemetry_attempt_id: telemetryAttemptId,
      state: 'running',
      conversation_state: 'running',
      current_attempt_id: event.attempt_id,
    };
  }
  if (phase === 'publishing') {
    return {
      ...current,
      telemetry_attempt_id: telemetryAttemptId,
      state: 'publishing',
      conversation_state: 'running',
      current_attempt_id: event.attempt_id,
    };
  }
  return current;
}

// Data hooks for the /agents surface.
//
// The API client and the adapter were both complete and tested while the UI
// still rendered fixtures, so this is the wiring between them rather than new
// behaviour. Two things it deliberately does not do: invent values the API does
// not return (the adapter decides that), and treat the stream ending as an
// error — the server's SSE cap is finite by design, so a drop is the expected
// shape and the client resumes from the last event id.

/** A project as the sidebar keys it: a repository, or `TEMPORARY_WORKSPACE`. */
export type SidebarProject = Omit<AgentProjectApi, 'repo'> & { repo: string };

/** The caller's projects, for the sidebar's folder tree. */
export function useAgentProjects(
  archived = false,
  enabled = true,
): {
  projects: SidebarProject[];
  loading: boolean;
  error: string | null;
  reload: () => void;
} {
  const [projects, setProjects] = useState<SidebarProject[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const generation = useRef(0);

  const reload = useCallback(() => {
    generation.current += 1;
    setTick((value) => value + 1);
  }, []);

  useEffect(() => onAgentSessionsChanged(reload), [reload]);

  useEffect(() => {
    let cancelled = false;
    const requestGeneration = generation.current;
    if (!enabled) return;
    listAgentProjects(archived)
      .then((api) => {
        if (cancelled || requestGeneration !== generation.current) return;
        setProjects(api.map((project) => ({ ...project, repo: projectKey(project.repo) })));
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!cancelled && requestGeneration === generation.current)
          setError(cause instanceof Error ? cause.message : 'could not load projects');
      })
      .finally(() => {
        if (!cancelled && requestGeneration === generation.current) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [archived, tick, enabled]);

  return { projects, loading, error, reload };
}

/** One job, its event log, and its patch — kept live over SSE. */
/**
 * Whether a read of the turn's row says that the turn on screen, still active
 * here, has ended. Such a read is not applied: the turn's own event stream ends
 * it, after delivering its last events and `job_finished`, which reloads the
 * conversation. Applying the read would close that stream before them. While
 * that stream is down, such a read reloads the conversation instead.
 */
function endsStreamingTurn(current: AgentJobApi | null, latest: AgentJobApi): boolean {
  return (
    current?.id === latest.id && isActiveJobState(current.state) && !isActiveJobState(latest.state)
  );
}

/**
 * Where to read a conversation again when its earlier log is held.
 *
 * Stored events are never changed or removed, so a later read only has to
 * reach what was added. It does not start after the newest held event: ids
 * are taken before their writes commit, so a write still in flight during
 * the earlier read could land below an id that read already returned. The
 * running turn writes, and so do the snapshot and publication that follow
 * the turn before it, which can still be committing when the next turn
 * starts. The last two held turns are therefore read again in full. A write
 * to an older turn takes an id from after they began, and is read either way.
 */
function resumeAfter(held: readonly AgentThreadEventApi[]): number {
  const turns = [...new Set(held.map((event) => event.turn_no))].sort((a, b) => b - a);
  const from = turns[1] ?? turns[0];
  let first = Infinity;
  for (const event of held) {
    if (event.turn_no >= from && event.id < first) first = event.id;
  }
  return Number.isFinite(first) ? first - 1 : 0;
}

export function useAgentJob(
  jobId: string,
  {
    active = true,
    hideForkedHistory = false,
  }: { active?: boolean; hideForkedHistory?: boolean } = {},
): {
  job: AgentJob | null;
  /** Every turn's stored stream, unfolded — what the trajectory view reads. */
  events: AgentThreadEventApi[];
  /**
   * The conversation's turn rows, as the API returned them.
   *
   * The display model keeps only what Chat renders, and the inspector needs the
   * per-turn execution fields it drops — started/finished, attempts, the exact
   * model. Returning them here avoids a second read of the same thread.
   */
  threadJobs: AgentJobApi[];
  loading: boolean;
  error: string | null;
  reload: () => void;
} {
  const [api, setApi] = useState<AgentJobApi | null>(null);
  const [events, setEvents] = useState<AgentJobEventApi[]>([]);
  const [threadEvents, setThreadEvents] = useState<AgentThreadEventApi[]>([]);
  const [patch, setPatch] = useState<string | null>(null);
  const [thread, setThread] = useState<AgentThreadApi | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const reads = useAgentReads();
  const seen = useRef<Map<number, AgentThreadEventApi>>(new Map());
  const seenScope = useRef({ jobId, reads });
  const historyRead = useRef<{
    jobId: string;
    events: Map<number, AgentThreadEventApi>;
    lifecycleEvents: Map<number, AgentThreadEventApi>;
  } | null>(null);
  const generation = useRef(0);
  // The conversation's log as of its last complete read. A follow-up turn, a
  // turn's end and a stream reconnect read the same conversation again; this
  // lets them read what was added instead of the whole log.
  const heldLog = useRef<{
    threadId: string;
    reads: typeof reads;
    events: AgentThreadEventApi[];
  } | null>(null);
  const loadedStreamCursor = useRef<{
    jobId: string;
    reads: typeof reads;
    cursor: number;
  } | null>(null);

  const reload = useCallback(() => {
    reads.invalidate(jobId);
    generation.current += 1;
    setTick((value) => value + 1);
  }, [jobId, reads]);

  useEffect(
    () =>
      onAgentSessionsChanged((change) => {
        if (!active || !jobId) return;
        // Until this job resolves, its conversation identity is unknown. A job
        // ID cannot be compared with thread IDs, so invalidate any pending load.
        const threadId = api?.id === jobId ? api.thread_id : undefined;
        if (threadId && !change.threadIds.includes(threadId)) return;
        if (threadId && change.titleSource === 'generated' && change.title !== undefined) {
          setApi((current) =>
            current && current.title_source !== 'manual'
              ? { ...current, title: change.title, title_source: 'generated' }
              : current,
          );
          return;
        }
        if (change.deleted) setApi(null);
        reload();
      }),
    [active, api?.id, api?.thread_id, jobId, reload],
  );

  useEffect(() => {
    if (!active || !jobId) return undefined;
    let cancelled = false;
    const controller = new AbortController();
    const requestGeneration = generation.current;
    if (seenScope.current.jobId !== jobId || seenScope.current.reads !== reads) {
      if (seenScope.current.reads !== reads) {
        setApi(null);
        setEvents([]);
        setThreadEvents([]);
        setThread(null);
        heldLog.current = null;
      }
      seen.current = new Map();
      seenScope.current = { jobId, reads };
    }
    const read = {
      jobId,
      events: new Map(seen.current),
      lifecycleEvents: new Map<number, AgentThreadEventApi>(),
    };
    historyRead.current = read;
    setPatch(null);
    setLoading(true);
    const progressive =
      loadedStreamCursor.current?.jobId !== jobId || loadedStreamCursor.current.reads !== reads;
    const publishEvents = (foundEvents: AgentThreadEventApi[]) => {
      // A refresh already has a complete transcript. Keep it and its live
      // appends visible until the replacement snapshot is complete as well.
      if (!progressive || cancelled || requestGeneration !== generation.current) return;
      const merged = new Map(foundEvents.map((event) => [event.id, event]));
      for (const [id, event] of read.events) merged.set(id, event);
      const rows = [...merged.values()].sort((left, right) => left.id - right.id);
      const own = rows.filter((event) => event.job_id === jobId);
      setEvents(own);
      setThreadEvents(rows);
      own.forEach((event) => seen.current.set(event.id, event));
    };

    // The turn's row is read before its events. A turn stores every event
    // before it ends, so events read after a row that says it ended are all
    // of them; read alongside, they could predate an end the row then reports,
    // and the turn would end on screen without its last events.
    Promise.all([reads.getAgentJob(jobId), reads.getAgentJobThread(jobId)])
      .then(async ([job, foundThread]) => {
        if (cancelled || requestGeneration !== generation.current) return null;
        // Durable messages and the prompt do not depend on downloading the
        // activity ledger. Keep a live same-turn row authoritative until the
        // complete snapshot below can end it with all of its final events.
        setApi((current) =>
          current?.id === jobId ? current : preserveResolvedTaskTitle(current, overlayPushed(job)),
        );
        const held =
          foundThread &&
          heldLog.current?.reads === reads &&
          heldLog.current.threadId === foundThread.thread_id
            ? heldLog.current.events
            : [];
        if (progressive && seen.current.size === 0) {
          setEvents(held.filter((event) => event.job_id === jobId));
          setThreadEvents(held);
        }
        setThread(foundThread);
        const added = await listAllAgentThreadEvents(jobId, {
          after: resumeAfter(held),
          signal: controller.signal,
          onPage: (page) => publishEvents([...held, ...page]),
        });
        return [job, foundThread, [...held, ...added]] as const;
      })
      .then((snapshot) => {
        if (!snapshot || cancelled || requestGeneration !== generation.current) return;
        const [job, foundThread, foundEvents] = snapshot;
        // The existing stream stays open during a same-turn reload. Its events
        // can arrive after the history snapshot was taken but before this read
        // returns; replacing state with that snapshot would discard them while
        // the stream's resume cursor has already advanced past them.
        const merged = new Map(foundEvents.map((event) => [event.id, event]));
        const replay = new Map(read.lifecycleEvents);
        for (const [id, event] of read.events) {
          if (!merged.has(id)) replay.set(id, event);
        }
        for (const [id, event] of read.events) merged.set(id, event);
        const completeEvents = [...merged.values()].sort((left, right) => left.id - right.id);
        heldLog.current = foundThread
          ? { threadId: foundThread.thread_id, reads, events: completeEvents }
          : null;
        if (historyRead.current === read) historyRead.current = null;
        const ownEvents = completeEvents.filter((event) => event.job_id === jobId);
        let cursor = 0;
        for (const event of ownEvents) cursor = Math.max(cursor, event.id);
        loadedStreamCursor.current = { jobId, reads, cursor };
        let latest = overlayPushed(job);
        // A queued/running row can also predate the lifecycle frame received
        // during this read. Apply new frames in stream order, while leaving a
        // terminal row and a newer attempt/publication phase authoritative.
        if (isActiveJobState(latest.state)) {
          for (const event of [...replay.values()].sort((a, b) => a.id - b.id)) {
            if (latest.current_attempt_id != null && event.attempt_id < latest.current_attempt_id)
              continue;
            if (
              latest.state === 'publishing' &&
              latest.current_attempt_id === event.attempt_id &&
              event.event_type === 'lifecycle' &&
              ACTIVE_ATTEMPT_PHASES.has(String(event.payload?.phase))
            )
              continue;
            latest = applyAgentLifecycleEvent(latest, event);
          }
        }
        setApi((current) => preserveResolvedTaskTitle(current, latest));
        setEvents(ownEvents);
        setThreadEvents(completeEvents);
        setThread(foundThread);
        ownEvents.forEach((event) => seen.current.set(event.id, event));
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!cancelled && requestGeneration === generation.current)
          setError(cause instanceof Error ? cause.message : 'could not load the job');
      })
      .finally(() => {
        if (historyRead.current === read) historyRead.current = null;
        if (!cancelled && requestGeneration === generation.current) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
      if (historyRead.current === read) historyRead.current = null;
    };
  }, [active, jobId, reads, tick]);

  // Diff state belongs to the retained group workspace, not to a per-turn
  // artifact uploaded by the runner. Fork siblings therefore see the same
  // current workspace diff, matching the Git panel below. Metadata reloads
  // clear the cached patch, so refetch even when the job state stays settled.
  // During an in-place turn change, api still belongs to the previous turn.
  // Do not open the new stream with that turn's attempt or event identity.
  const state = api?.id === jobId ? api.state : undefined;
  useEffect(() => {
    if (!active || !jobId || !state) return undefined;
    let cancelled = false;
    getAgentJobGit(jobId)
      .then((workspace) => {
        if (!cancelled) setPatch(workspace.available ? workspace.patch : null);
      })
      .catch(() => {
        if (!cancelled) setPatch(null);
      });
    return () => {
      cancelled = true;
    };
  }, [active, jobId, state, tick]);

  // Live events. Terminal jobs have nothing more to say, so no stream is
  // opened for them at all.
  // A metadata push can arrive before the initial history read. Subscribe
  // after that snapshot, resuming only from this owner's selected turn, so
  // its stored backlog is not transferred and parsed a second time.
  const loadedCursor = loadedStreamCursor.current;
  const isLive =
    isActiveJobState(state) && loadedCursor?.jobId === jobId && loadedCursor.reads === reads;
  const currentTurnNo =
    api?.turn_no ?? thread?.jobs.find((threadJob) => threadJob.id === jobId)?.turn_no ?? 1;
  // Whether this turn's own stream is open or opening. Only then is the end of
  // the turn left to it; while it is down, a read saying the turn ended reads
  // the conversation whole, which ends it with every event it stored.
  const streaming = useRef(false);
  const streamFailures = useRef(0);
  const [streamAttempt, setStreamAttempt] = useState(0);
  useEffect(() => {
    streamFailures.current = 0;
  }, [jobId]);
  useEffect(() => {
    if (!active || !jobId || !isLive) return undefined;
    const controller = new AbortController();
    let finished = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    // This opening follows one that could not connect, and the read made
    // then may have failed too. Once the server answers again, read the
    // conversation once more rather than leave that failure on screen for
    // the rest of the turn.
    let recovering = streamFailures.current > 0;
    streaming.current = true;
    void streamAgentJob(jobId, {
      signal: controller.signal,
      lastEventId:
        loadedStreamCursor.current?.jobId === jobId && loadedStreamCursor.current.reads === reads
          ? loadedStreamCursor.current.cursor
          : 0,
      onOpen: () => {
        if (!recovering) return;
        recovering = false;
        reload();
      },
      onEvent: (event) => {
        streamFailures.current = 0;
        const threadEvent = { ...event, job_id: jobId, turn_no: currentTurnNo };
        if (historyRead.current?.jobId === jobId) {
          historyRead.current.events.set(event.id, threadEvent);
        }
        // The stream can replay across a reconnect, so dedupe on the global id
        // the server assigns rather than assuming each frame is new.
        if (seen.current.has(event.id)) return;
        seen.current.set(event.id, threadEvent);
        if (historyRead.current?.jobId === jobId) {
          historyRead.current.lifecycleEvents.set(event.id, threadEvent);
        }
        setEvents((current) => [...current, event]);
        setThreadEvents((current) => [...current, threadEvent]);
        // The stream is also the fastest authority for a queued child
        // becoming active; keep the status pill in step without polling.
        setApi((current) => (current ? applyAgentLifecycleEvent(current, event) : current));
      },
      onFinished: () => {
        finished = true;
        streamFailures.current = 0;
        reload();
      },
    })
      .catch(() => undefined)
      .then(() => {
        if (controller.signal.aborted || finished) return;
        // The stream could not connect, or failed (it reconnects by itself
        // after a drop mid-stream). Read the conversation again, which ends
        // the turn if it has ended, and open the stream again after a pause.
        streaming.current = false;
        reload();
        const pause = Math.min(
          STREAM_RETRY_MAX_MS,
          STREAM_RETRY_MIN_MS * 2 ** streamFailures.current,
        );
        streamFailures.current += 1;
        retry = setTimeout(() => setStreamAttempt((attempt) => attempt + 1), pause);
      });
    return () => {
      streaming.current = false;
      controller.abort();
      clearTimeout(retry);
    };
  }, [active, currentTurnNo, jobId, isLive, reads, reload, streamAttempt]);

  // The turn on screen, for the reads below that decide outside a state update.
  const shownApi = useRef<AgentJobApi | null>(null);
  useEffect(() => {
    shownApi.current = api;
  }, [api]);

  // The owner's change stream reports this conversation's title, and any turn
  // of it changing. What it sends is patched in at once; the turn's own row is
  // then read for the fields a push does not carry (detail, goal, attempt).
  const pushLive = useAgentPushLive();
  const pushThreadId = api?.id === jobId ? api.thread_id : undefined;
  useEffect(() => {
    if (!active || !jobId) return undefined;
    let cancelled = false;
    let latestRead = 0;
    const unsubscribe = onAgentRecordsChanged((change) => {
      const touched =
        change.resync ||
        change.jobs.some(
          (job) =>
            job.id === jobId || (pushThreadId !== undefined && job.thread_id === pushThreadId),
        ) ||
        change.threads.some((thread) => thread.id === pushThreadId) ||
        (pushThreadId != null && change.publications.includes(pushThreadId));
      if (!touched) return;
      setApi((current) => (current?.id === jobId ? overlayPushed(current) : current));
      const read = ++latestRead;
      // A reload that starts while this read is out reads the row again, and
      // may already have ended the turn: this older answer must not reopen it.
      const version = generation.current;
      void getAgentJob(jobId)
        .then((latest) => {
          if (cancelled || read !== latestRead || generation.current !== version) return;
          if (!streaming.current && endsStreamingTurn(shownApi.current, latest)) {
            reload();
            return;
          }
          setApi((current) =>
            endsStreamingTurn(current, latest)
              ? current
              : preserveResolvedTaskTitle(current, overlayPushed(latest)),
          );
        })
        .catch(() => {
          // The next change, or the fallback clock below, reads it again.
        });
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [active, jobId, pushThreadId, reload]);

  // A claim may recover an earlier failed title, including on a follow-up.
  // Outlast the server's bounded 120s retry window without waiting for SSE to end.
  // While pushes flow, the generated title arrives with its conversation's row.
  const awaitingTitle =
    api?.id === jobId &&
    taskTitleNeedsGeneration(api) &&
    (state === 'running' || state === 'publishing' || state === 'succeeded');
  useEffect(() => {
    if (!active || !awaitingTitle || pushLive) return;
    let cancelled = false;
    let requests = 0;
    let pending = false;
    const version = generation.current;
    const read = () => {
      if (document.visibilityState === 'hidden') return;
      if (pending || requests >= 65) return;
      pending = true;
      if (++requests >= 65) clearInterval(timer);
      void getAgentJob(jobId)
        .then((latest) => {
          if (cancelled || generation.current !== version) return;
          setApi((current) => {
            if (current?.id !== latest.id) return current;
            return preserveResolvedTaskTitle(current, {
              ...current,
              title: latest.title,
              title_source: latest.title_source,
            });
          });
          if (
            latest.thread_id &&
            latest.title_source === 'generated' &&
            !taskTitleNeedsGeneration(latest)
          ) {
            // Completed conversations no longer drive sidebar polling. Publish
            // the recovered title so every navigation list refreshes as well.
            announceAgentSessionsChanged({
              threadIds: [latest.thread_id],
              title: latest.title ?? undefined,
              titleSource: 'generated',
            });
          }
        })
        .catch(() => {
          // A title lookup must never interrupt the running conversation.
        })
        .finally(() => {
          pending = false;
        });
    };
    const timer = setInterval(read, 2_000);
    document.addEventListener('visibilitychange', read);
    return () => {
      cancelled = true;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', read);
    };
  }, [active, awaitingTitle, jobId, pushLive, tick]);

  // A queued or terminal turn's stream cannot report another turn finishing.
  // Refresh shared activity until this turn takes over or the conversation
  // and its publication have both settled.
  const publicationActive = hasActiveWorkspacePublication(api);
  const conversationActive = isActiveConversationState(api?.conversation_state);
  // An active goal outlives the turn on screen: the previous round has settled
  // and the next one may not be queued yet, so `conversation_state` alone
  // reports idle while the platform still owes this conversation a round. The
  // goal row is the only field that knows, and it is read from this same row.
  const goalActive = hasActiveGoal(api?.goal);
  const metadataActive =
    api?.id === jobId &&
    (publicationActive ||
      goalActive ||
      (state !== 'running' && state !== 'publishing' && conversationActive));
  const metadataInterval = pushLive ? PUSH_FALLBACK_POLL_MS : 5_000;
  useEffect(() => {
    if (!active || !jobId || !metadataActive) return undefined;
    let cancelled = false;
    let pending = false;
    const version = generation.current;
    const read = () => {
      if (document.visibilityState === 'hidden') return;
      if (pending) return;
      pending = true;
      // These updates change only fields on the job row. Fetching the complete
      // conversation and event history every five seconds would add load and
      // briefly reset unrelated UI state for no benefit.
      void getAgentJob(jobId)
        .then((latest) => {
          if (cancelled || generation.current !== version) return;
          if (!streaming.current && endsStreamingTurn(shownApi.current, latest)) {
            reload();
            return;
          }
          setApi((current) =>
            endsStreamingTurn(current, latest)
              ? current
              : preserveResolvedTaskTitle(current, overlayPushed(latest)),
          );
        })
        .catch(() => {
          // This is a background freshness aid. Keep the last good terminal
          // view on a transient failure and try again on the next interval.
        })
        .finally(() => {
          pending = false;
        });
    };
    const timer = setInterval(read, metadataInterval);
    document.addEventListener('visibilitychange', read);
    return () => {
      cancelled = true;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', read);
    };
  }, [active, jobId, metadataActive, metadataInterval, reload, tick]);

  const visibleThread = useMemo(() => {
    if (!hideForkedHistory || !thread) return thread;
    const jobs = thread.jobs.filter((row) => !row.forked_from_job_id);
    const own = new Set(jobs.map((row) => row.id));
    return {
      ...thread,
      jobs,
      messages: thread.messages.filter((message) => own.has(message.job_id)),
    };
  }, [hideForkedHistory, thread]);
  const adapter = useMemo(
    () => ({ jobId, reads, project: createDisplayJobAdapter() }),
    [jobId, reads],
  );
  const historyLoading = !(loadedCursor?.jobId === api?.id && loadedCursor?.reads === reads);
  const job = useMemo(() => {
    if (!api) return null;
    return adapter.project(api, {
      events,
      patch,
      thread: visibleThread,
      threadEvents,
      historyLoading,
    });
  }, [adapter, api, events, patch, visibleThread, threadEvents, historyLoading]);
  // The turn rows come from the thread read, which is a snapshot: a live
  // attempt replacement updates `api` and would otherwise leave the inspector
  // reading the attempt that is already gone. The current turn is therefore
  // read from the live row, and only the other turns come from the snapshot.
  const threadJobs = useMemo(() => {
    const rows = thread?.jobs ?? [];
    if (!api) return rows;
    const index = rows.findIndex((row) => row.id === api.id);
    if (index === -1) return rows;
    const merged = [...rows];
    merged[index] = api;
    return merged;
  }, [api, thread]);
  // Both shapes, deliberately: `job.events` is the chat view's *reading* of
  // the stream — folded, deduplicated, unknown kinds dropped — while the
  // trajectory needs the rows the platform actually stored.
  return { job, events: threadEvents, threadJobs, loading, error, reload };
}

/** One directory of the workspace tree, as far as it has been loaded. */
export interface WorkspaceDirectory {
  entries: AgentJobFileEntryApi[] | null;
  /** Set when the listing leaves entries out, so `entries` is not the whole folder. */
  truncated?: true;
  loading: boolean;
  error: string | null;
}

/** A listing's incompleteness, carried only when it is set. */
const partialListing = (truncated: boolean | undefined) =>
  truncated ? { truncated: true as const } : {};

/**
 * A path-keyed record with no prototype, holding the entries of `sources`.
 * Workspace paths are the agent's to choose. On a plain object, a folder named
 * `constructor` or `__proto__` reads back an Object.prototype member: shown
 * open before anyone opened it, with that member standing in for its listing.
 */
const byPath = <T>(...sources: Array<Record<string, T>>): Record<string, T> =>
  Object.assign(Object.create(null) as Record<string, T>, ...sources);

export interface WorkspaceFilesState {
  /** Loaded directories keyed by path; the workspace root is the empty string. */
  directories: Record<string, WorkspaceDirectory>;
  expanded: Record<string, boolean>;
  selected: string | null;
  /** A directly opened folder remains browsable even when a parent listing omits it. */
  directoryPath: string | null;
  file: AgentJobFileApi | AgentJobSymlinkApi | null;
  fileLoading: boolean;
  fileError: string | null;
  /** The failed read found nothing at the selected path (404), rather than failing. */
  fileMissing: boolean;
  source: 'workspace' | null;
  toggleDirectory: (path: string) => void;
  retryDirectory: (path: string) => void;
  openFile: (path: string) => void;
  retryFile: () => void;
  showRootDirectory: () => void;
  closeFile: () => void;
  reload: () => void;
}

const filesError = (cause: unknown): string =>
  cause instanceof Error ? cause.message : 'Could not load workspace files';

/**
 * Browse the workspace as a lazily expanded tree, only while the tab is active.
 *
 * Each directory is one request, cached under its path, so expanding a folder
 * never discards the rest of the tree. A refresh (job state change, or a save)
 * refetches the root, every open folder and the open file. Closed folders are
 * marked stale and refetched when they are next opened.
 */
export function useWorkspaceFiles(
  jobId: string,
  enabled: boolean,
  refreshKey = '',
): WorkspaceFilesState {
  const [directories, setDirectories] = useState(() => byPath<WorkspaceDirectory>());
  const [expanded, setExpanded] = useState(() => byPath<boolean>());
  const [selected, setSelected] = useState<string | null>(null);
  const [directoryPath, setDirectoryPath] = useState<string | null>(null);
  const [file, setFile] = useState<AgentJobFileApi | AgentJobSymlinkApi | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const [fileMissing, setFileMissing] = useState(false);
  const [source, setSource] = useState<'workspace' | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((value) => value + 1), []);

  // A refresh invalidates every in-flight request: results from an older
  // generation belong to a workspace state the user is no longer looking at.
  const generation = useRef(0);
  const directoryGenerations = useRef(new Map<string, number>());
  const directoryRequests = useRef(new Map<string, { generation: number; request: number }>());
  const directoryRequest = useRef(0);
  const fileRequest = useRef(0);
  const alive = useRef(true);
  const expandedRef = useRef(expanded);
  const selectedRef = useRef(selected);

  useEffect(() => {
    expandedRef.current = expanded;
    selectedRef.current = selected;
  }, [expanded, selected]);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const loadDirectory = useCallback(
    async (path: string) => {
      const mine = generation.current;
      if (directoryRequests.current.get(path)?.generation === mine) return;
      const request = ++directoryRequest.current;
      directoryRequests.current.set(path, { generation: mine, request });
      const isCurrent = () =>
        alive.current &&
        mine === generation.current &&
        directoryRequests.current.get(path)?.request === request;
      setDirectories((current) =>
        byPath(current, {
          [path]: {
            entries: current[path]?.entries ?? null,
            ...partialListing(current[path]?.truncated),
            loading: true,
            error: null,
          },
        }),
      );
      try {
        const node = await getAgentJobFiles(jobId, path);
        if (!isCurrent()) return;
        directoryGenerations.current.set(path, mine);
        setDirectories((current) =>
          byPath(current, {
            [path]: {
              entries: node.kind === 'directory' ? (node.entries ?? []) : [],
              ...partialListing(node.kind === 'directory' && node.truncated),
              loading: false,
              error: null,
            },
          }),
        );
        if (path === '') setSource(node.source ?? null);
      } catch (cause) {
        if (!isCurrent()) return;
        setDirectories((current) =>
          byPath(current, {
            [path]: { entries: null, loading: false, error: filesError(cause) },
          }),
        );
      } finally {
        if (directoryRequests.current.get(path)?.request === request)
          directoryRequests.current.delete(path);
      }
    },
    [jobId],
  );

  const loadFile = useCallback(
    async (path: string) => {
      const mine = generation.current;
      const request = ++fileRequest.current;
      const isCurrent = () =>
        alive.current && mine === generation.current && request === fileRequest.current;
      setFileLoading(true);
      setFileError(null);
      setFileMissing(false);
      try {
        const node = await getAgentJobFiles(jobId, path);
        if (!isCurrent()) return;
        if (node.kind === 'directory') {
          // A typed folder may have parents that the tree has never loaded.
          // Reveal them too, using the path accepted by the workspace API.
          const parts = node.path.split('/').filter(Boolean);
          const ancestors = parts.map((_, index) => parts.slice(0, index + 1).join('/'));
          directoryRequests.current.delete(node.path);
          directoryGenerations.current.set(node.path, mine);
          setDirectories((current) =>
            byPath(current, {
              [node.path]: {
                entries: node.entries ?? [],
                ...partialListing(node.truncated),
                loading: false,
                error: null,
              },
            }),
          );
          setExpanded((current) =>
            byPath(current, Object.fromEntries(ancestors.map((ancestor) => [ancestor, true]))),
          );
          for (const ancestor of ancestors.slice(0, -1)) {
            if (directoryGenerations.current.get(ancestor) !== mine) void loadDirectory(ancestor);
          }
          setSelected(null);
          setDirectoryPath(node.path);
          selectedRef.current = null;
          setFile(null);
          if (node.source) setSource(node.source);
        } else {
          setFile(node);
        }
      } catch (cause) {
        if (!isCurrent()) return;
        setFile(null);
        setFileError(filesError(cause));
        setFileMissing(cause instanceof APIError && cause.statusCode === 404);
      } finally {
        if (isCurrent()) setFileLoading(false);
      }
    },
    [jobId, loadDirectory],
  );

  useEffect(() => {
    if (!enabled || !jobId) return;
    generation.current += 1;
    // Deliberately not `setDirectories({})`. `loadDirectory` already keeps the
    // entries it has while it reloads, so clearing here bought nothing except
    // a full blank-and-repaint of the tree on every refresh — and the refresh
    // fires while the agent is writing, which is exactly when the user is
    // watching. A stale row for one round trip beats an empty panel.
    const open = expandedRef.current;
    for (const path of ['', ...Object.keys(open).filter((path) => open[path])]) {
      void loadDirectory(path);
    }
    if (selectedRef.current) void loadFile(selectedRef.current);
  }, [enabled, jobId, refreshKey, tick, loadDirectory, loadFile]);

  const toggleDirectory = useCallback(
    (path: string) => {
      const open = Boolean(expanded[path]);
      setExpanded((current) => byPath(current, { [path]: !open }));
      if (
        !open &&
        (!directories[path]?.entries ||
          directoryGenerations.current.get(path) !== generation.current) &&
        directoryRequests.current.get(path)?.generation !== generation.current
      ) {
        void loadDirectory(path);
      }
    },
    [directories, expanded, loadDirectory],
  );

  const openFile = useCallback(
    (path: string) => {
      selectedRef.current = path;
      setSelected(path);
      setFile(null);
      void loadFile(path);
    },
    [loadFile],
  );

  const closeFile = useCallback(() => {
    fileRequest.current += 1;
    selectedRef.current = null;
    setSelected(null);
    setFile(null);
    setFileLoading(false);
    setFileError(null);
    setFileMissing(false);
  }, []);

  const retryDirectory = useCallback((path: string) => void loadDirectory(path), [loadDirectory]);
  const retryFile = useCallback(() => {
    if (selectedRef.current) void loadFile(selectedRef.current);
  }, [loadFile]);
  const showRootDirectory = useCallback(() => {
    setDirectoryPath(null);
    // A path read can resolve to either a file or a folder. Returning to the
    // root supersedes that navigation, so a late folder must not focus the
    // tree again. Keep an already loaded preview beside the root tree.
    fileRequest.current += 1;
    setFileLoading(false);
    if (!file) {
      selectedRef.current = null;
      setSelected(null);
      setFileError(null);
      setFileMissing(false);
    }
  }, [file]);

  return {
    directories,
    expanded,
    selected,
    directoryPath,
    file,
    fileLoading,
    fileError,
    fileMissing,
    source,
    toggleDirectory,
    retryDirectory,
    openFile,
    retryFile,
    showRootDirectory,
    closeFile,
    reload,
  };
}

'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  getAgentJob,
  getAgentJobFiles,
  getAgentJobGit,
  getAgentJobThread,
  listAllAgentThreadEvents,
  listAgentJobs,
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
import { toDisplayJob } from './adapt';
import { announceAgentSessionsChanged, onAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import type { AgentJob } from './types';
import { getConversationState } from './conversationState';
import { isActiveJobState, isActiveConversationState } from './conversationJob';
import { hasActiveGoal } from './goal/liveWork';
import { preserveResolvedTaskTitle, taskTitleNeedsGeneration } from './taskTitles';

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

/** List the caller's jobs, refreshed on an interval while any are live. */
export function useAgentJobList(
  pollMs = 10_000,
  archived = false,
): {
  jobs: AgentJob[];
  loading: boolean;
  error: string | null;
  reload: () => void;
} {
  const [jobs, setJobs] = useState<AgentJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const generation = useRef(0);

  const reload = useCallback(() => {
    generation.current += 1;
    setTick((value) => value + 1);
  }, []);

  useEffect(
    () =>
      onAgentSessionsChanged((change) => {
        if (change.deleted || (change.archived !== undefined && change.archived !== archived)) {
          const changed = new Set(change.threadIds);
          setJobs((current) => current.filter((job) => !changed.has(job.threadId ?? job.id)));
        }
        reload();
      }),
    [archived, reload],
  );

  useEffect(() => {
    let cancelled = false;
    const requestGeneration = generation.current;
    listAgentJobs(50, archived)
      .then((api: AgentJobApi[]) => {
        if (cancelled || requestGeneration !== generation.current) return;
        setJobs(api.map((job) => toDisplayJob(job)));
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!cancelled && requestGeneration === generation.current)
          setError(cause instanceof Error ? cause.message : 'could not load jobs');
      })
      .finally(() => {
        if (!cancelled && requestGeneration === generation.current) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [archived, tick]);

  // Only poll while something can still change; a list of finished jobs does
  // not need a request every ten seconds.
  const hasLive = jobs.some(
    (job) =>
      getConversationState(job) === 'running' ||
      getConversationState(job) === 'queued' ||
      (job.workspaceShared === true &&
        isActiveWorkspacePublicationState(job.workspacePublicationState)),
  );
  useEffect(() => {
    if (!hasLive) return undefined;
    const timer = setInterval(reload, pollMs);
    return () => clearInterval(timer);
  }, [hasLive, pollMs, reload]);

  return { jobs, loading, error, reload };
}

/** The caller's projects, for the sidebar's folder tree. */
export function useAgentProjects(archived = false): {
  projects: AgentProjectApi[];
  loading: boolean;
  error: string | null;
  reload: () => void;
} {
  const [projects, setProjects] = useState<AgentProjectApi[]>([]);
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
    listAgentProjects(archived)
      .then((api) => {
        if (cancelled || requestGeneration !== generation.current) return;
        setProjects(api);
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
  }, [archived, tick]);

  return { projects, loading, error, reload };
}

/**
 * One project's jobs, fetched on demand.
 *
 * The sidebar's shared job list is a global newest-first page, so a project
 * that has been quiet holds only its most recent turns there. Expanding such a
 * folder pulls that project's own page instead of widening the global one.
 */
export function useProjectJobs(limit = 200): {
  jobsByRepo: Map<string, AgentJob[]>;
  loadingRepos: Set<string>;
  errorRepos: Map<string, string>;
  load: (repo: string) => void;
} {
  const [jobsByRepo, setJobsByRepo] = useState<Map<string, AgentJob[]>>(new Map());
  const [loadingRepos, setLoadingRepos] = useState<Set<string>>(new Set());
  const [errorRepos, setErrorRepos] = useState<Map<string, string>>(new Map());
  const inFlight = useRef<Map<string, symbol>>(new Map());
  const requestedRepos = useRef<Set<string>>(new Set());

  const load = useCallback(
    (repo: string) => {
      if (inFlight.current.has(repo)) return;
      requestedRepos.current.add(repo);
      const request = Symbol(repo);
      inFlight.current.set(repo, request);
      setLoadingRepos((current) => new Set(current).add(repo));
      listAgentJobs(limit, false, repo)
        .then((api: AgentJobApi[]) => {
          if (inFlight.current.get(repo) !== request) return;
          setJobsByRepo((current) =>
            new Map(current).set(
              repo,
              api.map((job) => toDisplayJob(job)),
            ),
          );
          setErrorRepos((current) => {
            if (!current.has(repo)) return current;
            const next = new Map(current);
            next.delete(repo);
            return next;
          });
        })
        .catch((cause: unknown) => {
          if (inFlight.current.get(repo) !== request) return;
          setErrorRepos((current) =>
            new Map(current).set(
              repo,
              cause instanceof Error ? cause.message : 'could not load this project',
            ),
          );
        })
        .finally(() => {
          if (inFlight.current.get(repo) !== request) return;
          inFlight.current.delete(repo);
          setLoadingRepos((current) => {
            const next = new Set(current);
            next.delete(repo);
            return next;
          });
        });
    },
    [limit],
  );

  // Completed project pages do not poll. Invalidate both cached rows and any
  // older request, then refresh every folder the reader has already opened.
  useEffect(
    () =>
      onAgentSessionsChanged(() => {
        inFlight.current.clear();
        setJobsByRepo(new Map());
        setLoadingRepos(new Set());
        setErrorRepos(new Map());
        requestedRepos.current.forEach(load);
      }),
    [load],
  );

  useEffect(
    () => () => {
      inFlight.current.clear();
    },
    [],
  );

  return { jobsByRepo, loadingRepos, errorRepos, load };
}

/** One job, its event log, and its patch — kept live over SSE. */
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
  const seen = useRef<Set<number>>(new Set());
  const generation = useRef(0);

  const reload = useCallback(() => {
    generation.current += 1;
    setTick((value) => value + 1);
  }, []);

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
    const requestGeneration = generation.current;
    seen.current = new Set();
    setPatch(null);
    setLoading(true);

    Promise.all([getAgentJob(jobId), getAgentJobThread(jobId), listAllAgentThreadEvents(jobId)])
      .then(([job, foundThread, foundEvents]) => {
        if (cancelled || requestGeneration !== generation.current) return;
        const ownEvents = foundEvents.filter((event) => event.job_id === jobId);
        setApi((current) => preserveResolvedTaskTitle(current, job));
        setEvents(ownEvents);
        setThreadEvents(foundEvents);
        setThread(foundThread);
        ownEvents.forEach((event) => seen.current.add(event.id));
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!cancelled && requestGeneration === generation.current)
          setError(cause instanceof Error ? cause.message : 'could not load the job');
      })
      .finally(() => {
        if (!cancelled && requestGeneration === generation.current) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [active, jobId, tick]);

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
  const isLive = isActiveJobState(state);
  const currentTurnNo =
    api?.turn_no ?? thread?.jobs.find((threadJob) => threadJob.id === jobId)?.turn_no ?? 1;
  useEffect(() => {
    if (!active || !jobId || !isLive) return undefined;
    const controller = new AbortController();
    void streamAgentJob(jobId, {
      signal: controller.signal,
      onEvent: (event) => {
        // The stream can replay across a reconnect, so dedupe on the global id
        // the server assigns rather than assuming each frame is new.
        if (seen.current.has(event.id)) return;
        seen.current.add(event.id);
        setEvents((current) => [...current, event]);
        setThreadEvents((current) => [
          ...current,
          { ...event, job_id: jobId, turn_no: currentTurnNo },
        ]);
        // The stream is also the fastest authority for a queued child
        // becoming active; keep the status pill in step without polling.
        setApi((current) => (current ? applyAgentLifecycleEvent(current, event) : current));
      },
      onFinished: () => reload(),
    }).catch(() => {
      // A dropped stream is the expected shape when the server's cap fires;
      // the reload picks up anything missed.
      if (!controller.signal.aborted) reload();
    });
    return () => controller.abort();
  }, [active, currentTurnNo, jobId, isLive, reload]);

  // A claim may recover an earlier failed title, including on a follow-up.
  // Outlast the server's bounded 120s retry window without waiting for SSE to end.
  const awaitingTitle =
    api?.id === jobId &&
    taskTitleNeedsGeneration(api) &&
    (state === 'running' || state === 'publishing' || state === 'succeeded');
  useEffect(() => {
    if (!active || !awaitingTitle) return;
    let cancelled = false;
    let requests = 0;
    let pending = false;
    const version = generation.current;
    const timer = setInterval(() => {
      if (pending) return;
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
    }, 2_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [active, awaitingTitle, jobId, tick]);

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
  useEffect(() => {
    if (!active || !jobId || !metadataActive) return undefined;
    let cancelled = false;
    let pending = false;
    const version = generation.current;
    const timer = setInterval(() => {
      if (pending) return;
      pending = true;
      // These updates change only fields on the job row. Fetching the complete
      // conversation and event history every five seconds would add load and
      // briefly reset unrelated UI state for no benefit.
      void getAgentJob(jobId)
        .then((latest) => {
          if (!cancelled && generation.current === version)
            setApi((current) => preserveResolvedTaskTitle(current, latest));
        })
        .catch(() => {
          // This is a background freshness aid. Keep the last good terminal
          // view on a transient failure and try again on the next interval.
        })
        .finally(() => {
          pending = false;
        });
    }, 5_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [active, jobId, metadataActive, tick]);

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
  const job = useMemo(
    () => (api ? toDisplayJob(api, { events, patch, thread: visibleThread, threadEvents }) : null),
    [api, events, patch, visibleThread, threadEvents],
  );
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
  loading: boolean;
  error: string | null;
}

export interface WorkspaceFilesState {
  /** Loaded directories keyed by path; the workspace root is the empty string. */
  directories: Record<string, WorkspaceDirectory>;
  expanded: Record<string, boolean>;
  selected: string | null;
  file: AgentJobFileApi | AgentJobSymlinkApi | null;
  fileLoading: boolean;
  fileError: string | null;
  source: 'workspace' | null;
  toggleDirectory: (path: string) => void;
  openFile: (path: string) => void;
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
 * refetches the root, every open folder and the open file — nothing else.
 */
export function useWorkspaceFiles(
  jobId: string,
  enabled: boolean,
  refreshKey = '',
): WorkspaceFilesState {
  const [directories, setDirectories] = useState<Record<string, WorkspaceDirectory>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [selected, setSelected] = useState<string | null>(null);
  const [file, setFile] = useState<AgentJobFileApi | AgentJobSymlinkApi | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const [source, setSource] = useState<'workspace' | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => setTick((value) => value + 1), []);

  // A refresh invalidates every in-flight request: results from an older
  // generation belong to a workspace state the user is no longer looking at.
  const generation = useRef(0);
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
      setDirectories((current) => ({
        ...current,
        [path]: { entries: current[path]?.entries ?? null, loading: true, error: null },
      }));
      try {
        const node = await getAgentJobFiles(jobId, path);
        if (!alive.current || mine !== generation.current) return;
        setDirectories((current) => ({
          ...current,
          [path]: {
            entries: node.kind === 'directory' ? (node.entries ?? []) : [],
            loading: false,
            error: null,
          },
        }));
        if (path === '') setSource(node.source ?? null);
      } catch (cause) {
        if (!alive.current || mine !== generation.current) return;
        setDirectories((current) => ({
          ...current,
          [path]: { entries: null, loading: false, error: filesError(cause) },
        }));
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
      try {
        const node = await getAgentJobFiles(jobId, path);
        if (!isCurrent()) return;
        if (node.kind === 'directory') {
          // The entry turned out to be a directory — show it in the tree.
          setDirectories((current) => ({
            ...current,
            [path]: { entries: node.entries ?? [], loading: false, error: null },
          }));
          setExpanded((current) => ({ ...current, [path]: true }));
          setSelected(null);
          setFile(null);
        } else {
          setFile(node);
        }
      } catch (cause) {
        if (!isCurrent()) return;
        setFile(null);
        setFileError(filesError(cause));
      } finally {
        if (isCurrent()) setFileLoading(false);
      }
    },
    [jobId],
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
      setExpanded((current) => ({ ...current, [path]: !open }));
      if (!open && !directories[path]?.entries) void loadDirectory(path);
    },
    [directories, expanded, loadDirectory],
  );

  const openFile = useCallback(
    (path: string) => {
      setSelected(path);
      setFile(null);
      void loadFile(path);
    },
    [loadFile],
  );

  const closeFile = useCallback(() => {
    fileRequest.current += 1;
    setSelected(null);
    setFile(null);
    setFileLoading(false);
    setFileError(null);
  }, []);

  return {
    directories,
    expanded,
    selected,
    file,
    fileLoading,
    fileError,
    source,
    toggleDirectory,
    openFile,
    closeFile,
    reload,
  };
}

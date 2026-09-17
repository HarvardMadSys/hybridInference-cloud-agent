'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { forkAgentJob, getAgentJob, getAgentJobThread, type AgentJobApi } from '@/lib/api/agents';
import { announceAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { APIError } from '@/lib/utils/errors';
import type { AgentJob } from './types';

export interface SideChatSession {
  anchorJobId: string;
  sandboxId: string;
  requestSent?: boolean;
  jobId?: string;
  threadId?: string;
}

type Preparation = { session: SideChatSession; ready: boolean };
type Status = 'preparing' | 'waiting' | 'ready' | 'error';

// The association is local to this signed-in user's browser tab. The child is
// still an ordinary durable conversation in the server's project list.
export function sideChatStorageKey(userId: string, threadId: string): string {
  return `agent-side-chat:${userId}:${threadId}`;
}

const sessions = new Map<string, SideChatSession>();
const requests = new Map<string, Promise<Preparation>>();

function readSession(key: string): SideChatSession | undefined {
  const memory = sessions.get(key);
  if (memory) return memory;
  let saved: string | null = null;
  try {
    saved = sessionStorage.getItem(key);
  } catch {
    return undefined;
  }
  if (!saved) return undefined;
  const parsed = JSON.parse(saved) as SideChatSession;
  if (
    !parsed ||
    typeof parsed.anchorJobId !== 'string' ||
    typeof parsed.sandboxId !== 'string' ||
    (parsed.jobId !== undefined && typeof parsed.jobId !== 'string') ||
    (parsed.threadId !== undefined && typeof parsed.threadId !== 'string') ||
    Boolean(parsed.jobId) !== Boolean(parsed.threadId)
  ) {
    throw new Error('Could not restore the saved side chat.');
  }
  sessions.set(key, parsed);
  return parsed;
}

function saveSession(key: string, session: SideChatSession): void {
  sessions.set(key, session);
  try {
    sessionStorage.setItem(key, JSON.stringify(session));
  } catch {
    // Blocked storage still keeps one child for this page's lifetime.
  }
}

function isBusy(job: AgentJobApi): boolean {
  return (
    ['queued', 'running', 'publishing'].includes(job.state) ||
    job.conversation_state === 'running' ||
    job.conversation_state === 'queued'
  );
}

function childSession(
  source: AgentJob,
  session: SideChatSession,
  child: AgentJobApi,
): SideChatSession {
  if (
    !child.thread_id ||
    child.thread_id === source.threadId ||
    (session.threadId && child.thread_id !== session.threadId) ||
    child.sandbox_id !== session.sandboxId ||
    child.sandbox_id !== source.sandboxId ||
    child.fork_anchor_job_id !== session.anchorJobId
  ) {
    throw new Error('The saved side chat does not belong to this conversation’s workspace.');
  }
  return { ...session, jobId: child.id, threadId: child.thread_id };
}

async function prepare(
  key: string,
  source: AgentJob,
  sourceJobs: AgentJobApi[],
  canStart: () => boolean,
): Promise<Preparation> {
  let session = readSession(key);
  if (session?.jobId) {
    // Storage is a locator, never authority. Read the authenticated child and
    // prove its native parent/workspace before displaying or submitting to it.
    const thread = await getAgentJobThread(session.jobId);
    const latest = thread?.jobs.reduce<AgentJobApi | undefined>(
      (found, job) => (!found || (job.turn_no ?? 0) > (found.turn_no ?? 0) ? job : found),
      undefined,
    );
    if (!latest || thread?.thread_id !== session.threadId) {
      throw new Error('Could not restore the side chat.');
    }
    session = childSession(source, session, latest);
    saveSession(key, session);
    return { session, ready: true };
  }

  if (!session) {
    if (!source.threadId || !source.sandboxId || !source.workspaceShared || source.archivedAt) {
      throw new Error('Side chat is unavailable for this conversation.');
    }
    const jobs = sourceJobs
      .filter((job) => job.thread_id === source.threadId)
      .sort((a, b) => (b.turn_no ?? 0) - (a.turn_no ?? 0));
    // Opening an old turn still starts from the conversation's current native
    // boundary. If it is running, fix that turn now and wait for it to settle;
    // subsequently queued work must never move this boundary forward.
    const anchor = jobs.find((job) => ['running', 'publishing'].includes(job.state)) ?? jobs[0];
    session = { anchorJobId: anchor?.id ?? source.id, sandboxId: source.sandboxId };
    saveSession(key, session);
  }

  if (!canStart()) return { session, ready: false };
  const anchor = await getAgentJob(session.anchorJobId);
  if (anchor.thread_id !== source.threadId || anchor.sandbox_id !== session.sandboxId) {
    throw new Error('The main conversation’s native session is no longer available.');
  }
  if (!canStart()) return { session, ready: false };
  if (!session.requestSent) {
    if (
      anchor.archived_at ||
      anchor.native_state === 'lost' ||
      anchor.context_status === 'lost' ||
      anchor.state === 'failed' ||
      anchor.state === 'cancelled'
    ) {
      throw new Error('The main turn did not complete. Side chat could not be created.');
    }
    if (isBusy(anchor) || !canStart()) return { session, ready: false };
    if (!anchor.native_fork_available) {
      throw new Error('Native side chat is unavailable for this conversation.');
    }
    session = { ...session, requestSent: true };
    // Save before POST. Even a refresh after a lost response must recover this
    // operation, not pick a newer anchor or turn a retry into a second fork.
    saveSession(key, session);
  }

  try {
    // An issued request is retried directly, even if the source later becomes
    // busy: the server can return a previously committed child in that case.
    const child = await forkAgentJob(session.anchorJobId, { requestScope: key });
    session = childSession(source, session, child);
    saveSession(key, session);
    return { session, ready: true };
  } catch (cause) {
    if (cause instanceof APIError && cause.code === 'sandbox_busy') {
      return { session, ready: false };
    }
    throw cause;
  }
}

export function useSideChatSession(
  source: AgentJob,
  userId: string | undefined,
  active: boolean,
  sourceJobs: AgentJobApi[] = [],
) {
  const key = userId ? sideChatStorageKey(userId, source.threadId ?? source.id) : '';
  const [session, setSession] = useState<SideChatSession | null>(null);
  const [status, setStatus] = useState<Status>('preparing');
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const sourceRef = useRef({ job: source, jobs: sourceJobs });
  const activeRef = useRef(active);
  const mounted = useRef(false);
  sourceRef.current = { job: source, jobs: sourceJobs };
  activeRef.current = active;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    if (!active || !key || status === 'ready') return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setStatus('preparing');
    setError(null);
    let request = requests.get(key);
    if (!request) {
      request = prepare(
        key,
        sourceRef.current.job,
        sourceRef.current.jobs,
        () => mounted.current && activeRef.current,
      );
      requests.set(key, request);
      // Coalesce StrictMode/remounts and double activation. A settled request
      // leaves the map on both paths, without creating an unhandled rejection.
      const clear = () => {
        if (requests.get(key) === request) requests.delete(key);
      };
      void request.then(clear, clear);
    }
    void request.then(
      (result) => {
        if (cancelled) return;
        setSession(result.session);
        setStatus(result.ready ? 'ready' : 'waiting');
        if (result.ready && result.session.threadId) {
          announceAgentSessionsChanged({ threadIds: [result.session.threadId] });
        } else {
          timer = setTimeout(() => setTick((value) => value + 1), 3000);
        }
      },
      (cause: unknown) => {
        if (cancelled) return;
        setStatus('error');
        setError(cause instanceof Error ? cause.message : 'Could not open side chat.');
      },
    );
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // Status changes are the result of this operation, not a reason to restart
    // it. Retry, tab activation and a waiting poll are the only triggers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, key, tick]);

  const advance = useCallback(
    (job: AgentJobApi) => {
      if (!session || !key) return;
      const next = childSession(sourceRef.current.job, session, job);
      saveSession(key, next);
      setSession(next);
      announceAgentSessionsChanged({ threadIds: [next.threadId!] });
    },
    [key, session],
  );
  const retry = useCallback(() => setTick((value) => value + 1), []);
  return { session: status === 'ready' ? session : null, status, error, retry, advance, key };
}

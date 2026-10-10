'use client';

// What this tab has been told by the owner's change stream, shared by every
// list and page in it: whether pushes are flowing, the newest value of each
// pushed job and conversation, and a batched "these records changed" signal.
//
// Lists answer the signal by reading their page again; an open conversation
// patches the fields it was sent, and its usage line shows the usage it was
// sent. Either way the clocks they used to read on stay, much slower, for when
// pushes are not flowing.
import { useEffect, useSyncExternalStore } from 'react';

import type { AgentJobApi, AgentJobUsageApi } from '@/lib/api/agents';
import type {
  AgentChangeHint,
  AgentJobChangeApi,
  AgentThreadChangeApi,
} from '@/lib/api/agentChanges';
import { streamAgentChanges } from '@/lib/api/agentChanges';

import { isActiveJobState } from './conversationJob';

/** How a list re-reads while pushes flow: a backstop, not the way it learns. */
export const PUSH_FALLBACK_POLL_MS = 60_000;
/** Changes that land together are delivered together, so a list reads once. */
export const PUSH_BATCH_MS = 300;

export interface AgentRecordsChange {
  jobs: AgentJobChangeApi[];
  threads: AgentThreadChangeApi[];
  /** Jobs whose usage changed. */
  usage: string[];
  /**
   * The newest usage sent for those jobs, by job. A job in `usage` without one
   * here is read again.
   */
  usageValues: Record<string, AgentJobUsageApi>;
  /** Conversations whose waiting messages changed. */
  queues: string[];
  /** Jobs whose tool calls awaiting approval changed. */
  mcp: string[];
  /** Conversations whose publication progress changed. */
  publications: string[];
  /** Changes may have been missed: read again rather than trust patches. */
  resync: boolean;
}

const HINT_FIELD = {
  usage: 'usage',
  queue: 'queues',
  mcp: 'mcp',
  publication: 'publications',
} as const satisfies Record<AgentChangeHint, keyof AgentRecordsChange>;

let live = false;
const liveListeners = new Set<() => void>();
const recordListeners = new Set<(change: AgentRecordsChange) => void>();
const latestJobs = new Map<string, AgentJobChangeApi>();
const latestThreads = new Map<string, AgentThreadChangeApi>();
let pending: AgentRecordsChange | null = null;
let flushTimer: ReturnType<typeof setTimeout> | null = null;

function flush(): void {
  flushTimer = null;
  const change = pending;
  pending = null;
  if (!change) return;
  for (const listener of [...recordListeners]) listener(change);
}

function queue(update: (change: AgentRecordsChange) => void): void {
  pending ??= {
    jobs: [],
    threads: [],
    usage: [],
    usageValues: {},
    queues: [],
    mcp: [],
    publications: [],
    resync: false,
  };
  update(pending);
  flushTimer ??= setTimeout(flush, PUSH_BATCH_MS);
}

/** Keep `change` only if it is newer than what this tab already holds. */
function newer<T extends { id: string; revision?: number; deleted?: boolean }>(
  held: Map<string, T>,
  change: T,
): boolean {
  if (change.deleted) {
    held.set(change.id, change);
    return true;
  }
  const known = held.get(change.id);
  if (known?.deleted) return false;
  if (known?.revision !== undefined && (change.revision ?? -1) <= known.revision) return false;
  held.set(change.id, change);
  return true;
}

export function isAgentPushLive(): boolean {
  return live;
}

export function setAgentPushLive(next: boolean): void {
  if (live === next) return;
  live = next;
  for (const listener of [...liveListeners]) listener();
}

function subscribeLive(listener: () => void): () => void {
  liveListeners.add(listener);
  return () => liveListeners.delete(listener);
}

/** Whether pushes are flowing in this tab; lists poll slowly while they are. */
export function useAgentPushLive(): boolean {
  return useSyncExternalStore(subscribeLive, isAgentPushLive, () => false);
}

export function onAgentRecordsChanged(listener: (change: AgentRecordsChange) => void): () => void {
  recordListeners.add(listener);
  return () => recordListeners.delete(listener);
}

export function receiveJobChange(change: AgentJobChangeApi): void {
  if (newer(latestJobs, change)) queue((batch) => batch.jobs.push(change));
}

export function receiveThreadChange(change: AgentThreadChangeApi): void {
  if (newer(latestThreads, change)) queue((batch) => batch.threads.push(change));
}

export function receiveHint(kind: AgentChangeHint, id: string, usage?: AgentJobUsageApi): void {
  queue((batch) => {
    const ids = batch[HINT_FIELD[kind]];
    if (!ids.includes(id)) ids.push(id);
    if (kind !== 'usage') return;
    // The last word wins: a later "read it again" replaces an earlier value.
    if (usage) batch.usageValues[id] = usage;
    else delete batch.usageValues[id];
  });
}

export function receiveSync(): void {
  queue((batch) => {
    batch.resync = true;
  });
}

/**
 * Lay the newest pushed values over a job read from the API.
 *
 * A read that left before a change and arrived after its push must not put the
 * old title or state back; each row's revision says which of the two is newer.
 *
 * A push never ends a turn that is still active in the read. That turn's own
 * event stream does, after delivering its last events, and the reload it ends
 * with brings everything the push carried: ending it here would close that
 * stream before them.
 */
export function overlayPushed(job: AgentJobApi): AgentJobApi {
  let result = job;
  const pushedJob = latestJobs.get(job.id);
  if (
    pushedJob &&
    !pushedJob.deleted &&
    (pushedJob.revision ?? -1) > (job.revision ?? -1) &&
    pushedJob.state !== undefined &&
    !(isActiveJobState(job.state) && !isActiveJobState(pushedJob.state))
  ) {
    result = {
      ...result,
      revision: pushedJob.revision,
      state: pushedJob.state,
      conversation_state: pushedJob.conversation_state ?? result.conversation_state,
      published_pr_url: pushedJob.published_pr_url ?? null,
    };
  }
  const pushedThread = job.thread_id ? latestThreads.get(job.thread_id) : undefined;
  if (
    pushedThread &&
    !pushedThread.deleted &&
    (pushedThread.revision ?? -1) > (job.thread_revision ?? -1) &&
    pushedThread.title !== undefined
  ) {
    result = {
      ...result,
      thread_revision: pushedThread.revision,
      title: pushedThread.title,
      title_source: pushedThread.title_source ?? result.title_source,
    };
  }
  return result;
}

/**
 * Whether `candidate` is an older read of a job's usage than `shown`.
 *
 * A read the tab asked for and one it was pushed can arrive in either order;
 * each carries its snapshot's version, and the greater saw everything the
 * lesser did. Without versions on both, nothing is known to be older.
 */
export function isOlderUsage(
  candidate: AgentJobUsageApi,
  shown: AgentJobUsageApi | null | undefined,
): boolean {
  return Boolean(candidate.version && shown?.version && candidate.version < shown.version);
}

/** Forget everything this tab was told; for a new account and for tests. */
export function resetAgentPush(): void {
  if (flushTimer) clearTimeout(flushTimer);
  flushTimer = null;
  pending = null;
  latestJobs.clear();
  latestThreads.clear();
  setAgentPushLive(false);
}

/**
 * Hold the signed-in owner's change stream for this tab.
 *
 * Mounted once by the agents shell. Where the deployment pushes nothing the
 * stream ends at once and every list keeps its own clock.
 */
export function useAgentChangeStream(userId: string | undefined): void {
  useEffect(() => {
    if (!userId) return undefined;
    resetAgentPush();
    const controller = new AbortController();
    void streamAgentChanges({
      signal: controller.signal,
      onSync: (nowLive) => {
        setAgentPushLive(nowLive);
        receiveSync();
      },
      onJob: receiveJobChange,
      onThread: receiveThreadChange,
      onHint: receiveHint,
      onDisconnect: () => setAgentPushLive(false),
    }).then(() => setAgentPushLive(false));
    return () => {
      controller.abort();
      resetAgentPush();
    };
  }, [userId]);
}

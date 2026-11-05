import type { ThreadHistoryOptions } from './agents/threads';
import {
  getAgentJob,
  getAgentJobThread,
  type AgentJobApi,
  type AgentJobUsageApi,
  type AgentThreadApi,
} from './agents';

export interface AgentReadScope {
  getAgentJob: (jobId: string) => Promise<AgentJobApi>;
  getAgentJobThread: (
    jobId: string,
    options?: ThreadHistoryOptions,
  ) => Promise<AgentThreadApi | null>;
  /** The usage snapshot kept for a turn, or undefined when none was kept. */
  getUsage: (jobId: string) => AgentJobUsageApi | null | undefined;
  /** Keep a turn's usage snapshot within this owner's scope. */
  setUsage: (jobId: string, usage: AgentJobUsageApi | null) => void;
  /** Drop pending reads before a mutation, forced reload, or stream resync. */
  invalidate: (jobId?: string) => void;
}

/** Share concurrent readers only; every read after settlement goes to the API. */
function pendingRead<T>(
  pending: Map<string, Promise<T>>,
  jobId: string,
  load: (jobId: string) => Promise<T>,
): Promise<T> {
  const held = pending.get(jobId);
  if (held) return held;
  const request = load(jobId);
  pending.set(jobId, request);
  const clear = () => {
    // An invalidation may already have installed a newer request for this id.
    if (pending.get(jobId) === request) pending.delete(jobId);
  };
  void request.then(clear, clear);
  return request;
}

/** Each signed-in provider creates its own scope; nothing is held globally. */
export function createAgentReadScope(): AgentReadScope {
  const jobs = new Map<string, Promise<AgentJobApi>>();
  const threads = new Map<string, Promise<AgentThreadApi | null>>();
  // Kept usage snapshots, one per turn. They die with the owner's scope, so a
  // sign-out or account switch cannot carry a snapshot into the next session.
  const usage = new Map<string, AgentJobUsageApi | null>();
  return {
    getAgentJob: (jobId) => pendingRead(jobs, jobId, getAgentJob),
    getAgentJobThread: (jobId, options) =>
      pendingRead(threads, options ? JSON.stringify([jobId, options]) : jobId, () =>
        options ? getAgentJobThread(jobId, options) : getAgentJobThread(jobId),
      ),
    getUsage: (jobId) => usage.get(jobId),
    setUsage: (jobId, snapshot) => {
      usage.set(jobId, snapshot);
    },
    invalidate: (jobId) => {
      // Kept usage snapshots are deliberately not dropped here: their versions
      // already decide what a later open shows, and this clears the pending
      // reads only.
      if (jobId === undefined) {
        jobs.clear();
        threads.clear();
      } else {
        jobs.delete(jobId);
        for (const key of threads.keys()) {
          if (key === jobId || key.startsWith(`${JSON.stringify([jobId]).slice(0, -1)},`))
            threads.delete(key);
        }
      }
    },
  };
}

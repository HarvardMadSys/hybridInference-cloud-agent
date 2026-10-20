import {
  getAgentJob,
  getAgentJobThread,
  type AgentJobApi,
  type AgentJobUsageApi,
  type AgentThreadApi,
} from './agents';

export interface AgentReadScope {
  getAgentJob: (jobId: string) => Promise<AgentJobApi>;
  getAgentJobThread: (jobId: string) => Promise<AgentThreadApi | null>;
  /** Drop pending reads before a mutation, forced reload, or stream resync. */
  invalidate: (jobId?: string) => void;
  /**
   * The last usage snapshot read or pushed for each job. Unlike the pending
   * reads it outlives an invalidation: the panel decides from the snapshot's
   * own ledger whether to read again.
   */
  usage: Map<string, AgentJobUsageApi | null>;
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
  return {
    getAgentJob: (jobId) => pendingRead(jobs, jobId, getAgentJob),
    getAgentJobThread: (jobId) => pendingRead(threads, jobId, getAgentJobThread),
    usage: new Map(),
    invalidate: (jobId) => {
      if (jobId === undefined) {
        jobs.clear();
        threads.clear();
      } else {
        jobs.delete(jobId);
        threads.delete(jobId);
      }
    },
  };
}

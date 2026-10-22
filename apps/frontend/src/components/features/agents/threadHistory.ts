import type { AgentThreadApi, AgentThreadEventApi } from '@/lib/api/agents';
import type { AgentReadScope } from '@/lib/api/agentReadScope';
import { latestJob } from './conversationJob';

/** Retain immutable older pages; never let a preview replace a fuller message. */
export function mergeThreadHistory(
  held: AgentThreadApi | null,
  page: AgentThreadApi,
  older = false,
): AgentThreadApi {
  if (held?.thread_id !== page.thread_id) return page;
  const jobs = new Map(held.jobs.map((job) => [job.id, job]));
  for (const job of page.jobs) {
    if ((jobs.get(job.id)?.revision ?? 0) <= (job.revision ?? 0)) jobs.set(job.id, job);
  }
  const messages = new Map(held.messages.map((message) => [message.id, message]));
  for (const message of page.messages) {
    const previous = messages.get(message.id);
    if (!previous || previous.content.length <= message.content.length)
      messages.set(message.id, message);
  }
  const orderedJobs = [...jobs.values()].sort(
    (left, right) => (left.turn_no ?? 1) - (right.turn_no ?? 1),
  );
  const turns = new Map(orderedJobs.map((job) => [job.id, job.turn_no ?? 1]));
  const phase = (message: AgentThreadApi['messages'][number]) =>
    message.role === 'user' && !message.mid_turn ? 0 : 1;
  return {
    ...held,
    ...page,
    jobs: orderedJobs,
    messages: [...messages.values()].sort(
      (left, right) =>
        (turns.get(left.job_id) ?? 1) - (turns.get(right.job_id) ?? 1) ||
        phase(left) - phase(right) ||
        left.id - right.id,
    ),
    next_cursor: older ? page.next_cursor : held.next_cursor,
  };
}

async function withCurrent(reads: AgentReadScope, snapshot: AgentThreadApi | null) {
  const currentId = snapshot?.current_job_id;
  if (
    !snapshot ||
    !currentId ||
    snapshot.jobs.some((job) => job.id === currentId && !job.thread_preview)
  )
    return snapshot;
  const current = await reads.getAgentJob(currentId);
  return {
    ...snapshot,
    jobs: [...snapshot.jobs.filter((job) => job.id !== currentId), current].sort(
      (a, b) => (a.turn_no ?? 1) - (b.turn_no ?? 1),
    ),
  };
}

/** Sidebar and detail share the same anchored page and current-turn read. */
export async function readConversationSelection(reads: AgentReadScope, jobId: string) {
  const [job, page] = await Promise.all([
    reads.getAgentJob(jobId),
    reads.getAgentJobThread(jobId, { anchor: true }),
  ]);
  const thread = await withCurrent(reads, page);
  return { job, thread, latest: latestJob([job, ...(thread?.jobs ?? [])]) ?? job };
}

/** Scoped to one mounted owner's hook; no transcript survives an account change. */
export function createThreadHistoryReader(reads: AgentReadScope) {
  let held: AgentThreadApi | null = null;
  let selectedJob = '';
  let selectedTurn = 0;
  let version = 0;

  async function tail(jobId: string, snapshot: AgentThreadApi, cursor: string) {
    let next: string | null | undefined = cursor;
    const visited = new Set<string>();
    while (next) {
      if (visited.has(next)) throw new Error('Thread history cursor did not advance');
      visited.add(next);
      const page = await reads.getAgentJobThread(jobId, { after: next });
      if (!page) return null;
      snapshot = mergeThreadHistory(snapshot, page);
      next = page.next_cursor;
    }
    return snapshot;
  }

  return {
    async read(jobId: string) {
      const request = ++version;
      const previous = held;
      let snapshot: AgentThreadApi | null;
      if (previous?.sync_cursor && selectedJob === jobId) {
        snapshot = await tail(jobId, previous, previous.sync_cursor);
      } else {
        const page = await reads.getAgentJobThread(jobId, { anchor: true });
        const anchorTurn = page?.jobs.find((job) => job.id === jobId)?.turn_no ?? 0;
        const oldestTurn = page?.jobs[0]?.turn_no ?? 0;
        // A jump to a distant historical URL creates a new backfill window;
        // retaining old rows must not hide the unloaded gap between windows.
        const newWindow = anchorTurn < selectedTurn || oldestTurn > selectedTurn + 1;
        snapshot = page && mergeThreadHistory(previous, page, newWindow);
        if (
          !newWindow &&
          snapshot &&
          previous?.sync_cursor &&
          previous.thread_id === snapshot.thread_id
        ) {
          snapshot = await tail(jobId, snapshot, previous.sync_cursor);
        }
      }
      snapshot = await withCurrent(reads, snapshot);
      if (request === version) {
        held = snapshot;
        selectedJob = jobId;
        selectedTurn = snapshot?.jobs.find((job) => job.id === jobId)?.turn_no ?? 0;
      }
      return snapshot;
    },
    async older(jobId: string) {
      const request = version;
      if (!held?.next_cursor || selectedJob !== jobId) return null;
      const page = await reads.getAgentJobThread(jobId, { cursor: held.next_cursor });
      if (!page || request !== version || held?.thread_id !== page.thread_id) return null;
      held = mergeThreadHistory(held, page, true);
      return held;
    },
  };
}

/** Replay the two mutable turns to cover IDs allocated before their writes commit. */
export function resumeAfter(held: readonly AgentThreadEventApi[]): number {
  const turns = [...new Set(held.map((event) => event.turn_no))].sort((a, b) => b - a);
  const from = turns[1] ?? turns[0];
  let first = Infinity;
  for (const event of held) {
    if (event.turn_no >= from && event.id < first) first = event.id;
  }
  return Number.isFinite(first) ? first - 1 : 0;
}

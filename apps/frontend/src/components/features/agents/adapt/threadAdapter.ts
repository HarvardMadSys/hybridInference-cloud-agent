// Adapts the conversation (Thread) API shape: the durable messages and the
// other turns' events that surround the turn being shown.

import type {
  AgentJobApi,
  AgentJobEventApi,
  AgentThreadApi,
  AgentThreadEventApi,
  AgentThreadMessageApi,
} from '@/lib/api/agents';
import type { AgentEvent, AgentHistoryTurn } from '../types';
import { displayEvents } from './events';
import { storedGoalTurn, toDisplayState } from './jobState';
import { mapThreadMessage } from './threadMessage';

/** Index a conversation once, rather than scanning its full log for each turn. */
export function groupByJob<T extends { job_id: string }>(rows: T[]): Map<string, T[]> {
  const groups = new Map<string, T[]>();
  for (const row of rows) {
    const group = groups.get(row.job_id);
    if (group) group.push(row);
    else groups.set(row.job_id, [row]);
  }
  return groups;
}

export function sameRecords<T>(previous: T[], next: T[]): boolean {
  return previous.length === next.length && previous.every((row, index) => row === next[index]);
}

export function displayHistoryTurn(
  job: AgentJobApi,
  messages: AgentThreadMessageApi[],
  events: AgentThreadEventApi[],
  historyLoading = false,
): AgentHistoryTurn {
  const goalTurn = storedGoalTurn(job);
  return {
    jobId: job.id,
    turnNo: job.turn_no ?? 1,
    state: toDisplayState(job),
    // Only the server can prove the native session and host are forkable.
    forkable: job.native_fork_available === true,
    ...(goalTurn ? { goalTurn } : {}),
    messages: messages.map(mapThreadMessage),
    events: transcriptEvents(events, job, messages, historyLoading),
  };
}

export function transcriptEvents(
  events: AgentJobEventApi[],
  job: AgentJobApi,
  messages: AgentThreadMessageApi[],
  historyLoading: boolean,
): AgentEvent[] {
  const projected = displayEvents(events, job);
  if (!historyLoading) return projected;
  // Event ids and durable-message ids do not identify each other. Do not let
  // the first partial reply suppress the remaining durable replies, or show
  // the same text twice while the ledger is incomplete.
  const hasReplies = messages.some((message) => message.role === 'assistant');
  const hasUsers = messages.some((message) => message.role === 'user');
  return projected.filter(
    (event) =>
      !(hasReplies && event.kind === 'message') && !(hasUsers && event.kind === 'user_message'),
  );
}

export type HistoryProjector = (
  jobs: AgentJobApi[],
  messages: Map<string, AgentThreadMessageApi[]>,
  events: Map<string, AgentThreadEventApi[]>,
  historyLoading: boolean,
) => AgentHistoryTurn[];

type DisplayThreadMessage = ReturnType<typeof mapThreadMessage>;

/** The thread around one turn, as `AgentJob` carries it. */
export interface ThreadProjection {
  threadMessages: DisplayThreadMessage[];
  historyTurns?: AgentHistoryTurn[];
  historyIncludesPrompt: boolean;
  publishedPrUrl: string | null;
}

export interface ThreadSources {
  thread?: AgentThreadApi | null;
  threadEvents?: AgentThreadEventApi[];
  historyLoading: boolean;
}

/** The job ids of the turns before this one, or null when the thread cannot say. */
function priorJobIdsOf(
  thread: AgentThreadApi,
  currentTurn: number | undefined,
): Set<string> | null {
  if (currentTurn === undefined || !thread.jobs.length) return null;
  return new Set(
    thread.jobs
      .filter((threadJob) => (threadJob.turn_no ?? 1) < currentTurn)
      .map((threadJob) => threadJob.id),
  );
}

function historyTurnsOf(
  historicalJobs: AgentJobApi[],
  messagesByJob: Map<string, AgentThreadMessageApi[]>,
  threadEvents: AgentThreadEventApi[],
  historyLoading: boolean,
  projectHistory?: HistoryProjector,
): AgentHistoryTurn[] {
  const eventsByJob = groupByJob(threadEvents);
  if (projectHistory) {
    return projectHistory(historicalJobs, messagesByJob, eventsByJob, historyLoading);
  }
  return historicalJobs.map((threadJob) =>
    displayHistoryTurn(
      threadJob,
      messagesByJob.get(threadJob.id) ?? [],
      eventsByJob.get(threadJob.id) ?? [],
      historyLoading,
    ),
  );
}

/** The newest pull request of the conversation this turn may link to. */
function publishedPrUrlOf(job: AgentJobApi, thread: AgentThreadApi | null | undefined) {
  const threadPrJob = [...(thread?.jobs ?? [])]
    .reverse()
    .find((threadJob) => threadJob.published_pr_url);
  return threadPrJob && (threadPrJob.turn_no ?? 1) > (job.turn_no ?? 1)
    ? threadPrJob.published_pr_url
    : (job.published_pr_url ?? threadPrJob?.published_pr_url ?? null);
}

/** The other turns' messages and history, and this turn's own stored copy when it needs one. */
export function projectThread(
  job: AgentJobApi,
  events: AgentJobEventApi[],
  { thread, threadEvents, historyLoading }: ThreadSources,
  projectHistory?: HistoryProjector,
): ThreadProjection {
  const threadJobs = thread?.jobs ?? [];
  const threadMessages = thread?.messages ?? [];
  const currentTurn =
    job.turn_no ?? threadJobs.find((threadJob) => threadJob.id === job.id)?.turn_no;
  const priorJobIds = thread ? priorJobIdsOf(thread, currentTurn) : null;
  const messagesByJob = groupByJob(threadMessages);
  const priorMessages = threadMessages
    .filter((message) =>
      priorJobIds ? priorJobIds.has(message.job_id) : message.job_id !== job.id,
    )
    .map(mapThreadMessage);
  const historicalJobs = threadJobs.filter(
    (threadJob) =>
      threadJob.id !== job.id &&
      (currentTurn === undefined || (threadJob.turn_no ?? 1) < currentTurn),
  );
  const historyTurns =
    threadEvents === undefined
      ? undefined
      : historyTurnsOf(historicalJobs, messagesByJob, threadEvents, historyLoading, projectHistory);
  // A turn copied by fork has no events; its durable messages are the only
  // record of what it said. For a settled job whose event log carries no
  // message rows, render the thread's copy of the turn — prompt included, so
  // the turn keeps its chronology and the prompt card knows to stand down.
  const settled = job.state === 'succeeded' || job.state === 'failed' || job.state === 'cancelled';
  let ownMessages: DisplayThreadMessage[] = [];
  if (historyLoading || (settled && !events.some((event) => event.event_type === 'message'))) {
    ownMessages = (messagesByJob.get(job.id) ?? []).map(mapThreadMessage);
  }
  return {
    threadMessages: [...priorMessages, ...ownMessages],
    ...(historyTurns ? { historyTurns } : {}),
    historyIncludesPrompt: ownMessages.some((message) => message.role === 'user'),
    publishedPrUrl: publishedPrUrlOf(job, thread),
  };
}

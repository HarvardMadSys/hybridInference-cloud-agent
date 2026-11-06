// Adapts the agent-jobs (Job) API shape to what the UI renders.
//
// The UI was designed against a richer mock than the API currently returns.
// This module makes that gap explicit rather than papering over it: fields the
// backend genuinely provides are mapped, and fields it does not are left empty
// so the UI renders an honest blank instead of an invented value. The
// surrounding conversation comes from `threadAdapter`; events, diffs and
// resources are read by the helpers beside it.

import type {
  AgentJobApi,
  AgentJobEventApi,
  AgentThreadApi,
  AgentThreadEventApi,
  AgentThreadMessageApi,
} from '@/lib/api/agents';
import { projectKey } from '@/lib/temporaryWorkspace';
import type { AgentAttempt, AgentEvent, AgentHistoryTurn, AgentJob } from '../types';
import { agentLiveStatus } from '../trajectory/agentLiveStatus';
import {
  workspaceGitProjection,
  type WorkspaceGitOptions,
} from '../workspace/workspaceGitMetadata';
import { parsePatch } from './diff';
import { asText } from './events';
import {
  failureCauseOf,
  ownerFailureMessage,
  storedGoalTurn,
  toDisplayGoal,
  toDisplayState,
} from './jobState';
import { toAttemptTelemetry, toTurnResources } from './resources';
import {
  displayHistoryTurn,
  projectThread,
  sameRecords,
  transcriptEvents,
  type HistoryProjector,
} from './threadAdapter';

/** Describe the trusted execution backend reported for the current attempt. */
function sandboxLabel(events: AgentJobEventApi[], currentAttemptId: number | null): string {
  const started = [...events]
    .reverse()
    .find(
      (event) =>
        event.event_type === 'lifecycle' &&
        event.payload?.phase === 'started' &&
        (currentAttemptId === null || event.attempt_id === currentAttemptId),
    );
  if (!started) return '';

  const backend = asText(started.payload, 'sandbox_backend');
  const runtime = asText(started.payload, 'sandbox_runtime');
  return sandboxDisplayLabel(backend || runtime, runtime);
}

/** Keep older runtime identifiers readable when they reach the display model. */
export function sandboxDisplayLabel(backend: string, runtime?: string): string {
  if (
    backend === 'kata' ||
    backend === 'io.containerd.kata.v2' ||
    runtime === 'io.containerd.kata.v2'
  ) {
    return 'Isolated VM';
  }
  if (backend === 'container' || backend === 'docker') return 'Container';
  if (backend === 'process') return 'Host process';
  return backend;
}

export interface AdaptOptions extends WorkspaceGitOptions {
  events?: AgentJobEventApi[];
  threadEvents?: AgentThreadEventApi[];
  /** Keep complete durable replies visible while activity pages are still arriving. */
  historyLoading?: boolean;
  thread?: AgentThreadApi | null;
}

/**
 * A projection cache owned by one mounted conversation. Stored rows are
 * immutable: live appends keep their references, while reloads replace them.
 * A changed row, message or event rebuilds that turn, including late results.
 * Only the visible history is retained; there is no cross-owner or TTL cache.
 */
export function createDisplayJobAdapter(): typeof toDisplayJob {
  type CachedTurn = {
    job: AgentJobApi;
    messages: AgentThreadMessageApi[];
    events: AgentThreadEventApi[];
    historyLoading: boolean;
    turn: AgentHistoryTurn;
  };
  let scope: string | undefined;
  let cached = new Map<string, CachedTurn>();
  let history: AgentHistoryTurn[] = [];
  const projectHistory: HistoryProjector = (jobs, messages, events, historyLoading) => {
    const nextCache = new Map<string, CachedTurn>();
    const nextHistory = jobs.map((job) => {
      const jobMessages = messages.get(job.id) ?? [];
      const jobEvents = events.get(job.id) ?? [];
      let entry = cached.get(job.id);
      if (
        !entry ||
        entry.job !== job ||
        entry.historyLoading !== historyLoading ||
        !sameRecords(entry.messages, jobMessages) ||
        !sameRecords(entry.events, jobEvents)
      ) {
        entry = {
          job,
          messages: jobMessages,
          events: jobEvents,
          historyLoading,
          turn: displayHistoryTurn(job, jobMessages, jobEvents, historyLoading),
        };
      }
      nextCache.set(job.id, entry);
      return entry.turn;
    });
    cached = nextCache;
    if (!sameRecords(history, nextHistory)) history = nextHistory;
    return history;
  };
  // The live turn's rows from the last projection. A running turn projects
  // again for every event it streams, building every row anew; a row that
  // reads the same keeps its object, so the rows that did not change are not
  // rendered again.
  let live: { jobId: string; rows: AgentEvent[] } = { jobId: '', rows: [] };
  return (job, options = {}) => {
    const nextScope = options.thread?.thread_id ?? job.thread_id ?? job.id;
    if (scope !== nextScope || options.threadEvents === undefined) {
      cached.clear();
      history = [];
      scope = nextScope;
    }
    const adapted = adaptJob(job, options, projectHistory);
    const rows =
      live.jobId === job.id ? keepUnchangedRows(live.rows, adapted.events) : adapted.events;
    live = { jobId: job.id, rows };
    return rows === adapted.events ? adapted : { ...adapted, events: rows };
  };
}

/**
 * `next`, with each row that reads the same as the row at its place in
 * `previous` replaced by that earlier object. Rows are only ever appended or
 * replaced where they stand, so a row's place identifies it.
 */
function keepUnchangedRows(previous: AgentEvent[], next: AgentEvent[]): AgentEvent[] {
  let reused = 0;
  const rows = next.map((row, index) => {
    const earlier = previous[index];
    if (earlier === undefined || !sameRow(earlier, row)) return row;
    reused += 1;
    return earlier;
  });
  return reused === next.length && next.length === previous.length ? previous : rows;
}

/** Field by field; a field whose value is a new object is compared by content. */
function sameRow(left: AgentEvent, right: AgentEvent): boolean {
  const leftRecord = left as unknown as Record<string, unknown>;
  const rightRecord = right as unknown as Record<string, unknown>;
  let fields = 0;
  for (const key in leftRecord) {
    fields += 1;
    if (!Object.hasOwn(rightRecord, key)) return false;
    const value = leftRecord[key];
    if (value !== rightRecord[key] && !sameValue(value, rightRecord[key])) return false;
  }
  for (const key in rightRecord) if (Object.hasOwn(rightRecord, key)) fields -= 1;
  return fields === 0;
}

function sameValue(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return (
      Array.isArray(left) &&
      Array.isArray(right) &&
      left.length === right.length &&
      left.every((item, index) => sameValue(item, right[index]))
    );
  }
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object') return false;
  const leftRecord = left as Record<string, unknown>;
  const rightRecord = right as Record<string, unknown>;
  const keys = Object.keys(leftRecord);
  return (
    keys.length === Object.keys(rightRecord).length &&
    keys.every(
      (key) => Object.hasOwn(rightRecord, key) && sameValue(leftRecord[key], rightRecord[key]),
    )
  );
}

export function toDisplayJob(job: AgentJobApi, options: AdaptOptions = {}): AgentJob {
  return adaptJob(job, options);
}

function adaptJob(
  job: AgentJobApi,
  options: AdaptOptions,
  projectHistory?: HistoryProjector,
): AgentJob {
  const failureCause = failureCauseOf(job.detail);
  const events = options.events ?? [];
  const patch = options.patch ?? '';
  const parsed = parsePatch(patch);
  const ownStoredMessages = (options.thread?.messages ?? []).filter(
    (message) => message.job_id === job.id,
  );
  const historyLoading = options.historyLoading === true;
  const renderedEvents = transcriptEvents(events, job, ownStoredMessages, historyLoading);
  const liveStatus = agentLiveStatus(job, events);

  const { attempts, currentAttemptIndex, telemetryAttemptIndex } = projectAttempts(job, events);

  const thread = projectThread(
    job,
    events,
    { thread: options.thread, threadEvents: options.threadEvents, historyLoading },
    projectHistory,
  );
  const publishedPrUrl = thread.publishedPrUrl;

  return {
    id: job.id,
    createdAt: job.created_at,
    startedAt: job.started_at,
    finishedAt: job.finished_at,
    currentAttemptId: job.current_attempt_id,
    pinnedAt: job.pinned_at,
    archivedAt: job.archived_at,
    title: job.title ?? options.thread?.title ?? job.task_prompt.split('\n')[0].slice(0, 80),
    prompt: job.task_prompt,
    state: toDisplayState(job),
    cancelRequested: job.cancel_requested,
    conversationState: job.conversation_state ?? undefined,
    stateNote: job.detail ? ownerFailureMessage(job.detail) : undefined,
    ...(failureCause ? { failureCause } : {}),
    // A temporary workspace has no repository; it is keyed and grouped under
    // the reserved project key instead (see `temporaryWorkspace`).
    repo: projectKey(job.repo),
    baseRef: job.base_ref ?? undefined,
    baseSha: (job.base_sha ?? '').slice(0, 7),
    // The conversation workspace's local branch, shared by every turn. A
    // temporary workspace has no repository, so it has no branch at all.
    branch:
      job.output_branch ??
      (job.repo === null ? '' : `agent/${job.thread_id ?? options.thread?.thread_id ?? job.id}`),
    runtime: job.runtime,
    model: job.model,
    ...(job.model_connection_id ? { modelConnectionId: job.model_connection_id } : {}),
    reasoningEffort: job.reasoning_effort ?? undefined,
    skills: job.skills ?? [],
    // The conversation's goal, and this turn's relationship to it. Assigned
    // after the literal rather than spread inside it: a ternary returning an
    // object, spread into another object, is indented differently by Prettier
    // and ESLint and the two refuse to agree. Both fields are optional; a
    // goalless conversation, or an older deployment, carries neither.
    ...(job.goal ? { goal: toDisplayGoal(job.goal) } : {}),
    // Through the same helper the stored turns use, not a second copy of it:
    // written out inline, every job got a `goalTurn` — including the ones whose
    // conversation has no goal at all — and "absent" stopped meaning anything.
    ...(storedGoalTurn(job) ? { goalTurn: storedGoalTurn(job) } : {}),
    ...(job.continuity_mode ? { continuityMode: job.continuity_mode } : {}),
    ...(job.context_status ? { contextStatus: job.context_status } : {}),
    ...(job.resident_state ? { residentState: job.resident_state } : {}),
    ...(job.sandbox_id ? { sandboxId: job.sandbox_id } : {}),
    ...(job.workspace_shared ? { workspaceShared: true } : {}),
    ...(job.native_state ? { nativeState: job.native_state } : {}),
    ...(job.native_fork_available ? { nativeForkAvailable: true } : {}),
    ...(job.workspace_pr_url ? { workspacePrUrl: job.workspace_pr_url } : {}),
    ...(typeof job.workspace_epoch === 'number' ? { workspaceEpoch: job.workspace_epoch } : {}),
    ...(typeof job.duration_ms === 'number' ? { durationMs: job.duration_ms } : {}),
    ...(job.resources ? { resources: toTurnResources(job.resources) } : {}),
    sandbox: sandboxLabel(events, job.current_attempt_id),
    attempts,
    attemptTelemetryAvailable: job.attempt_telemetry !== undefined,
    currentAttemptNo: currentAttemptIndex >= 0 ? currentAttemptIndex + 1 : undefined,
    telemetryAttemptNo: telemetryAttemptIndex >= 0 ? telemetryAttemptIndex + 1 : undefined,
    events: renderedEvents,
    liveNote: liveStatus?.note,
    liveNoteStartedAt: liveStatus?.startedAt,
    ...workspaceGitProjection(parsed.files, options),
    workspaceDiffAvailable: typeof options.patch === 'string',
    diffFileDetails: parsed.details,
    diffStat: parsed.stat,
    gates: [],
    prLabel: publishedPrUrl ? `draft PR ${publishedPrUrl.split('/').pop()}` : undefined,
    prUrl: publishedPrUrl ?? undefined,
    threadId: job.thread_id ?? options.thread?.thread_id ?? undefined,
    parentJobId: job.parent_job_id ?? undefined,
    turnNo: job.turn_no,
    threadMessages: thread.threadMessages,
    ...(thread.historyTurns ? { historyTurns: thread.historyTurns } : {}),
    historyIncludesPrompt: thread.historyIncludesPrompt,
  };
}

/** The attempts a job's event log shows, and where its current and telemetry attempts fall. */
function projectAttempts(job: AgentJobApi, events: AgentJobEventApi[]) {
  // Attempts are inferred from the event log rather than fetched: an attempt
  // is visible once it writes events. Supersession alone does not prove a
  // retry exists: lost retained sessions can fail without a replacement.
  const attemptIds = [...new Set(events.map((event) => event.attempt_id))].sort((a, b) => a - b);
  const currentAttemptIndex =
    job.current_attempt_id === null ? -1 : attemptIds.indexOf(job.current_attempt_id);
  const telemetryAttemptIndex =
    job.telemetry_attempt_id == null ? -1 : attemptIds.indexOf(job.telemetry_attempt_id);
  const supersededIds = new Set(
    events
      .filter((event) => event.event_type === 'attempt_superseded')
      .map((event) => event.attempt_id),
  );
  const telemetryByAttemptId = new Map(
    (job.attempt_telemetry ?? []).map((attempt) => [attempt.attempt_id, attempt]),
  );
  const attempts = attemptIds.map((id, index): AgentAttempt => {
    const recorded = telemetryByAttemptId.get(id);
    // Only legacy servers need the top-level fallback. Its identity belongs
    // to the fetched snapshot, not whichever attempt SSE has since started.
    let telemetry: AgentAttempt['telemetry'];
    if (recorded) {
      telemetry = toAttemptTelemetry(recorded);
    } else if (
      job.attempt_telemetry === undefined &&
      id ===
        (job.telemetry_attempt_id === undefined ? job.current_attempt_id : job.telemetry_attempt_id)
    ) {
      telemetry = {
        ...(job.continuity_mode ? { continuityMode: job.continuity_mode } : {}),
        ...(job.context_status ? { contextStatus: job.context_status } : {}),
        ...(typeof job.duration_ms === 'number' ? { durationMs: job.duration_ms } : {}),
        ...(job.resources ? { resources: toTurnResources(job.resources) } : {}),
      };
    }
    return {
      id,
      no: index + 1,
      status: supersededIds.has(id)
        ? 'superseded'
        : job.state === 'running' || job.state === 'publishing'
          ? 'live'
          : 'finished',
      note: supersededIds.has(id)
        ? index < attemptIds.length - 1
          ? 'This run was interrupted. Another run started.'
          : 'This run was interrupted.'
        : undefined,
      ...(telemetry ? { telemetry } : {}),
    };
  });
  return { attempts, currentAttemptIndex, telemetryAttemptIndex };
}

// The inspectable projection of the stored stream.
//
// `trajectory.ts` keeps one row per stored event and is deliberately literal:
// nothing is folded, merged or hidden, and the session export depends on that.
// This module is the second layer the inspector reads — an assistant message
// with the blocks it carried, a tool call with the result that answered it,
// and a status computed only from evidence the platform actually recorded.
//
// Two rules shape everything below. Identity is scoped to `jobId + attemptId`,
// because a retried turn repeats its tool ids and its native message ids.
// Selection uses a stable key derived from that identity, never a row index or
// a visible step number, because both move when history backfills.

import type { AgentJobEventApi, AgentThreadEventApi } from '@/lib/api/agents';
import { preview, toTrajectory, type TrajectoryLane, type TrajectoryRow } from './trajectory';

/** A stored event, with or without the conversation labels a thread read adds. */
export type StoredEvent = AgentJobEventApi | AgentThreadEventApi;

/** The three detail shapes the inspector can render. */
export type StepKind = 'assistant' | 'tool' | 'event';

/**
 * What the stored records can prove about a step. `waiting` is the only state
 * that claims something is still in flight, and it requires an active attempt.
 */
export type StepStatus =
  | 'completed'
  | 'failed'
  | 'waiting'
  | 'cancelled'
  | 'recorded'
  | 'unknown'
  | 'not_recorded';

export const STATUS_LABEL: Record<StepStatus, string> = {
  completed: 'Completed',
  failed: 'Failed',
  waiting: 'Waiting for result',
  cancelled: 'Cancelled',
  recorded: 'Recorded',
  unknown: 'Unknown',
  not_recorded: 'Not recorded',
};

/** One recorded content block of a native message, in the order it was stored. */
export interface AssistantBlock {
  type: 'thinking' | 'text';
  text: string;
}

/**
 * Usage attributable to a single native message. Every field is null when the
 * runtime did not record it; nothing here is derived from a turn total.
 */
export interface AssistantUsage {
  outputTokens: number | null;
  inputTokens: number | null;
  reasoningTokens: number | null;
  contentTokens: number | null;
  cacheReadTokens: number | null;
  cacheWriteTokens: number | null;
}

/** The result that answered a call, as the stored event records it. */
export interface ToolResultView {
  text: string;
  isError: boolean;
  exitCode: number | null;
  eventIds: number[];
}

/**
 * A time interval with the evidence that produced it.
 *
 * `session_timestamps` brackets the call record and the result record, which
 * includes queueing and forwarding; it is not tool CPU time. `tool_execution_hooks`
 * is the sandbox's own boundary marker and is only attached by the inspector
 * when the attempt telemetry reports it as exact.
 */
export interface StepTiming {
  source: 'tool_execution_hooks' | 'session_timestamps' | null;
  startedAt: string | null;
  finishedAt: string | null;
  durationMs: number | null;
}

export interface TrajectoryStep {
  /** Stable across backfill: derived from recorded identity, never a position. */
  key: string;
  kind: StepKind;
  jobId: string;
  turnNo: number;
  attemptId: number;
  /** Display number within its turn; recomputed, never used as identity. */
  stepNo: number;
  anchorEventId: number;
  sourceEvents: StoredEvent[];
  lane: TrajectoryLane;
  actor: TrajectoryRow['actor'];
  label: string;
  detail: string;
  at: string | null;
  isError: boolean;
  status: StepStatus;
  /** True when any source payload was truncated by the platform. */
  truncated: boolean;
  timing: StepTiming;
  /** Assistant steps: the native message identity the runtime recorded. */
  nativeMessageId?: string;
  blocks?: AssistantBlock[];
  usage?: AssistantUsage;
  /** Assistant steps: the tool steps that carry this message's calls. */
  toolKeys?: string[];
  /** Tool steps: the call identity and the message that issued it. */
  callId?: string;
  toolName?: string;
  parentKey?: string;
  /** Tool steps: the argument value, only when an argument key was recorded. */
  payloadValue?: unknown;
  /** Tool steps: a recorded one-line argument preview, when that is all there is. */
  payloadPreview?: string;
  result?: ToolResultView;
  /**
   * Set when the records disagree — a repeated call id, or two results for one
   * call. The step stays visible and stops claiming a verified pairing.
   */
  ambiguity?: 'duplicate_call' | 'duplicate_result';
}

/** What the projection needs to know about a turn that the events cannot say. */
export interface TurnContext {
  /** Whether this attempt is the one still running, from the job row. */
  isAttemptActive: (jobId: string, attemptId: number) => boolean;
}

// Server-side job states that can still produce a record. A queued turn is one
// that has not started yet; the rest are running or finishing.
const ACTIVE_JOB_STATES = new Set(['running', 'queued', 'publishing']);

/**
 * Build the default context from thread job rows: the attempt a live turn names
 * is active, everything else has already stopped.
 *
 * A row that names no current attempt is a turn between attempts — superseded
 * and waiting for its replacement, or queued and not started. Nothing is in
 * flight there, so a call that never got a result cannot still be waiting for
 * one. Only a row that omits the field leaves the question open.
 */
export function turnContextFromJobs(
  jobs: Array<{ id: string; state: string; current_attempt_id?: number | null }>,
): TurnContext {
  const byId = new Map(jobs.map((job) => [job.id, job]));
  return {
    isAttemptActive: (jobId, attemptId) => {
      const job = byId.get(jobId);
      if (!job || !ACTIVE_JOB_STATES.has(job.state)) return false;
      if (job.current_attempt_id === null) return false;
      if (job.current_attempt_id === undefined) return true;
      return job.current_attempt_id === attemptId;
    },
  };
}

const INPUT_KEYS = ['input_tokens', 'input_token_count', 'prompt_tokens'];
const OUTPUT_KEYS = ['output_tokens', 'output_token_count', 'completion_tokens'];
const CACHE_READ_KEYS = ['cache_read_input_tokens', 'cache_read_tokens', 'cached_tokens'];
const CACHE_WRITE_KEYS = ['cache_creation_input_tokens', 'cache_write_tokens'];
const REASONING_KEYS = ['reasoning_tokens', 'reasoning_output_tokens', 'reasoning'];
/** Only used when a runtime states the text share directly; never subtracted. */
const CONTENT_KEYS = ['content_tokens', 'text_tokens'];

const RESULT_TEXT_KEYS = ['content', 'output', 'aggregated_output', 'text'] as const;

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function asText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (value === null || value === undefined) return '';
  try {
    return JSON.stringify(value);
  } catch {
    return '';
  }
}

/** First recorded finite, non-negative count among a runtime's spellings. */
function firstCount(record: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0) return value;
  }
  return null;
}

function scopeOf(event: StoredEvent): string {
  return `${jobIdOf(event)}:${event.attempt_id}`;
}

/** A conversation read labels its events; a job-scoped read does not. */
export function jobIdOf(event: StoredEvent): string {
  return (event as AgentThreadEventApi).job_id ?? '';
}

function turnNoOf(event: StoredEvent): number {
  return (event as AgentThreadEventApi).turn_no ?? 1;
}

function usageIdOf(payload: Record<string, unknown>): string {
  const value = payload.usage_id;
  return typeof value === 'string' ? value : '';
}

function callIdOf(event: StoredEvent): string {
  const payload = asRecord(event.payload);
  return event.event_type === 'tool_use' ? asText(payload.id) : '';
}

function resultCallIdOf(event: AgentThreadEventApi | AgentJobEventApi): string {
  const payload = asRecord(event.payload);
  return event.event_type === 'tool_result' ? asText(payload.tool_use_id) : '';
}

export function isTruncatedPayload(payload: Record<string, unknown>): boolean {
  return payload.truncated === true || typeof payload.truncation_notice === 'string';
}

function assistantKeyOf(event: StoredEvent, usageId: string): string {
  return usageId
    ? `assistant:${jobIdOf(event)}:${event.attempt_id}:${usageId}`
    : `assistant:${jobIdOf(event)}:${event.attempt_id}:event:${event.id}`;
}

function toolKeyOf(event: StoredEvent, callId: string): string {
  const scope = `tool:${jobIdOf(event)}:${event.attempt_id}`;
  // A call with no recorded id has no identity to share, so the record's own
  // event id keeps it a step of its own instead of folding it into the call
  // that happened to be stored before it.
  return callId ? `${scope}:${callId}` : `${scope}:event:${event.id}`;
}

function timingOf(call: StoredEvent | undefined, result: StoredEvent | undefined): StepTiming {
  if (!call) {
    return {
      source: result ? 'session_timestamps' : null,
      startedAt: null,
      finishedAt: result?.created_at ?? null,
      durationMs: null,
    };
  }
  if (!result) {
    return { source: null, startedAt: call.created_at ?? null, finishedAt: null, durationMs: null };
  }
  const started = call.created_at ? Date.parse(call.created_at) : NaN;
  const ended = result.created_at ? Date.parse(result.created_at) : NaN;
  // A negative span is a record we cannot read, not a zero-length call.
  const durationMs =
    Number.isFinite(started) && Number.isFinite(ended) && ended >= started ? ended - started : null;
  return {
    source: 'session_timestamps',
    startedAt: call.created_at ?? null,
    finishedAt: result.created_at ?? null,
    durationMs,
  };
}

function statusOfTool(
  call: StoredEvent,
  result: StoredEvent | undefined,
  context: TurnContext,
): StepStatus {
  if (result) {
    const payload = asRecord(result.payload);
    const exitCode = payload.exit_code;
    const failed = payload.is_error === true || (typeof exitCode === 'number' && exitCode !== 0);
    return failed ? 'failed' : 'completed';
  }
  // No result record. Only an attempt that is still running can still produce one.
  if (context.isAttemptActive(jobIdOf(call), call.attempt_id)) return 'waiting';
  return 'not_recorded';
}

function blockOf(event: StoredEvent): AssistantBlock | null {
  const payload = asRecord(event.payload);
  if (event.event_type === 'thinking') {
    return { type: 'thinking', text: asText(payload.text) };
  }
  if (event.event_type === 'message') {
    return { type: 'text', text: asText(payload.text) };
  }
  return null;
}

/**
 * Project stored events into the steps a reader can select.
 *
 * Every stored event remains reachable: tool results join their call, and an
 * event that belongs to no call or message becomes its own step. Nothing is
 * dropped to make the list tidier.
 */
export function buildTrajectorySteps(
  events: StoredEvent[],
  context: TurnContext = { isAttemptActive: () => false },
): TrajectoryStep[] {
  // Pass one: the identities a step can be matched on. Results are indexed
  // before calls are emitted so a call always finds the answer that arrived.
  const resultsByCall = new Map<string, StoredEvent[]>();
  const callCounts = new Map<string, number>();
  const assistantFirstIndex = new Map<string, number>();
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index];
    const callId = resultCallIdOf(event);
    if (callId) {
      const key = `${scopeOf(event)}:${callId}`;
      resultsByCall.set(key, [...(resultsByCall.get(key) ?? []), event]);
    }
    const call = callIdOf(event);
    if (call) {
      const key = `${scopeOf(event)}:${call}`;
      callCounts.set(key, (callCounts.get(key) ?? 0) + 1);
    }
    if (event.event_type === 'message' || event.event_type === 'thinking') {
      const key = assistantKeyOf(event, usageIdOf(asRecord(event.payload)));
      if (!assistantFirstIndex.has(key)) assistantFirstIndex.set(key, index);
    }
    if (event.event_type === 'tool_use') {
      const usageId = usageIdOf(asRecord(event.payload));
      if (usageId) {
        const key = assistantKeyOf(event, usageId);
        if (!assistantFirstIndex.has(key)) assistantFirstIndex.set(key, index);
      }
    }
  }

  const steps: TrajectoryStep[] = [];
  const byKey = new Map<string, TrajectoryStep>();
  const toolStepByKey = new Map<string, TrajectoryStep>();

  const pushAssistant = (event: StoredEvent, usageId: string): TrajectoryStep => {
    const key = assistantKeyOf(event, usageId);
    const block = blockOf(event);
    const step: TrajectoryStep = {
      key,
      kind: 'assistant',
      jobId: jobIdOf(event),
      turnNo: turnNoOf(event),
      attemptId: event.attempt_id,
      stepNo: 0,
      anchorEventId: event.id,
      sourceEvents: [event],
      lane: 'model',
      actor: 'assistant',
      label: block?.type === 'thinking' ? 'Thinking' : 'Assistant message',
      detail: block ? preview(block.text) : '',
      at: event.created_at ?? null,
      isError: false,
      status: block?.type === 'text' ? 'completed' : 'recorded',
      truncated: isTruncatedPayload(asRecord(event.payload)),
      timing: { source: null, startedAt: null, finishedAt: null, durationMs: null },
      ...(usageId ? { nativeMessageId: usageId } : {}),
      blocks: block ? [block] : [],
      toolKeys: [],
    };
    byKey.set(key, step);
    steps.push(step);
    return step;
  };

  const pushTool = (event: StoredEvent): TrajectoryStep => {
    const payload = asRecord(event.payload);
    const callId = asText(payload.id);
    const key = toolKeyOf(event, callId);
    const hasInput = Object.prototype.hasOwnProperty.call(payload, 'input');
    const resultEvents = resultsByCall.get(`${scopeOf(event)}:${callId}`) ?? [];
    // A call id stored twice, or a call answered more than once, leaves the
    // pairing unverified. The records are all kept, but a status and a duration
    // would be claims about a pairing the platform cannot prove, so neither is
    // computed: the step reports what it can, which is that the records exist.
    const repeatedCall = callId !== '' && (callCounts.get(`${scopeOf(event)}:${callId}`) ?? 0) > 1;
    const ambiguous = repeatedCall || resultEvents.length > 1;
    const step: TrajectoryStep = {
      key,
      kind: 'tool',
      jobId: jobIdOf(event),
      turnNo: turnNoOf(event),
      attemptId: event.attempt_id,
      stepNo: 0,
      anchorEventId: event.id,
      // The call record leads; its answers stay reachable through the same
      // step, which is what Raw, Result and the truncation marker read.
      sourceEvents: [event, ...resultEvents],
      lane: 'tools',
      actor: 'tool',
      label: asText(payload.name || payload.tool) || 'tool',
      detail: preview(hasInput ? payload.input : (payload.detail ?? payload.text)),
      at: event.created_at ?? null,
      isError: false,
      status: ambiguous ? 'unknown' : statusOfTool(event, resultEvents[0], context),
      truncated:
        isTruncatedPayload(payload) ||
        resultEvents.some((item) => isTruncatedPayload(asRecord(item.payload))),
      timing: ambiguous
        ? { source: null, startedAt: event.created_at ?? null, finishedAt: null, durationMs: null }
        : timingOf(event, resultEvents[0]),
      callId,
      toolName: asText(payload.name || payload.tool) || 'tool',
      ...(hasInput ? { payloadValue: payload.input } : {}),
      ...(!hasInput && payload.detail !== undefined
        ? { payloadPreview: preview(payload.detail, 200) }
        : {}),
      ...(resultEvents.length ? { result: resultView(resultEvents) } : {}),
      ...(repeatedCall ? { ambiguity: 'duplicate_call' as const } : {}),
      ...(resultEvents.length > 1 ? { ambiguity: 'duplicate_result' as const } : {}),
    };
    toolStepByKey.set(key, step);
    byKey.set(key, step);
    steps.push(step);
    return step;
  };

  for (const event of events) {
    const payload = asRecord(event.payload);
    const usageId = usageIdOf(payload);

    if (event.event_type === 'message' || event.event_type === 'thinking') {
      const key = assistantKeyOf(event, usageId);
      const current = byKey.get(key);
      if (current && current.kind === 'assistant') {
        const block = blockOf(event);
        current.sourceEvents.push(event);
        if (block) {
          current.blocks = [...(current.blocks ?? []), block];
          current.detail = preview(
            (current.blocks ?? [])
              .filter((item) => item.type === 'text')
              .map((item) => item.text)
              .join(' ') || block.text,
          );
          if (block.type === 'text') {
            current.status = 'completed';
            current.label = 'Assistant message';
          }
        }
        current.truncated = current.truncated || isTruncatedPayload(payload);
        continue;
      }
      pushAssistant(event, usageId);
      continue;
    }

    if (event.event_type === 'tool_use') {
      const callId = asText(payload.id);
      const key = toolKeyOf(event, callId);
      const existing = toolStepByKey.get(key);
      if (existing) {
        // The same call id twice in one attempt: keep both records on the step
        // and stop claiming a verified pairing.
        existing.sourceEvents.push(event);
        existing.ambiguity = 'duplicate_call';
        continue;
      }
      const parentKey = usageId ? assistantKeyOf(event, usageId) : '';
      if (parentKey && !byKey.has(parentKey)) {
        // The parent message's blocks may be stored after the call it issued;
        // the design orders the message first, so open it here.
        pushAssistant(event, usageId);
      }
      const step = pushTool(event);
      if (parentKey && byKey.get(parentKey)?.kind === 'assistant') {
        step.parentKey = parentKey;
        const parent = byKey.get(parentKey);
        parent?.toolKeys?.push(step.key);
      }
      continue;
    }

    if (event.event_type === 'tool_result') {
      const callId = asText(payload.tool_use_id);
      if (callId && toolStepByKey.has(toolKeyOf(event, callId))) continue; // joined to its call
      steps.push({
        key: `tool-result:${jobIdOf(event)}:${event.attempt_id}:${event.id}`,
        kind: 'tool',
        jobId: jobIdOf(event),
        turnNo: turnNoOf(event),
        attemptId: event.attempt_id,
        stepNo: 0,
        anchorEventId: event.id,
        sourceEvents: [event],
        lane: 'tools',
        actor: 'tool',
        label: 'Tool result',
        detail: preview(resultTextOf(payload)),
        at: event.created_at ?? null,
        isError: payload.is_error === true,
        status: 'recorded',
        truncated: isTruncatedPayload(payload),
        timing: timingOf(undefined, event),
        ...(callId ? { callId } : {}),
        result: resultView([event]),
      });
      continue;
    }

    const row = toTrajectory([event])[0];
    steps.push({
      key: `event:${jobIdOf(event)}:${event.attempt_id}:${event.id}`,
      kind: 'event',
      jobId: jobIdOf(event),
      turnNo: turnNoOf(event),
      attemptId: event.attempt_id,
      stepNo: 0,
      anchorEventId: event.id,
      sourceEvents: [event],
      lane: row.lane,
      actor: row.actor,
      label: row.label,
      detail: row.detail,
      at: event.created_at ?? null,
      isError: row.isError,
      status: event.event_type === 'error' ? 'failed' : 'recorded',
      truncated: isTruncatedPayload(payload),
      timing: { source: null, startedAt: null, finishedAt: null, durationMs: null },
    });
  }

  // Usage that a native message recorded, de-duplicated by message identity:
  // the same response repeats its counters on every block it emitted.
  for (const step of steps) {
    if (step.kind !== 'assistant') continue;
    step.usage = usageOf(step.sourceEvents);
  }

  // Display numbers are a property of the whole turn, so they do not shift
  // when the reader searches; selection never uses them.
  const perTurn = new Map<string, number>();
  for (const step of steps) {
    const turn = `${step.turnNo}:${step.jobId}`;
    const next = (perTurn.get(turn) ?? 0) + 1;
    perTurn.set(turn, next);
    step.stepNo = next;
  }

  return steps;
}

function resultTextOf(payload: Record<string, unknown>): string {
  for (const key of RESULT_TEXT_KEYS) {
    if (payload[key] !== undefined && payload[key] !== null) return asText(payload[key]);
  }
  return '';
}

function resultView(events: StoredEvent[]): ToolResultView {
  const payload = asRecord(events[0].payload);
  const exitCode = payload.exit_code;
  return {
    text: resultTextOf(payload),
    isError:
      events.some((event) => asRecord(event.payload).is_error === true) ||
      (typeof exitCode === 'number' && exitCode !== 0),
    exitCode: typeof exitCode === 'number' ? exitCode : null,
    eventIds: events.map((event) => event.id),
  };
}

function usageOf(events: StoredEvent[]): AssistantUsage {
  for (const event of events) {
    const usage = asRecord(asRecord(event.payload).usage);
    if (Object.keys(usage).length === 0) continue;
    return {
      outputTokens: firstCount(usage, OUTPUT_KEYS),
      inputTokens: firstCount(usage, INPUT_KEYS),
      reasoningTokens: firstCount(usage, REASONING_KEYS),
      contentTokens: firstCount(usage, CONTENT_KEYS),
      cacheReadTokens: firstCount(usage, CACHE_READ_KEYS),
      cacheWriteTokens: firstCount(usage, CACHE_WRITE_KEYS),
    };
  }
  return {
    outputTokens: null,
    inputTokens: null,
    reasoningTokens: null,
    contentTokens: null,
    cacheReadTokens: null,
    cacheWriteTokens: null,
  };
}

/**
 * Filter steps by a free-text query.
 *
 * Matching the visible label and preview keeps the filter honest: a step the
 * query cannot be seen to match reads as a broken search.
 */
export function filterSteps(steps: TrajectoryStep[], query: string): TrajectoryStep[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return steps;
  return steps.filter((step) =>
    `${step.label} ${step.detail} ${step.kind}`.toLowerCase().includes(needle),
  );
}

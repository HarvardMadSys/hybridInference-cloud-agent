import { isActiveJobState } from './conversationJob';
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
import {
  rawNotification,
  notificationHasError,
  isRuntimeNotification,
  isRuntimeTelemetry,
  projectProtocolEvents,
} from './trajectoryProtocol';

/** A stored event, with or without the conversation labels a thread read adds. */
export type StoredEvent = AgentJobEventApi | AgentThreadEventApi;

/** The three detail shapes the inspector can render. */
export type StepKind = 'user' | 'assistant' | 'tool' | 'event';

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
  channel?: string;
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

/** One MCP server a runtime named at init, and the status it gave it. */
export interface HarnessMcpServer {
  name: string;
  status: string | null;
}

/**
 * What a runtime said about the harness it started, quoted from the record.
 *
 * Claude Code builds its own system prompt inside the CLI and never puts one on
 * the wire, so this is not a prompt and is never presented as one. It is the
 * configuration the CLI itself attested at init: the tool menu it loaded, the
 * servers it reached, and the permission mode it ran under. Nothing here is
 * derived from a name or a convention — a reader sees what the runtime said.
 */
export interface HarnessInventory {
  /** The runtime's own statement of the CLI it ran, when it made one. */
  runtimeVersion: string | null;
  /** The model the runtime reported starting with. */
  model: string | null;
  permissionMode: string | null;
  tools: string[];
  /** True when the platform's bound cut the tool list short. */
  toolsTruncated: boolean;
  mcpServers: HarnessMcpServer[];
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
  nativeItemId?: string;
  streaming?: boolean;
  inputText?: string;
  blocks?: AssistantBlock[];
  usage?: AssistantUsage;
  /** Assistant steps: the tool steps that carry this message's calls. */
  toolKeys?: string[];
  /** Tool steps: the call identity and the message that issued it. */
  callId?: string;
  toolName?: string;
  parentKey?: string;
  /** The tool that delegated this agent, separate from model-message ownership. */
  delegationParentKey?: string;
  /** Explicitly recorded activity from the agent this tool delegated. */
  delegatedKeys?: string[];
  /** Tool steps: the argument value, only when an argument key was recorded. */
  payloadValue?: unknown;
  /** Tool steps: a recorded one-line argument preview, when that is all there is. */
  payloadPreview?: string;
  result?: ToolResultView;
  outputPreview?: string;
  /**
   * Set when the records disagree — a repeated call id, or two results for one
   * call. The step stays visible and stops claiming a verified pairing.
   */
  ambiguity?: 'duplicate_call' | 'duplicate_result';
  /**
   * Event steps: what the runtime reported about the harness it started. Only
   * an event that carries such a report has one; the inspector shows the tabs
   * that read it only then.
   */
  harness?: HarnessInventory;
  /**
   * Runtime bookkeeping: excluded from execution counts, the timeline and the
   * step list. The stored records stay in Run details and in Session log.
   */
  diagnostic?: boolean;
}

/** What the projection needs to know about a turn that the events cannot say. */
export interface TurnContext {
  /** Whether this attempt is the one still running, from the job row. */
  isAttemptActive: (jobId: string, attemptId: number) => boolean;
  inputs?: Array<{ jobId: string; turnNo: number; text: string; at: string | null }>;
}

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
  jobs: Array<{
    id: string;
    state: string;
    current_attempt_id?: number | null;
    task_prompt?: string;
    turn_no?: number;
    created_at?: string | null;
  }>,
): TurnContext {
  const byId = new Map(jobs.map((job) => [job.id, job]));
  return {
    inputs: jobs.flatMap((job) => {
      if (typeof job.task_prompt !== 'string') return [];
      return [
        {
          jobId: job.id,
          turnNo: job.turn_no ?? 1,
          text: job.task_prompt,
          at: job.created_at ?? null,
        },
      ];
    }),
    isAttemptActive: (jobId, attemptId) => {
      const job = byId.get(jobId);
      if (!job || !isActiveJobState(job.state)) return false;
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

/** The tool names a report listed, in the order it listed them. */
function harnessTools(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((name): name is string => typeof name === 'string' && name.trim().length > 0);
}

/** The MCP servers a report named, each with the status it gave that server. */
function harnessMcpServers(value: unknown): HarnessMcpServer[] {
  if (!Array.isArray(value)) return [];
  const servers: HarnessMcpServer[] = [];
  for (const entry of value) {
    const server = asRecord(entry);
    const name = typeof server.name === 'string' ? server.name.trim() : '';
    if (!name) continue;
    servers.push({ name, status: typeof server.status === 'string' ? server.status : null });
  }
  return servers;
}

/**
 * Read the harness a lifecycle event reported, if it reported one.
 *
 * An inventory is an object or it is nothing. The platform bounds an event
 * payload by replacing an oversized value with a text preview, and a preview is
 * not a record of what a session loaded, so it is refused here rather than
 * parsed for whatever names it happens to contain. An inventory that names no
 * tool, no server and no permission mode is nothing either: the tabs it would
 * open have nothing to show.
 */
function harnessOf(payload: Record<string, unknown>): HarnessInventory | null {
  const reported = payload.harness;
  if (!reported || typeof reported !== 'object' || Array.isArray(reported)) return null;
  const harness = reported as Record<string, unknown>;
  const tools = harnessTools(harness.tools);
  const mcpServers = harnessMcpServers(harness.mcp_servers);
  const mode = harness.permission_mode;
  const permissionMode = typeof mode === 'string' && mode.trim() ? mode : null;
  if (tools.length === 0 && mcpServers.length === 0 && permissionMode === null) return null;
  return {
    runtimeVersion: typeof payload.runtime_version === 'string' ? payload.runtime_version : null,
    model: typeof payload.model === 'string' ? payload.model : null,
    permissionMode,
    tools,
    toolsTruncated: harness.tools_truncated === true,
    mcpServers,
  };
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

function messageIdOf(payload: Record<string, unknown>): string {
  return typeof payload.native_item_id === 'string' ? payload.native_item_id : usageIdOf(payload);
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

// Adapted tool rows can address their diagnostic step without constructing the
// rest of the trajectory. Native protocol rows supply their projected call id.
export function toolStepKey(event: StoredEvent, callId = callIdOf(event)): string {
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
    if (payload.native_tool_status === 'cancelled') return 'cancelled';
    const exitCode = payload.exit_code;
    const failed = payload.is_error === true || (typeof exitCode === 'number' && exitCode !== 0);
    return failed ? 'failed' : 'completed';
  }
  const nativeStatus = asRecord(call.payload).native_tool_status;
  if (nativeStatus === 'completed') return 'completed';
  if (nativeStatus === 'failed') return 'failed';
  if (nativeStatus === 'cancelled') return 'cancelled';
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
 * Every stored event remains reachable: tool results join their call, known
 * protocol notifications share one log step per attempt, and other events
 * become their own steps. Grouping never changes the session-log export.
 */
export function buildTrajectorySteps(
  storedEvents: StoredEvent[],
  context: TurnContext = { isAttemptActive: () => false },
): TrajectoryStep[] {
  const singleInput = context.inputs?.length === 1 ? context.inputs[0] : undefined;
  const events = projectProtocolEvents(storedEvents).map((event) =>
    !('job_id' in event) && singleInput
      ? { ...event, job_id: singleInput.jobId, turn_no: singleInput.turnNo }
      : event,
  );
  const originals = new Map(storedEvents.map((event) => [event.id, event]));
  // Pass one: the identities a step can be matched on. Results are indexed
  // before calls are emitted so a call always finds the answer that arrived.
  const resultsByCall = new Map<string, StoredEvent[]>();
  const callCounts = new Map<string, number>();
  const outputByCall = new Map<string, StoredEvent[]>();
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index];
    if (event.event_type === 'tool_output') {
      const id = asText(asRecord(event.payload).tool_use_id);
      const key = `${scopeOf(event)}:${id}`;
      outputByCall.set(key, [...(outputByCall.get(key) ?? []), event]);
    }
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
  }

  const steps: TrajectoryStep[] = [];
  const byKey = new Map<string, TrajectoryStep>();
  const toolStepByKey = new Map<string, TrajectoryStep>();

  const pushUser = (
    jobId: string,
    turnNo: number,
    text: string,
    at: string | null,
    event?: StoredEvent,
  ): TrajectoryStep => {
    const key = `user:${jobId || `attempt:${event?.attempt_id}`}`;
    const current = byKey.get(key);
    if (current) {
      if (event) current.sourceEvents.push(event);
      return current;
    }
    const step: TrajectoryStep = {
      key,
      kind: 'user',
      jobId,
      turnNo,
      attemptId: event?.attempt_id ?? 0,
      stepNo: 0,
      anchorEventId: event?.id ?? 0,
      sourceEvents: event ? [event] : [],
      lane: 'input',
      actor: 'user',
      label: 'User message',
      detail: preview(text),
      inputText: text,
      at,
      isError: false,
      status: 'recorded',
      truncated: false,
      timing: { source: null, startedAt: null, finishedAt: null, durationMs: null },
    };
    byKey.set(key, step);
    steps.push(step);
    return step;
  };
  const inputs = new Map((context.inputs ?? []).map((input) => [input.jobId, input]));

  const pushAssistant = (event: StoredEvent, usageId: string): TrajectoryStep => {
    const key = assistantKeyOf(event, usageId);
    const block = blockOf(event);
    const payload = asRecord(event.payload);
    const streaming = typeof payload.stream_state === 'string';
    if (block && streaming) block.channel = asText(payload.stream_channel) || 'summary';
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
      status: streaming
        ? context.isAttemptActive(jobIdOf(event), event.attempt_id)
          ? 'waiting'
          : 'recorded'
        : block?.type === 'text'
          ? 'completed'
          : 'recorded',
      truncated: isTruncatedPayload(asRecord(event.payload)),
      timing: { source: null, startedAt: null, finishedAt: null, durationMs: null },
      ...(typeof payload.native_item_id === 'string'
        ? { nativeItemId: payload.native_item_id }
        : usageId
          ? { nativeMessageId: usageId }
          : {}),
      ...(streaming ? { streaming: true } : {}),
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
    const key = toolStepKey(event, callId);
    const hasInput = Object.prototype.hasOwnProperty.call(payload, 'input');
    const resultEvents = resultsByCall.get(`${scopeOf(event)}:${callId}`) ?? [];
    const outputs = outputByCall.get(`${scopeOf(event)}:${callId}`) ?? [];
    // A call id stored twice, or a call answered more than once, leaves the
    // pairing unverified. The records are all kept, but a status and a duration
    // would be claims about a pairing the platform cannot prove, so neither is
    // computed: the step reports what it can, which is that the records exist.
    const repeatedCall = callId !== '' && (callCounts.get(`${scopeOf(event)}:${callId}`) ?? 0) > 1;
    const ambiguous = repeatedCall || resultEvents.length > 1;
    const timing: StepTiming = ambiguous
      ? { source: null, startedAt: event.created_at ?? null, finishedAt: null, durationMs: null }
      : timingOf(event, resultEvents[0]);
    if (payload.native_tool_completion_only === true) {
      timing.source = null;
      timing.startedAt = null;
      timing.finishedAt = null;
      timing.durationMs = null;
    }
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
      sourceEvents: [event, ...outputs, ...resultEvents],
      lane: 'tools',
      actor: 'tool',
      label:
        payload.name === 'commandExecution'
          ? 'Shell'
          : asText(payload.name || payload.tool) || 'tool',
      detail: preview(hasInput ? payload.input : (payload.detail ?? payload.text)),
      at: event.created_at ?? null,
      isError:
        payload.is_error === true ||
        resultEvents.some((item) => asRecord(item.payload).is_error === true),
      status: ambiguous ? 'unknown' : statusOfTool(event, resultEvents[0], context),
      truncated:
        isTruncatedPayload(payload) ||
        resultEvents.some((item) => isTruncatedPayload(asRecord(item.payload))),
      timing,
      callId,
      toolName: asText(payload.name || payload.tool) || 'tool',
      ...(hasInput ? { payloadValue: payload.input } : {}),
      ...(!hasInput && payload.detail !== undefined
        ? { payloadPreview: preview(payload.detail, 200) }
        : {}),
      ...(resultEvents.length
        ? { result: resultView(resultEvents) }
        : payload.native_tool_status && payload.output !== undefined
          ? { result: resultView([event]) }
          : {}),
      ...(outputs.length
        ? { outputPreview: outputs.map((item) => asText(asRecord(item.payload).text)).join('') }
        : {}),
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
    const input = inputs.get(jobIdOf(event));
    if (input) pushUser(input.jobId, input.turnNo, input.text, input.at);

    if (event.event_type === 'input') {
      // A subagent's prompt is its delegating call's input, not this turn's.
      if (asText(payload.parent_tool_use_id) || asText(payload.agent_thread_id)) continue;
      const step = pushUser(
        jobIdOf(event),
        turnNoOf(event),
        asText(payload.text),
        event.created_at ?? null,
        event,
      );
      if (!input) {
        step.inputText = asText(payload.text);
        step.detail = preview(payload.text);
      }
      step.truncated ||= isTruncatedPayload(payload);
      continue;
    }

    // An answer still being written: one step per answer, grown by its pieces,
    // until the complete message with the same preview id takes its place.
    const previewId = event.event_type === 'message' ? asText(payload.preview_id) : '';
    const previewKey = `preview:${scopeOf(event)}:${previewId}`;
    if (previewId && typeof payload.stream_state === 'string') {
      const current = byKey.get(previewKey);
      if (current?.blocks) {
        const text =
          payload.stream_state === 'reset'
            ? asText(payload.text)
            : (current.blocks[0]?.text ?? '') + asText(payload.text);
        current.sourceEvents.push(event);
        current.blocks = [{ type: 'text', text, channel: 'summary' }];
        current.detail = preview(text);
        continue;
      }
      const step = pushAssistant(event, '');
      byKey.delete(step.key);
      step.key = previewKey;
      byKey.set(previewKey, step);
      continue;
    }
    let previewSources: StoredEvent[] = [];
    let previewIndex = -1;
    const previewStep = previewId ? byKey.get(previewKey) : undefined;
    if (previewStep) {
      // Its pieces stay reachable through the step that has the whole text.
      previewIndex = steps.indexOf(previewStep);
      steps.splice(previewIndex, 1);
      byKey.delete(previewKey);
      previewSources = previewStep.sourceEvents;
    }

    if (event.event_type === 'message' || event.event_type === 'thinking') {
      const messageId = messageIdOf(payload);
      const key = assistantKeyOf(event, messageId);
      const current = byKey.get(key);
      if (current && current.kind === 'assistant') {
        const block = blockOf(event);
        current.sourceEvents.push(...previewSources, event);
        if (block) {
          if (typeof payload.native_item_id === 'string') {
            const channel = asText(payload.stream_channel) || 'summary';
            const previous = current.blocks?.find(
              (item) => item.type === block.type && (item.channel ?? 'summary') === channel,
            );
            if (payload.stream_state === 'delta') block.text = (previous?.text ?? '') + block.text;
            block.channel = channel;
            current.blocks = [
              ...(current.blocks ?? []).filter(
                (item) => !(item.type === block.type && (item.channel ?? 'summary') === channel),
              ),
              block,
            ];
            current.streaming = typeof payload.stream_state === 'string';
          } else {
            current.blocks = [...(current.blocks ?? []), block];
          }
          current.detail = preview(
            (current.blocks ?? [])
              .filter((item) => item.type === 'text')
              .map((item) => item.text)
              .join(' ') || block.text,
          );
          if (current.streaming) {
            current.status = context.isAttemptActive(jobIdOf(event), event.attempt_id)
              ? 'waiting'
              : 'recorded';
          } else if (block.type === 'text') {
            current.status = 'completed';
            current.label = 'Assistant message';
          } else if (typeof payload.native_item_id === 'string') {
            current.status = 'recorded';
          }
        }
        current.truncated = current.truncated || isTruncatedPayload(payload);
        continue;
      }
      const step = pushAssistant(event, messageId);
      if (previewSources.length) {
        step.sourceEvents.unshift(...previewSources);
        // Where the answer was being written, not after what followed it.
        steps.splice(previewIndex, 0, steps.pop() as TrajectoryStep);
      }
      continue;
    }

    if (event.event_type === 'tool_use') {
      const callId = asText(payload.id);
      const key = toolStepKey(event, callId);
      const existing = toolStepByKey.get(key);
      if (existing) {
        // The same call id twice in one attempt: keep both records on the step
        // and stop claiming a verified pairing.
        existing.sourceEvents.push(event);
        existing.ambiguity = 'duplicate_call';
        continue;
      }
      // DSH groups tool-call blocks by their owning model message. A tool
      // item ID only identifies the call; it cannot create a model message.
      const parentKey = usageId ? assistantKeyOf(event, usageId) : '';
      if (parentKey && !byKey.has(parentKey)) pushAssistant(event, usageId);
      const step = pushTool(event);
      if (parentKey && byKey.get(parentKey)?.kind === 'assistant') {
        step.parentKey = parentKey;
        const parent = byKey.get(parentKey);
        parent?.toolKeys?.push(step.key);
        if (parent && !parent.sourceEvents.some((source) => source.id === event.id)) {
          parent.sourceEvents.push(event);
        }
      }
      continue;
    }

    if (event.event_type === 'tool_result') {
      const callId = asText(payload.tool_use_id);
      if (callId && toolStepByKey.has(toolStepKey(event, callId))) continue; // joined to its call
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

    if (event.event_type === 'tool_output') {
      // The call indexes all its fragments before rendering; an orphan stays
      // available as a runtime record instead of inventing a completed result.
      const id = asText(payload.tool_use_id);
      if (callCounts.has(`${scopeOf(event)}:${id}`)) continue;
    }

    const row = toTrajectory([event])[0];
    const harness = harnessOf(payload);
    const notification = rawNotification(event);
    const lifecycleFailure =
      event.event_type === 'lifecycle' &&
      (row.isError ||
        Boolean(payload.error) ||
        payload.status === 'failed' ||
        payload.status === 'error' ||
        payload.phase === 'failed');
    const runtimeNotification =
      (notification !== null && isRuntimeNotification(notification)) ||
      isRuntimeTelemetry(event) ||
      event.event_type === 'tool_output';
    const diagnostic =
      runtimeNotification ||
      event.event_type === 'usage' ||
      (event.event_type === 'lifecycle' && !lifecycleFailure);
    const key = runtimeNotification
      ? `runtime:${scopeOf(event)}`
      : `event:${jobIdOf(event)}:${event.attempt_id}:${event.id}`;
    if (runtimeNotification) {
      const existing = byKey.get(key);
      if (existing) {
        existing.sourceEvents.push(event);
        existing.truncated ||= isTruncatedPayload(payload);
        existing.detail = `${existing.sourceEvents.length} runtime records`;
        continue;
      }
    }
    const params = asRecord(notification?.params);
    const protocolError = notification !== null && notificationHasError(notification);
    const step: TrajectoryStep = {
      key,
      kind: 'event',
      jobId: jobIdOf(event),
      turnNo: turnNoOf(event),
      attemptId: event.attempt_id,
      stepNo: 0,
      anchorEventId: event.id,
      sourceEvents: [event],
      lane: row.lane,
      actor: row.actor,
      label: runtimeNotification
        ? 'Runtime records'
        : notification
          ? asText(notification.method)
          : row.label,
      detail: runtimeNotification
        ? '1 runtime record'
        : notification
          ? preview(params.summary ?? params.message ?? params.error ?? params)
          : row.detail,
      at: event.created_at ?? null,
      isError: row.isError || protocolError || lifecycleFailure,
      status:
        event.event_type === 'error' || protocolError || lifecycleFailure ? 'failed' : 'recorded',
      truncated: isTruncatedPayload(payload),
      timing: { source: null, startedAt: null, finishedAt: null, durationMs: null },
      ...(harness ? { harness } : {}),
      ...(diagnostic ? { diagnostic: true } : {}),
    };
    byKey.set(key, step);
    steps.push(step);
  }

  for (const input of inputs.values()) pushUser(input.jobId, input.turnNo, input.text, input.at);
  // Link only explicit runtime delegation owners after all calls are indexed,
  // so backfilled parents and concurrent siblings keep their real identities.
  for (const step of steps) {
    // Runtime records are grouped per attempt across every agent, so one
    // subagent record cannot make the whole group its child.
    if (step.diagnostic) continue;
    const source = step.sourceEvents.find((event) => {
      const parent = asRecord(event.payload).parent_tool_use_id;
      return typeof parent === 'string' && parent.length > 0;
    });
    if (!source) continue;
    const callId = asText(asRecord(source.payload).parent_tool_use_id);
    const parent = toolStepByKey.get(toolStepKey(source, callId));
    if (!parent || parent === step || parent.ambiguity || step.ambiguity) continue;
    step.delegationParentKey = parent.key;
    (parent.delegatedKeys ??= []).push(step.key);
    if (step.kind === 'assistant') step.label = 'Subagent message';
  }
  // A queued turn may have an input but no stored event yet.
  steps.sort((left, right) => left.turnNo - right.turnNo);

  // Usage is metadata, never an injected Context message. Attach it only
  // when the runtime recorded a matching model-message owner; unowned/turn
  // totals stay runtime bookkeeping and cannot create a new Assistant.
  for (let index = steps.length - 1; index >= 0; index -= 1) {
    const event = steps[index].sourceEvents[0];
    if (event?.event_type !== 'usage') continue;
    const payload = asRecord(event.payload);
    const usageId = usageIdOf(payload);
    if (!usageId || payload.usage_scope === 'attempt') continue;
    const owner = byKey.get(assistantKeyOf(event, usageId));
    if (owner?.kind !== 'assistant' || owner.nativeMessageId !== usageId) continue;
    owner.sourceEvents.push(event);
    steps.splice(index, 1);
  }

  // Usage that a native message recorded, de-duplicated by message identity:
  // the same response repeats its counters on every block it emitted.
  for (const step of steps) {
    step.sourceEvents = step.sourceEvents.map((event) => originals.get(event.id) ?? event);
    if (step.diagnostic) continue;
    if (step.kind !== 'assistant') continue;
    if (step.streaming && !context.isAttemptActive(step.jobId, step.attemptId)) {
      step.streaming = false;
    }
    if (
      !step.streaming &&
      !step.blocks?.some((block) => block.text.trim()) &&
      !step.toolKeys?.length
    ) {
      step.diagnostic = true;
      step.label = 'Empty assistant record';
    }
    step.usage = usageOf(step.sourceEvents);
  }

  // Display numbers are a property of the whole turn, so they do not shift
  // when the reader searches; selection never uses them.
  const perTurn = new Map<string, number>();
  for (const step of steps) {
    if (step.diagnostic) continue;
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
  const snapshots = events
    .filter((event) => asRecord(event.payload).usage_scope !== 'attempt')
    .sort((left, right) => right.seq - left.seq)
    .map((event) => {
      const payload = asRecord(event.payload);
      // pi reports a response's counters as a usage event whose payload is
      // the usage itself; the other runtimes nest it under `usage`.
      return event.event_type === 'usage' && payload.usage === undefined
        ? payload
        : asRecord(payload.usage);
    });
  const latestCount = (keys: string[]): number | null =>
    snapshots.map((usage) => firstCount(usage, keys)).find((count) => count !== null) ?? null;
  return {
    outputTokens: latestCount(OUTPUT_KEYS),
    inputTokens: latestCount(INPUT_KEYS),
    reasoningTokens: latestCount(REASONING_KEYS),
    contentTokens: latestCount(CONTENT_KEYS),
    cacheReadTokens: latestCount(CACHE_READ_KEYS),
    cacheWriteTokens: latestCount(CACHE_WRITE_KEYS),
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
  return steps.filter((step) => stepSearchText(step).includes(needle));
}

export function stepSearchText(step: TrajectoryStep): string {
  return `${step.label} ${step.detail} ${step.kind} ${step.result?.text ?? step.outputPreview ?? ''}`.toLowerCase();
}

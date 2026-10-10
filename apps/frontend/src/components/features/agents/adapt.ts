// Adapts the agent-jobs API shape to what the UI renders.
//
// The UI was designed against a richer mock than the API currently returns.
// This module makes that gap explicit rather than papering over it: fields the
// backend genuinely provides are mapped, and fields it does not are left empty
// so the UI renders an honest blank instead of an invented value.

import type {
  AgentJobApi,
  AgentJobEventApi,
  AgentAttemptTelemetryApi,
  AgentAttemptSummaryApi,
  AttemptResourcesApi,
  AgentThreadApi,
  AgentThreadEventApi,
  AgentThreadMessageApi,
  ToolCallResourcesApi,
  ProcessObservationApi,
} from '@/lib/api/agents';

import type {
  AgentDiffFile,
  AgentEvent,
  AgentHistoryTurn,
  AgentAttempt,
  AgentGoal,
  AgentJob,
  AgentJobState,
  DiffLine,
  GoalTurn,
  ToolCallResources,
  ProcessObservation,
  TurnResources,
} from './types';
import { projectProtocolEvents, replyPreviewPiece } from './trajectoryProtocol';
import { agentLiveNote } from './agentLiveStatus';

/**
 * Preserve a server result as a per-turn display state.
 *
 * Historical turns and their actions keep the distinction between success,
 * failure and cancellation. Conversation badges use `toConversationState`
 * to present successful and stopped turns as idle regardless of publication.
 */
export function toDisplayState(job: AgentJobApi): AgentJobState {
  if (job.state === 'queued') return 'queued';
  if (job.state === 'running' || job.state === 'publishing') return 'running';
  // Retain the legacy result values for historical turn consumers.
  if (job.state === 'succeeded') return job.published_pr_url ? 'done' : 'needs_review';
  if (job.state === 'failed') return 'failed';
  if (job.state === 'cancelled') return 'cancelled';
  return 'queued';
}

function asText(payload: Record<string, unknown> | null, ...keys: string[]): string {
  for (const key of keys) {
    const value = payload?.[key];
    if (typeof value === 'string' && value) return value;
  }
  return '';
}

function modelRetryMessage(payload: Record<string, unknown>): string {
  const delay = payload.retry_delay_ms;
  let message = 'Retrying model request';
  if (typeof delay === 'number' && Number.isFinite(delay) && delay >= 0) {
    message = delay === 0 ? 'Retrying now' : `Retrying in ${Math.ceil(delay / 1000)}s`;
  }
  const attempt = payload.attempt;
  if (typeof attempt === 'number' && Number.isInteger(attempt) && attempt > 0) {
    const maximum = payload.max_retries;
    message +=
      typeof maximum === 'number' && Number.isInteger(maximum) && maximum >= attempt
        ? ` (retry ${attempt} of ${maximum})`
        : ` (retry ${attempt})`;
  }
  const prefix = payload.error_status === 503 ? 'Provider temporarily unavailable. ' : '';
  return `${prefix}${message}.`;
}

function readableValue(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value === null || value === undefined) return '';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function commandText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') {
    const input = value as Record<string, unknown>;
    const command = input.command ?? input.cmd;
    if (typeof command === 'string') return command;
    if (Array.isArray(command) && command.every((part) => typeof part === 'string')) {
      return command.join(' ');
    }
  }
  return readableValue(value);
}

function toolOutput(payload: Record<string, unknown>): string[] | undefined {
  const output =
    readableValue(payload.output) ||
    readableValue(payload.aggregated_output) ||
    readableValue(payload.stdout);
  return output ? output.split('\n') : undefined;
}

/** Map one stored event onto a UI row, or null when it has nothing to show. */
/**
 * The span between a tool call and its result, when both carry a time.
 *
 * Message-level by construction: the window opens when the model asked for
 * the call rather than when the sandbox began running it, so it includes
 * whatever thinking preceded execution. Returns an empty object rather than
 * a null field so a row without usable times carries no duration at all.
 */
export function durationBetween(
  startedAt: string | null | undefined,
  endedAt: string | null | undefined,
): { durationMs?: number } {
  if (!startedAt || !endedAt) return {};
  const start = Date.parse(startedAt);
  const end = Date.parse(endedAt);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return {};
  return { durationMs: end - start };
}

export function toDisplayEvent(event: AgentJobEventApi): AgentEvent | null {
  const payload = event.payload ?? {};
  const type = event.event_type;
  // Every mapped row carries the stored id, including the ones that borrow
  // their text from a payload: it is the only identity that survives folding,
  // and the Chat reading anchor is restored by row.
  const sourceEventId = event.id;

  // Never carry chain-of-thought into the display model. The UI represents
  // this as a compact reasoning status, not as model-authored hidden text.
  if (type === 'thinking')
    return asText(payload, 'parent_tool_use_id')
      ? null
      : { kind: 'thinking', text: '', sourceEventId };
  if (type === 'message') {
    // A delegated agent's answer belongs to its Task/Agent result, and is
    // separately inspectable in Trajectory. It is not the main reply.
    if (asText(payload, 'parent_tool_use_id')) return null;
    const previewId = asText(payload, 'preview_id');
    return {
      kind: 'message',
      text: asText(payload, 'text'),
      sourceEventId,
      ...(previewId ? { previewId } : {}),
    };
  }
  if (type === 'usage') return { kind: 'usage', text: asText(payload, 'text'), sourceEventId };
  // A message the owner sent into the running turn reads where it arrived,
  // between what the agent did before it and what it did after.
  if (type === 'user_message') {
    return { kind: 'user_message', text: asText(payload, 'text'), sourceEventId };
  }

  if (type === 'tool_use') {
    const name = asText(payload, 'name', 'tool') || 'Bash';
    const exitCode = payload.exit_code;
    const output = toolOutput(payload);
    const outputIsError =
      (typeof exitCode === 'number' && exitCode !== 0) ||
      payload.status === 'failed' ||
      payload.is_error === true ||
      payload.native_tool_status === 'failed';
    return {
      kind: 'tool_use',
      tool: name,
      id: asText(payload, 'id') || undefined,
      ...(Object.hasOwn(payload, 'input') ? { input: payload.input } : {}),
      ...(asText(payload, 'parent_tool_use_id')
        ? { parentToolUseId: asText(payload, 'parent_tool_use_id') }
        : {}),
      ...(payload.native_tool_status === 'completed' ? { resultState: 'received' as const } : {}),
      ...(payload.native_tool_status === 'cancelled' ? { resultState: 'cancelled' as const } : {}),
      ...(event.created_at ? { at: event.created_at } : {}),
      detail: asText(payload, 'detail', 'text') || commandText(payload.input) || 'Running tool',
      ...(output ? { output } : {}),
      ...(outputIsError ? { outputIsError: true } : {}),
      sourceEventId,
    };
  }

  if (type === 'tool_result') {
    return {
      kind: 'tool_result',
      text:
        readableValue(payload.content) ||
        readableValue(payload.output) ||
        readableValue(payload.aggregated_output) ||
        asText(payload, 'text', 'detail'),
      toolUseId: asText(payload, 'tool_use_id') || undefined,
      ...(asText(payload, 'parent_tool_use_id')
        ? { parentToolUseId: asText(payload, 'parent_tool_use_id') }
        : {}),
      ...(payload.native_tool_status === 'cancelled' ? { cancelled: true } : {}),
      isError: payload.is_error === true,
      ...(event.created_at ? { at: event.created_at } : {}),
      sourceEventId,
    };
  }

  if (type === 'error') {
    // Older runs recorded a failed connection with only the host it named.
    // Say what happened without claiming a policy blocked it: the network is
    // open to the public internet, so a failure there is usually the target's.
    const host = asText(payload, 'host');
    if (host && !asText(payload, 'detail', 'text')) {
      return { kind: 'lifecycle', text: `Could not reach ${host}`, sourceEventId };
    }
    return {
      kind: 'lifecycle',
      text: ownerFailureMessage(asText(payload, 'detail', 'text') || 'An error occurred'),
      sourceEventId,
    };
  }

  if (type === 'attempt_superseded') {
    return { kind: 'lifecycle', text: 'attempt_superseded', sourceEventId };
  }
  if (type === 'lifecycle') {
    const phase = asText(payload, 'phase', 'text') || 'lifecycle';
    if (phase === 'api_retry') {
      return { kind: 'lifecycle', text: modelRetryMessage(payload), sourceEventId };
    }
    // The runner reports whether it reused the tree; the milestone reads
    // differently depending on the answer, and only the runner can know it.
    if (phase === 'checked_out' && payload.reused === true) {
      return { kind: 'lifecycle', text: 'workspace_reused', sourceEventId };
    }
    if (phase === 'rate_limit') {
      const info = payload.info;
      const status =
        info && typeof info === 'object' ? (info as Record<string, unknown>).status : null;
      if (status === 'rejected') {
        return { kind: 'lifecycle', text: 'Model usage limit reached', sourceEventId };
      }
      if (status === 'allowed_warning') {
        return { kind: 'lifecycle', text: 'Approaching model usage limit', sourceEventId };
      }
      return null;
    }
    if (phase === 'shared_tool_errors_observed') {
      return { kind: 'lifecycle', text: 'Some tools encountered errors', sourceEventId };
    }
    return Object.hasOwn(LIFECYCLE_LABELS, phase)
      ? { kind: 'lifecycle', text: phase, sourceEventId }
      : null;
  }
  if (type === 'raw') {
    // An answer while it is being written: shown, then replaced by the
    // complete message (see `displayEvents`).
    const piece = replyPreviewPiece(event);
    if (piece) {
      return {
        kind: 'message',
        text: piece.text,
        sourceEventId,
        previewId: piece.previewId,
        streaming: true,
      };
    }
    // Other raw records are unclassified runtime protocol data. They remain
    // available through the events API, but are not terminal commands or output.
    return null;
  }

  // Diff is rendered from the retained workspace rather than a stream marker
  // that only says whether files changed.
  if (type === 'diff') return null;

  return null;
}

/** Extract the file list a patch touches, for the diff header. */
export function toDiffFiles(patch: string): string[] {
  const files = new Set<string>();
  for (const line of patch.split('\n')) {
    const match = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (match) files.add(match[2]);
  }
  return [...files];
}

/** Split a patch into independently selectable files with per-file stats. */
export function toDiffFileDetails(patch: string): AgentDiffFile[] {
  if (!patch) return [];
  const files: AgentDiffFile[] = [];
  let current: AgentDiffFile | null = null;

  for (const line of patch.split('\n')) {
    const header = /^diff --git a\/(.+?) b\/(.+)$/.exec(line);
    if (header) {
      current = { path: header[2], add: 0, del: 0, lines: [] };
      files.push(current);
      continue;
    }
    if (!current || line.startsWith('index ')) continue;

    let marker: DiffLine['marker'] = 'ctx';
    if (line.startsWith('@@')) marker = 'hunk';
    else if (line.startsWith('+') && !line.startsWith('+++')) {
      marker = 'add';
      current.add += 1;
    } else if (line.startsWith('-') && !line.startsWith('---')) {
      marker = 'del';
      current.del += 1;
    }
    current.lines.push({ marker, text: line });
  }
  return files;
}

function diffStat(patch: string): { add: number; del: number } | undefined {
  if (!patch) return undefined;
  let add = 0;
  let del = 0;
  for (const line of patch.split('\n')) {
    if (line.startsWith('+') && !line.startsWith('+++')) add += 1;
    else if (line.startsWith('-') && !line.startsWith('---')) del += 1;
  }
  return { add, del };
}

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

/** Render a token count compactly, or empty when there is no ledger. */
function formatTokens(value: number | null): string {
  if (value === null || value === undefined) return '';
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

export interface AdaptOptions {
  events?: AgentJobEventApi[];
  threadEvents?: AgentThreadEventApi[];
  patch?: string | null;
  thread?: AgentThreadApi | null;
}

const LIFECYCLE_LABELS: Record<string, string> = {
  // Three phases used to share one label, so the platform starting a turn and
  // the agent announcing itself rendered as the same ticked step, twice. They
  // are different milestones and now say so — the back-to-back collapse below
  // only ever hid the pair when nothing came between them, which on a resident
  // turn it does.
  started: 'Run started',
  init: 'Agent ready',
  system: 'Agent ready',
  checked_out: 'Repository ready',
  // A resident turn skipped the clone: the tree is the one this conversation
  // has been building. Saying "repository ready" either way told the owner
  // their thread had been rebuilt when it had not.
  workspace_reused: 'Reusing this conversation\u2019s workspace',
  context_restored: 'Previous work restored',
  setup: 'Installing project dependencies',
  workspace_preparing: 'Preparing workspace',
  workspace_ready: 'Workspace ready',
  workspace_finalizing: 'Saving workspace changes',
  compact_boundary: 'Context compacted to keep going',
  result: 'Agent finished',
  publishing: 'Opening draft pull request',
  published: 'Draft pull request ready',
  shared_snapshot_publishing: 'Opening draft pull request',
  shared_snapshot_published: 'Draft pull request ready',
  cancelled_by_owner: 'Stopped by you',
};

/**
 * Render one lifecycle phase, and say whether it is a milestone.
 *
 * `milestone: false` is the important half. A phase this build has never heard
 * of used to be title-cased and given a ✓ like a completed step, so a progress
 * counter the CLI happened to emit (`thinking_tokens`) read as 408 finished
 * milestones. An unrecognised phase is a diagnostic: shown, because dropping it
 * hides a real change in what the runtime reports, but never dressed up as
 * something the job accomplished.
 */
export function lifecyclePhaseLabel(value: string): { text: string; milestone: boolean } {
  const phase = value.trim().toLowerCase();
  const known = LIFECYCLE_LABELS[phase];
  if (known) return { text: known, milestone: true };
  if (phase.includes('superseded')) {
    return { text: 'Run interrupted', milestone: true };
  }
  return { text: value.replaceAll('_', ' '), milestone: false };
}

/** Infrastructure detail shapes the control plane emits for recoverable failures.
 * Listed individually so adding a new backend message is an explicit edit here
 * rather than a silent leak of infrastructure jargon into the owner-facing UI.
 */
const INFRASTRUCTURE_FAILURE_PATTERNS = [
  /lease[ _](?:expired|lost)/i,
  /lease[^.\n]*expir/i,
  /activation lease/i,
  /indeterminate.*(?:turn|admission)/i,
  /turn.*indeterminate/i,
  /context[ _](?:lost|marked lost)/i,
  /native(?: Agent CLI)?[- _](?:context|session|state)/i,
  /finalization expired/i,
  /Session Supervisor/i,
  /resident transport/i,
  /attempt dispatch failed/i,
  /retained (?:sandbox|Agent CLI|chat|session)/i,
  /shared (?:live|chat|supervisor|Agent CLI|member)/i,
];

/** The runner measured the checkout against its server's import limit and
 * refused it before the agent started. Every attempt measures the same
 * repository, so a follow-up fails the same way. Hosts older than the size
 * report name neither the size nor the limit.
 */
const REPOSITORY_TOO_LARGE =
  /source (?:checkout|archive)(?: \((\S+ \S+) including its Git objects\))? exceeds (?:this host's (\S+ \S+)|the) retained import size limit/i;

function isRepositoryTooLarge(detail: string | null | undefined): boolean {
  return Boolean(detail && REPOSITORY_TOO_LARGE.test(detail));
}

/** The runner refused a turn its site's own setup made impossible, such as
 * connected tools on a server with no MCP relay. Its message always starts
 * this way and ends by asking for an admin, the only person who can clear
 * the cause, so a follow-up fails the same way until one does.
 */
const SITE_MISCONFIGURED = /^Site misconfigured:\s*/;

function failureCauseOf(detail: string | null | undefined): AgentJob['failureCause'] {
  if (isRepositoryTooLarge(detail)) return 'repository_too_large';
  if (detail && SITE_MISCONFIGURED.test(detail)) return 'site_misconfigured';
  return undefined;
}

/** A site-misconfigured failure's reason, for a surface whose title already
 * says the site is misconfigured. */
export function siteMisconfiguredReason(detail: string): string {
  const reason = detail.replace(SITE_MISCONFIGURED, '');
  return reason.charAt(0).toUpperCase() + reason.slice(1);
}

/** Translate infrastructure failures only; keep task errors and transcripts intact.
 * The original detail remains in the API and operator logs for diagnosis.
 */
export function ownerFailureMessage(detail: string): string {
  // Written for the owner already, and naming what only an admin can fix; it
  // must never fold into the generic connection message below.
  if (SITE_MISCONFIGURED.test(detail)) return detail;
  const tooLarge = REPOSITORY_TOO_LARGE.exec(detail);
  if (tooLarge) {
    const [, size, limit] = tooLarge;
    const sizes = [size, limit && `the limit is ${limit}`].filter(Boolean).join('; ');
    return `This repository is larger than this server allows${sizes ? ` (${sizes})` : ''}.`;
  }
  if (/^shared Agent CLI turn exceeded its execution timeout/i.test(detail)) {
    return 'This response took too long and was stopped.';
  }
  if (/^cancelled (?:after|by owner)/i.test(detail)) {
    return 'This response was stopped at your request.';
  }
  if (/finalization expired/i.test(detail)) {
    return 'The response finished, but its completion could not be confirmed. Review the response before continuing.';
  }
  if (INFRASTRUCTURE_FAILURE_PATTERNS.some((pattern) => pattern.test(detail))) {
    return 'The connection was interrupted before this response could be confirmed.';
  }
  return detail;
}

/** Preserve attempt boundaries and attach each result to its tool activity. */
function displayEvents(events: AgentJobEventApi[], job?: AgentJobApi): AgentEvent[] {
  const attemptIds = [...new Set(events.map((event) => event.attempt_id))].sort((a, b) => a - b);
  const attemptNoById = new Map(attemptIds.map((id, index) => [id, index + 1]));
  const rows: AgentEvent[] = [];
  const toolsById = new Map<string, number>();
  const lastToolByAttempt = new Map<number, number>();

  const activityEvents = new Map(
    projectProtocolEvents(events)
      .filter((event) => event.event_type === 'tool_use' || event.event_type === 'tool_result')
      .map((event) => [event.id, event]),
  );
  const callCounts = new Map<string, number>();
  const resultCounts = new Map<string, number>();
  for (const event of activityEvents.values()) {
    const id = asText(event.payload, event.event_type === 'tool_use' ? 'id' : 'tool_use_id');
    if (!id) continue;
    const key = `${attemptNoById.get(event.attempt_id)}:${id}`;
    const counts = event.event_type === 'tool_use' ? callCounts : resultCounts;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  for (const storedEvent of events) {
    // Chat has its own answer-preview folding. Read native tool activity here
    // without projecting message fragments a second time.
    const event = activityEvents.get(storedEvent.id) ?? storedEvent;
    const mapped = toDisplayEvent(event);
    if (!mapped) continue;
    const attemptNo = attemptNoById.get(event.attempt_id);
    const row = { ...mapped, attemptNo } as AgentEvent;

    if (row.kind === 'tool_use') {
      if (
        event.payload?.native_tool_request === true &&
        row.id &&
        callCounts.get(`${attemptNo}:${row.id}`) === 1
      ) {
        row.activityKey = `${event.attempt_id}:${row.id}`;
      }
      if (
        row.id &&
        ((callCounts.get(`${attemptNo}:${row.id}`) ?? 0) > 1 ||
          (resultCounts.get(`${attemptNo}:${row.id}`) ?? 0) > 1)
      )
        row.resultState = 'unknown';
      row.resultState ??= row.outputIsError
        ? 'failed'
        : row.output !== undefined
          ? 'received'
          : job &&
              (job.state === 'running' || job.state === 'publishing') &&
              job.current_attempt_id === event.attempt_id
            ? 'waiting'
            : 'not_recorded';
      const rowIndex = rows.push(row) - 1;
      if (row.id) toolsById.set(`${attemptNo}:${row.id}`, rowIndex);
      if (attemptNo !== undefined) lastToolByAttempt.set(attemptNo, rowIndex);
      continue;
    }

    if (row.kind === 'tool_result') {
      const explicit = row.toolUseId ? toolsById.get(`${attemptNo}:${row.toolUseId}`) : undefined;
      const targetIndex = row.toolUseId
        ? explicit
        : attemptNo === undefined
          ? undefined
          : lastToolByAttempt.get(attemptNo);
      const target = targetIndex === undefined ? undefined : rows[targetIndex];
      if (
        targetIndex !== undefined &&
        target?.kind === 'tool_use' &&
        !target.output &&
        target.resultState !== 'unknown' &&
        (!row.parentToolUseId || row.parentToolUseId === target.parentToolUseId)
      ) {
        rows[targetIndex] = {
          ...target,
          output: row.text ? row.text.split('\n') : ['Command completed with no output.'],
          outputIsError: row.isError,
          resultState: row.cancelled ? 'cancelled' : row.isError ? 'failed' : 'received',
          // Folding is where the pair still exists. Downstream only sees the
          // surviving tool_use row, so a consumer that wanted to time the
          // call — the turn footer does — would find nothing left to pair
          // with and render every call as unfinished. The window is resolved
          // here, while both ends are in hand.
          ...durationBetween(target.at, row.at),
        };
        continue;
      }
    }

    if (row.kind === 'message' && row.previewId) {
      // Pieces of an answer grow one row, and the complete message takes that
      // row's place, between whatever came before and after it.
      let previewIndex = rows.length - 1;
      for (; previewIndex >= 0; previewIndex -= 1) {
        const item = rows[previewIndex];
        if (
          item.kind === 'message' &&
          item.streaming === true &&
          item.previewId === row.previewId &&
          item.attemptNo === row.attemptNo
        ) {
          break;
        }
      }
      const preview = rows[previewIndex];
      if (preview?.kind === 'message') {
        // The first piece's id stays the row's identity, live and on reload,
        // so completing the answer never moves the reading anchor.
        rows[previewIndex] = !row.streaming
          ? { ...row, sourceEventId: preview.sourceEventId }
          : replyPreviewPiece(event)?.reset
            ? { ...preview, text: row.text }
            : { ...preview, text: preview.text + row.text };
        continue;
      }
    }

    // Reasoning is a state, not a sequence of steps. A run emits one of these
    // per thinking block, and rendering each as its own row turns "the agent
    // thought about it" into a wall of identical lines. Consecutive ones
    // collapse into the single status row that was always intended.
    if (row.kind === 'thinking' && rows[rows.length - 1]?.kind === 'thinking') continue;

    // Same for a milestone repeated back to back: the platform's "started" and
    // the CLI's "init" both mean "setting up environment", so they rendered as
    // two identical ticked-off steps.
    const previous = rows[rows.length - 1];
    if (
      row.kind === 'lifecycle' &&
      previous?.kind === 'lifecycle' &&
      lifecyclePhaseLabel(previous.text).text === lifecyclePhaseLabel(row.text).text
    ) {
      continue;
    }

    rows.push(row);
  }

  return rows;
}

/** Rename the host's rollup into the UI's shape, keeping every null a null. */
export function toTurnResources(api: AttemptResourcesApi): TurnResources {
  const toolCallsTruncated = api.tool_calls_truncated === true;
  const toolCalls = (api.tool_calls ?? []).map((call) => {
    const adapted = toToolCallResources(call);
    if (!toolCallsTruncated) return adapted;
    // A dropped call may have started processes the retained rows now
    // claim; nothing per-call survives a truncated list, exit-accounted or
    // sampled alike.
    return {
      ...adapted,
      sampleCount: 0,
      measuredSpanMs: null,
      cpuUsec: null,
      memPeakBytes: null,
      exclusive: false,
      processCpuUsec: null,
      processMemPeakBytes: null,
      processIoReadBytes: null,
      processIoWriteBytes: null,
      processResourcesPartial: false,
    };
  });
  const processObserver = api.process_observer ?? null;
  const processEventsLost = api.process_events_lost ?? 0;
  const processesTruncated = api.processes_truncated === true;
  const processObservationComplete =
    processObserver !== null &&
    api.process_observation_complete === true &&
    processEventsLost === 0 &&
    !processesTruncated;
  // An incomplete census may have lost a process a call started, so no
  // call's exit-accounted numbers can be presented as its total. The host
  // marks this too; the reader is kept conservative even against an older
  // or wrong row.
  const conservativeCalls = toolCalls.map((call) => {
    if (processObservationComplete || (call.processCount ?? 0) === 0) return call;
    return { ...call, processResourcesPartial: true };
  });
  return {
    cpuUsec: api.cpu_usec,
    memPeakBytes: api.mem_peak_bytes,
    memAvgBytes: api.mem_avg_bytes,
    ioReadBytes: api.io_read_bytes,
    ioWriteBytes: api.io_write_bytes,
    netRxBytes: api.net_rx_bytes,
    netTxBytes: api.net_tx_bytes,
    sampleCount: api.sample_count,
    samplerVersion: api.sampler_version,
    coverage: api.coverage,
    toolTimeMs: toolCallsTruncated ? null : (api.tool_time_ms ?? null),
    toolCalls: conservativeCalls,
    toolCallsTruncated,
    processObserver,
    processObservationComplete,
    processEventsLost,
    processesTruncated,
    processes: (api.processes ?? []).map(toProcessObservation),
  };
}

/**
 * Collapse the exclusive/shared column pair into one number and a flag.
 *
 * The pair exists in the database so a shared measurement cannot be read as
 * an exclusive one by a query that does not know the difference. Here the
 * difference is carried by `exclusive`, and every renderer of these numbers
 * has to answer it — a value with no flag beside it is what this shape makes
 * impossible.
 */
function toToolCallResources(api: ToolCallResourcesApi): ToolCallResources {
  const exclusive = api.cpu_usec_exclusive !== null || api.mem_peak_exclusive_bytes !== null;
  return {
    toolUseId: api.tool_use_id,
    toolName: api.tool_name,
    timingSource: api.timing_source === 'exact' ? 'exact' : 'message',
    durationMs: api.duration_ms,
    sampleCount: api.sample_count,
    overlapN: api.overlap_n,
    measuredSpanMs: api.measured_span_ms,
    cpuUsec: exclusive ? api.cpu_usec_exclusive : api.cpu_usec_shared,
    memPeakBytes: exclusive ? api.mem_peak_exclusive_bytes : api.mem_peak_shared_bytes,
    exclusive,
    processCount: api.process_count ?? 0,
    processCpuUsec: api.process_cpu_usec ?? null,
    processMemPeakBytes: api.process_mem_peak_bytes ?? null,
    processIoReadBytes: api.process_io_read_bytes ?? null,
    processIoWriteBytes: api.process_io_write_bytes ?? null,
    processResourcesPartial: api.process_resources_partial === true,
  };
}

function toProcessObservation(api: ProcessObservationApi): ProcessObservation {
  return {
    processSeq: api.process_seq,
    uid: api.uid,
    pid: api.pid,
    tgid: api.tgid,
    ppid: api.ppid,
    comm: api.comm,
    executable: api.executable,
    startedOffsetMs: api.started_offset_ms,
    durationMs: api.duration_ms,
    exitCode: api.exit_code,
    observedFork: api.observed_fork,
    observedExec: api.observed_exec,
    observedExit: api.observed_exit,
    toolCallSeqs: api.tool_call_seqs,
    cpuUsec: api.cpu_usec ?? null,
    memPeakBytes: api.mem_peak_bytes ?? null,
    ioReadBytes: api.io_read_bytes ?? null,
    ioWriteBytes: api.io_write_bytes ?? null,
    ioRcharBytes: api.io_rchar_bytes ?? null,
    ioWcharBytes: api.io_wchar_bytes ?? null,
  };
}

export function toAttemptTelemetry(
  api: AgentAttemptSummaryApi & Partial<Pick<AgentAttemptTelemetryApi, 'resources'>>,
): NonNullable<AgentAttempt['telemetry']> {
  return {
    ...(api.continuity_mode ? { continuityMode: api.continuity_mode } : {}),
    ...(api.context_status ? { contextStatus: api.context_status } : {}),
    ...(typeof api.duration_ms === 'number' ? { durationMs: api.duration_ms } : {}),
    ...(api.resources ? { resources: toTurnResources(api.resources) } : {}),
  };
}

/** How one stored turn relates to the conversation's goal, if it does at all.
 *
 * Only an automatic round has one: the platform reads a report out of a turn it
 * queued itself, and a person's turn is not part of the completion protocol.
 * Every other turn returns nothing, which is what keeps a goal from appearing
 * on every turn of every chat the moment the feature ships.
 */
function storedGoalTurn(job: AgentJobApi): GoalTurn | undefined {
  if (job.turn_source !== 'goal') return undefined;
  return {
    source: 'goal',
    ...(typeof job.goal_round === 'number' ? { round: job.goal_round } : {}),
    ...(job.goal_round_kind ? { kind: job.goal_round_kind } : {}),
    ...(job.goal_report_parse ? { reportParse: job.goal_report_parse } : {}),
    ...(job.goal_report_outcome ? { reportOutcome: job.goal_report_outcome } : {}),
    ...(job.goal_report_detail ? { reportDetail: job.goal_report_detail } : {}),
  };
}

/** The conversation's goal as the band reads it. */
function toDisplayGoal(goal: NonNullable<AgentJobApi['goal']>): AgentGoal {
  return {
    objective: goal.objective,
    state: goal.state,
    revision: goal.revision,
    roundsUsed: goal.rounds_used,
    maxRounds: goal.max_rounds,
    ...(goal.pause_reason ? { pauseReason: goal.pause_reason } : {}),
    ...(goal.pause_detail ? { pauseDetail: goal.pause_detail } : {}),
    ...(goal.completion_basis ? { completionBasis: goal.completion_basis } : {}),
  };
}

export function toDisplayJob(job: AgentJobApi, options: AdaptOptions = {}): AgentJob {
  const failureCause = failureCauseOf(job.detail);
  const events = options.events ?? [];
  const patch = options.patch ?? '';
  const renderedEvents = displayEvents(events, job);

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

  const currentTurn =
    job.turn_no ?? options.thread?.jobs.find((threadJob) => threadJob.id === job.id)?.turn_no;
  let priorJobIds: Set<string> | null = null;
  if (currentTurn !== undefined && options.thread?.jobs.length) {
    priorJobIds = new Set(
      options.thread.jobs
        .filter((threadJob) => (threadJob.turn_no ?? 1) < currentTurn)
        .map((threadJob) => threadJob.id),
    );
  }
  const mapMessage = (message: AgentThreadMessageApi) => ({
    id: message.id,
    role: message.role,
    content: message.content,
    jobId: message.job_id,
    createdAt: message.created_at,
    ...(message.mid_turn ? { midTurn: true } : {}),
  });
  const priorMessages = (options.thread?.messages ?? [])
    .filter((message) =>
      priorJobIds ? priorJobIds.has(message.job_id) : message.job_id !== job.id,
    )
    .map(mapMessage);
  const historicalJobs = (options.thread?.jobs ?? []).filter(
    (threadJob) =>
      threadJob.id !== job.id &&
      (currentTurn === undefined || (threadJob.turn_no ?? 1) < currentTurn),
  );
  const threadEvents = options.threadEvents;
  let historyTurns: AgentHistoryTurn[] | undefined;
  if (threadEvents !== undefined) {
    historyTurns = historicalJobs.map((threadJob) => ({
      jobId: threadJob.id,
      turnNo: threadJob.turn_no ?? 1,
      // The stored turn's own outcome, so the rail can say a turn failed
      // without re-deriving it from the events it happens to carry.
      state: toDisplayState(threadJob),
      // This is stronger than platform success: the API has also proved a
      // settled native ordinal, compatible online host/image, and idle source
      // membership. Showing a button from state alone creates a predictable
      // 409 for failed rollout generations.
      forkable: threadJob.native_fork_available === true,
      // Each stored turn carries its own relationship to the goal: whether the
      // platform queued it, and what it reported. Absent for a conversation
      // without a goal, which is what keeps this from touching every turn.
      ...(storedGoalTurn(threadJob) ? { goalTurn: storedGoalTurn(threadJob) } : {}),
      messages: (options.thread?.messages ?? [])
        .filter((message) => message.job_id === threadJob.id)
        .map(mapMessage),
      events: displayEvents(
        threadEvents.filter((event) => event.job_id === threadJob.id),
        threadJob,
      ),
    }));
  }
  // A turn copied by fork has no events; its durable messages are the only
  // record of what it said. For a settled job whose event log carries no
  // message rows, render the thread's copy of the turn — prompt included, so
  // the turn keeps its chronology and the prompt card knows to stand down.
  const settled = job.state === 'succeeded' || job.state === 'failed' || job.state === 'cancelled';
  let ownMessages: ReturnType<typeof mapMessage>[] = [];
  if (settled && !events.some((event) => event.event_type === 'message')) {
    ownMessages = (options.thread?.messages ?? [])
      .filter((message) => message.job_id === job.id)
      .map(mapMessage);
  }
  const threadPrUrl = [...(options.thread?.jobs ?? [])]
    .reverse()
    .find((threadJob) => threadJob.published_pr_url)?.published_pr_url;
  const publishedPrUrl = job.published_pr_url ?? threadPrUrl ?? null;

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
    conversationState: job.conversation_state ?? undefined,
    stateNote: job.detail ? ownerFailureMessage(job.detail) : undefined,
    ...(failureCause ? { failureCause } : {}),
    repo: job.repo,
    baseRef: job.base_ref ?? undefined,
    baseSha: (job.base_sha ?? '').slice(0, 7),
    // Every turn in a conversation publishes to the same branch/PR.
    branch: job.output_branch ?? `agent/${job.thread_id ?? options.thread?.thread_id ?? job.id}`,
    runtime: job.runtime,
    model: job.model,
    reasoningEffort: job.reasoning_effort ?? undefined,
    skills: job.skills ?? [],
    // The conversation's goal, and this turn's relationship to it. Assigned
    // after the literal rather than spread inside it: a ternary returning an
    // object, spread into another object, is indented differently by Prettier
    // and ESLint and the two refuse to agree. Both fields are optional — a
    // conversation without a goal carries neither, and a deployment older than
    // this feature carries no fields at all.
    ...(job.goal ? { goal: toDisplayGoal(job.goal) } : {}),
    // Through the same helper the stored turns use, not a second copy of it:
    // written out inline, every job got a `goalTurn` — including the ones whose
    // conversation has no goal at all — and "absent" stopped meaning anything.
    ...(storedGoalTurn(job) ? { goalTurn: storedGoalTurn(job) } : {}),
    // Summed server-side from api_logs. Null there means no ledger is
    // configured; 0 is the honest display for that, but the panel below reads
    // `hasLedger` so it can say so rather than imply the job was free.
    spentUsd: job.spent_usd ?? 0,
    hasLedger: job.spent_usd !== null,
    ...(job.continuity_mode ? { continuityMode: job.continuity_mode } : {}),
    ...(job.context_status ? { contextStatus: job.context_status } : {}),
    ...(job.resident_state ? { residentState: job.resident_state } : {}),
    ...(job.sandbox_id ? { sandboxId: job.sandbox_id } : {}),
    ...(job.workspace_shared ? { workspaceShared: true } : {}),
    ...(job.native_state ? { nativeState: job.native_state } : {}),
    ...(job.native_fork_available ? { nativeForkAvailable: true } : {}),
    ...(job.workspace_publication_state
      ? { workspacePublicationState: job.workspace_publication_state }
      : {}),
    ...(typeof job.workspace_epoch === 'number' ? { workspaceEpoch: job.workspace_epoch } : {}),
    ...(typeof job.duration_ms === 'number' ? { durationMs: job.duration_ms } : {}),
    ...(job.resources ? { resources: toTurnResources(job.resources) } : {}),
    timeoutLabel: '',
    sandbox: sandboxLabel(events, job.current_attempt_id),
    attempts,
    attemptTelemetryAvailable: job.attempt_telemetry !== undefined,
    currentAttemptNo: currentAttemptIndex >= 0 ? currentAttemptIndex + 1 : undefined,
    telemetryAttemptNo: telemetryAttemptIndex >= 0 ? telemetryAttemptIndex + 1 : undefined,
    events: renderedEvents,
    liveNote: agentLiveNote(job, events),
    diffFiles: toDiffFiles(patch),
    workspaceDiffAvailable: typeof options.patch === 'string',
    diffFileDetails: toDiffFileDetails(patch),
    diffStat: diffStat(patch),
    gates: [],
    usage: {
      tokensIn: formatTokens(job.tokens_in),
      tokensOut: formatTokens(job.tokens_out),
      cachePct: 0,
      turns: job.model_calls,
    },
    prLabel: publishedPrUrl ? `draft PR ${publishedPrUrl.split('/').pop()}` : undefined,
    prUrl: publishedPrUrl ?? undefined,
    threadId: job.thread_id ?? options.thread?.thread_id ?? undefined,
    parentJobId: job.parent_job_id ?? undefined,
    turnNo: job.turn_no,
    threadMessages: [...priorMessages, ...ownMessages],
    ...(historyTurns ? { historyTurns } : {}),
    historyIncludesPrompt: ownMessages.some((message) => message.role === 'user'),
  };
}

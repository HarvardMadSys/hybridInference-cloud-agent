// Stored events to transcript rows. Job and thread event rows share this
// shape, so both adapters project them here.

import type { AgentJobApi, AgentJobEventApi } from '@/lib/api/agents';
import type { AgentEvent } from '../types';
import {
  notificationHasError,
  projectProtocolEvents,
  rawNotification,
  record,
  replyPreviewPiece,
} from '../trajectory/trajectoryProtocol';
import { ownerFailureMessage } from './jobState';
import { sandboxIncidentMessage } from './sandboxIncidentNotice';

export function asText(payload: Record<string, unknown> | null, ...keys: string[]): string {
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

/** Stands in for a paired result that carried no text; it is not the tool's output. */
export const EMPTY_TOOL_OUTPUT = 'Command completed with no output.';

function markedTruncated(payload: Record<string, unknown>): boolean {
  return payload.truncated === true || typeof payload.truncation_notice === 'string';
}

function toolOutput(payload: Record<string, unknown>): string[] | undefined {
  const output =
    readableValue(payload.output) ||
    readableValue(payload.aggregated_output) ||
    readableValue(payload.stdout);
  return output ? linesOf(payload, output) : undefined;
}

/**
 * `text` split into lines, kept with the stored object it was read from.
 *
 * A running turn projects its events again for each one it streams. Splitting
 * each output anew made new arrays every time, so a row that had not changed
 * could not be told apart from one that had without reading every line.
 */
const splitOutputs = new WeakMap<object, { text: string; lines: string[] }>();

function linesOf(source: object, text: string): string[] {
  const kept = splitOutputs.get(source);
  if (kept?.text === text) return kept.lines;
  const lines = text.split('\n');
  splitOutputs.set(source, { text, lines });
  return lines;
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
  // The delegated agent that produced this event: Claude's delegating call,
  // or the Codex thread a subagent runs in.
  const owner = asText(payload, 'parent_tool_use_id', 'agent_thread_id');

  // Recorded reasoning has its own row, separate from the assistant's output.
  if (type === 'thinking')
    return owner ? null : { kind: 'thinking', text: asText(payload, 'text'), sourceEventId };
  if (type === 'message') {
    // A delegated agent's answer belongs to its delegation's result, and is
    // separately inspectable in Trajectory. It is not the main reply.
    if (owner) return null;
    const previewId = asText(payload, 'preview_id');
    return {
      kind: 'message',
      text: asText(payload, 'text'),
      sourceEventId,
      ...(previewId ? { previewId } : {}),
    };
  }
  // A model call's token counts: read by the usage and stats views, never a
  // transcript row (it has no text, and each one counted as another step).
  if (type === 'usage') return null;
  // A message the owner sent into the running turn reads where it arrived,
  // between what the agent did before it and what it did after.
  if (type === 'user_message') {
    return { kind: 'user_message', text: asText(payload, 'text'), sourceEventId };
  }

  if (type === 'tool_use') {
    const name = asText(payload, 'name', 'tool') || 'Bash';
    const mcpServer = asText(payload, 'mcp_server');
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
      ...(mcpServer ? { mcpServer } : {}),
      id: asText(payload, 'id') || undefined,
      ...(Object.hasOwn(payload, 'input') ? { input: payload.input } : {}),
      ...(owner ? { parentToolUseId: owner } : {}),
      ...(payload.native_tool_status === 'completed' ? { resultState: 'received' as const } : {}),
      ...(payload.native_tool_status === 'cancelled' ? { resultState: 'cancelled' as const } : {}),
      ...(event.created_at ? { at: event.created_at } : {}),
      detail: asText(payload, 'detail', 'text') || commandText(payload.input) || 'Running tool',
      ...(output ? { output } : {}),
      ...(Object.hasOwn(payload, 'structured_output')
        ? { structuredOutput: payload.structured_output }
        : {}),
      ...(outputIsError ? { outputIsError: true } : {}),
      ...(output && markedTruncated(payload) ? { outputTruncated: true } : {}),
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
      ...(Object.hasOwn(payload, 'structured_output')
        ? { structuredOutput: payload.structured_output }
        : {}),
      ...(owner ? { parentToolUseId: owner } : {}),
      ...(payload.native_tool_status === 'cancelled' ? { cancelled: true } : {}),
      isError: payload.is_error === true,
      ...(markedTruncated(payload) ? { truncated: true } : {}),
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
    // A subagent finishing its own turn is not this conversation's milestone.
    if (owner) return null;
    const phase = asText(payload, 'phase', 'text') || 'lifecycle';
    if (phase === 'api_retry') {
      return { kind: 'lifecycle', text: modelRetryMessage(payload), sourceEventId };
    }
    // The runner reports whether it reused the tree; the milestone reads
    // differently depending on the answer, and only the runner can know it.
    if (phase === 'checked_out' && payload.reused === true) {
      return { kind: 'lifecycle', text: 'workspace_reused', sourceEventId };
    }
    // A temporary workspace cloned nothing: it starts from one empty commit.
    if (phase === 'checked_out' && payload.temporary === true) {
      return { kind: 'lifecycle', text: 'workspace_created', sourceEventId };
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
    if (phase === 'sandbox_incident') {
      return { kind: 'lifecycle', text: sandboxIncidentMessage(payload), sourceEventId };
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
  // No repository: nothing was cloned, and the tree starts empty.
  workspace_created: 'Empty workspace ready',
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
  // Own entries only: the text can be the agent's own words, and `__proto__`
  // must not find Object.prototype's.
  const known = Object.hasOwn(LIFECYCLE_LABELS, phase) ? LIFECYCLE_LABELS[phase] : undefined;
  if (known) return { text: known, milestone: true };
  if (phase.includes('superseded')) {
    return { text: 'Run interrupted', milestone: true };
  }
  return { text: value.replaceAll('_', ' '), milestone: false };
}

function nativeItemKey(event: AgentJobEventApi, id: string): string {
  return `${'job_id' in event ? event.job_id : ''}:${event.attempt_id}:${id}`;
}

/** A native item snapshot carries complete channels rather than text deltas. */
function reasoningSnapshot(
  stored: AgentJobEventApi,
  projected: AgentJobEventApi,
  identified: Set<string>,
): AgentJobEventApi {
  const notification = rawNotification(stored);
  if (!notification || notificationHasError(notification)) return projected;
  const item = record(record(notification.params).item);
  const id = typeof item.id === 'string' ? item.id : '';
  const method = notification.method;
  if (
    item.type !== 'reasoning' ||
    !id ||
    !['item/started', 'item/updated', 'item/completed'].includes(String(method)) ||
    (stored.payload?.protocol !== 'codex-app-server' && !identified.has(nativeItemKey(stored, id)))
  ) {
    return projected;
  }
  const blocks = [
    { channel: 'summary', content: item.summary },
    { channel: 'reasoning', content: item.content },
  ].flatMap(({ channel, content }) => {
    if (!Array.isArray(content)) return [];
    const parts = content.map((value) => (typeof value === 'string' ? value : ''));
    const text = parts.join('\n');
    return text ? [{ channel, text, parts }] : [];
  });
  return {
    ...projected,
    event_type: 'thinking',
    payload: {
      ...projected.payload,
      text: typeof item.text === 'string' ? item.text : '',
      native_item_id: id,
      stream_state: method === 'item/started' ? 'started' : 'snapshot',
      ...(blocks.length ? { reasoning_blocks: blocks } : {}),
    },
  };
}

/** Preserve attempt boundaries and fold updates into their recorded activity. */
export function displayEvents(events: AgentJobEventApi[], job?: AgentJobApi): AgentEvent[] {
  const attemptIds = [...new Set(events.map((event) => event.attempt_id))].sort((a, b) => a - b);
  const attemptNoById = new Map(attemptIds.map((id, index) => [id, index + 1]));
  const rows: AgentEvent[] = [];
  const toolsById = new Map<string, number>();
  const lastToolByAttempt = new Map<number, number>();
  const reasoningByItem = new Map<string, number>();
  const reasoningChannelsByItem = new Map<string, Map<string, Map<number, string>>>();
  // The row each answer still being written occupies, by attempt and preview.
  // An answer's first piece has no row yet, and searching back for one read
  // every row before it, once per answer and again for each streamed event.
  const streamingPreviewRows = new Map<string, number>();
  const identifiedReasoning = new Set(
    events.flatMap((event) => {
      const id = event.event_type === 'thinking' ? asText(event.payload, 'native_item_id') : '';
      return id ? [nativeItemKey(event, id)] : [];
    }),
  );

  const activityEvents = new Map(
    projectProtocolEvents(events)
      .map((event, index) => reasoningSnapshot(events[index], event, identifiedReasoning))
      .filter((event) => ['tool_use', 'tool_result', 'thinking'].includes(event.event_type))
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
    // Chat has its own answer-preview folding. Project native reasoning and
    // tools without projecting message fragments a second time.
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
          output: row.text ? linesOf(storedEvent, row.text) : [EMPTY_TOOL_OUTPUT],
          outputIsError: row.isError,
          ...(row.truncated ? { outputTruncated: true } : {}),
          ...(row.structuredOutput !== undefined ? { structuredOutput: row.structuredOutput } : {}),
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
      const previewKey = `${row.attemptNo}:${row.previewId}`;
      const previewIndex = streamingPreviewRows.get(previewKey);
      const preview = previewIndex === undefined ? undefined : rows[previewIndex];
      if (previewIndex !== undefined && preview?.kind === 'message') {
        // The first piece's id stays the row's identity, live and on reload,
        // so completing the answer never moves the reading anchor.
        rows[previewIndex] = !row.streaming
          ? { ...row, sourceEventId: preview.sourceEventId }
          : replyPreviewPiece(event)?.reset
            ? { ...preview, text: row.text }
            : { ...preview, text: preview.text + row.text };
        if (!row.streaming) streamingPreviewRows.delete(previewKey);
        continue;
      }
    }

    if (row.kind === 'thinking') {
      const nativeId = asText(event.payload, 'native_item_id');
      if (nativeId) {
        const key = nativeItemKey(event, nativeId);
        const index = reasoningByItem.get(key);
        const previous = index === undefined ? undefined : rows[index];
        const target = previous?.kind === 'thinking' ? previous : row;
        const channels = reasoningChannelsByItem.get(key) ?? new Map<string, Map<number, string>>();
        reasoningChannelsByItem.set(key, channels);
        const snapshotBlocks = event.payload?.reasoning_blocks;
        let updates: { text: string; channel: string; parts?: unknown[] }[] = [
          { text: row.text, channel: asText(event.payload, 'stream_channel') || 'summary' },
        ];
        if (Array.isArray(snapshotBlocks)) {
          updates = snapshotBlocks.map((value) => {
            const block = record(value);
            return {
              text: typeof block.text === 'string' ? block.text : '',
              channel: typeof block.channel === 'string' ? block.channel : 'summary',
              parts: Array.isArray(block.parts) ? block.parts : undefined,
            };
          });
        }
        const params = record(rawNotification(storedEvent)?.params);
        for (const update of updates) {
          if (!update.text) continue;
          if (event.payload?.stream_state === 'delta') {
            const parts = channels.get(update.channel) ?? new Map<number, string>();
            const nativeIndex =
              update.channel === 'reasoning' ? params.contentIndex : params.summaryIndex;
            const partIndex =
              typeof nativeIndex === 'number' && Number.isInteger(nativeIndex) && nativeIndex >= 0
                ? nativeIndex
                : 0;
            parts.set(partIndex, (parts.get(partIndex) ?? '') + update.text);
            channels.set(update.channel, parts);
          } else {
            channels.set(
              update.channel,
              new Map(
                (update.parts ?? [update.text]).map((text, partIndex) => [
                  partIndex,
                  typeof text === 'string' ? text : '',
                ]),
              ),
            );
          }
        }
        const blocks = [...channels].map(([channel, parts]) => ({
          channel,
          text: [...parts]
            .sort(([left], [right]) => left - right)
            .map(([, text]) => text)
            .join('\n'),
        }));
        const folded = {
          ...target,
          text: blocks.map((block) => block.text).join('\n\n'),
          ...(blocks.length ? { reasoningBlocks: blocks } : {}),
        };
        if (index === undefined) reasoningByItem.set(key, rows.push(folded) - 1);
        else rows[index] = folded;
        continue;
      }
      // Empty runtime statuses may repeat; recorded text blocks stay distinct.
      const previous = rows[rows.length - 1];
      if (
        !row.text.trim() &&
        previous?.kind === 'thinking' &&
        !previous.text.trim() &&
        previous.attemptNo === row.attemptNo
      ) {
        continue;
      }
    }

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

    const index = rows.push(row) - 1;
    if (row.kind === 'message' && row.streaming === true && row.previewId)
      streamingPreviewRows.set(`${row.attemptNo}:${row.previewId}`, index);
  }

  return rows;
}

/** A delegated agent's recorded identity: the call that started it and its Codex threads. */
export interface SubagentOwner {
  callId?: string;
  threads: readonly string[];
}

/**
 * Whether a record's owner fields assign it to this subagent: to the
 * delegating call (`callId`) or, for Codex, to one of the threads that call
 * started.
 */
export function ownedBySubagent(
  payload: Record<string, unknown> | null | undefined,
  owner: SubagentOwner,
): boolean {
  const parent = asText(payload ?? {}, 'parent_tool_use_id');
  const thread = asText(payload ?? {}, 'agent_thread_id');
  // One call may start several threads, and each thread's records name that
  // call. Once the threads are known, a record belongs to its own thread.
  if (thread && owner.threads.length) return owner.threads.includes(thread);
  return Boolean(parent) && parent === owner.callId;
}

/**
 * One subagent's own transcript, read as if that subagent were the
 * conversation: the records `ownedBySubagent` assigns to it. Its turn
 * milestones stay runtime bookkeeping rather than rows.
 */
export function subagentDisplayEvents(
  events: AgentJobEventApi[],
  owner: SubagentOwner,
  job?: AgentJobApi,
): AgentEvent[] {
  const own = events.flatMap((event) => {
    const payload = event.payload ?? {};
    if (!ownedBySubagent(payload, owner)) return [];
    const { parent_tool_use_id: _parent, agent_thread_id: _thread, ...rest } = payload;
    return [{ ...event, payload: rest }];
  });
  return displayEvents(own, job).filter((row) => row.kind !== 'lifecycle');
}

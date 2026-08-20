// The trajectory model: one row per stored event, unfolded.
//
// The chat view is a *reading* of the stream — tool results fold into the call
// that made them, repeated thinking collapses to one status line, and anything
// the normalizer did not recognize is dropped. That is right for reading and
// wrong for inspecting: someone asking "what actually happened in this turn"
// needs the rows the platform stored, in the order it stored them, including
// the ones no view knows how to render.
//
// So this projects the raw API events rather than the display model. Nothing
// here is folded, deduplicated, or hidden.

import type { AgentJobEventApi } from '@/lib/api/agents';

/** The lanes the minimap groups events into, coarse on purpose. */
export type TrajectoryLane = 'input' | 'model' | 'tools' | 'other';

export interface TrajectoryRow {
  id: number;
  attemptId: number;
  seq: number;
  eventType: string;
  /** Who or what this row is, in the vocabulary the reader sees. */
  actor: 'user' | 'assistant' | 'tool' | 'context' | 'system';
  lane: TrajectoryLane;
  /** One-line label: the tool's name, the lifecycle phase, the event type. */
  label: string;
  /** Compact preview of what the row carries, already truncated. */
  detail: string;
  at: string | null;
  isError: boolean;
  payload: Record<string, unknown>;
  /** For a tool call: the result row that answered it, when one is stored. */
  resultOf?: number;
  /** Milliseconds from a tool call to its result, when both carry a time. */
  durationMs?: number;
}

const LANE_BY_TYPE: Record<string, TrajectoryLane> = {
  message: 'model',
  thinking: 'model',
  usage: 'model',
  tool_use: 'tools',
  tool_result: 'tools',
  diff: 'tools',
  lifecycle: 'other',
  error: 'other',
  raw: 'other',
};

const ACTOR_BY_TYPE: Record<string, TrajectoryRow['actor']> = {
  message: 'assistant',
  thinking: 'assistant',
  tool_use: 'assistant',
  tool_result: 'tool',
  diff: 'tool',
  usage: 'context',
  lifecycle: 'system',
  error: 'system',
  raw: 'system',
};

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

/** A single line, trimmed and bounded — rows are one line high. */
export function preview(value: unknown, limit = 160): string {
  const text = asText(value).replace(/\s+/g, ' ').trim();
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

function labelOf(eventType: string, payload: Record<string, unknown>): string {
  if (eventType === 'tool_use') return asText(payload.name || payload.tool) || 'tool';
  if (eventType === 'lifecycle') return asText(payload.phase) || 'lifecycle';
  if (eventType === 'error') return asText(payload.host) ? 'egress denied' : 'error';
  return eventType;
}

function detailOf(eventType: string, payload: Record<string, unknown>): string {
  if (eventType === 'tool_use') {
    return preview(payload.detail ?? payload.input ?? payload.text);
  }
  if (eventType === 'tool_result') {
    return preview(payload.content ?? payload.output ?? payload.aggregated_output ?? payload.text);
  }
  if (eventType === 'raw') {
    // The whole point of a raw row is that nobody parsed it. Show the line.
    return preview(payload.line ?? payload.text ?? payload);
  }
  return preview(payload.text ?? payload.detail ?? payload);
}

/**
 * Project stored events into trajectory rows, pairing calls with results.
 *
 * Pairing is scoped to the attempt, because a retried turn repeats its tool
 * ids: matching on the bare id would time a call against another attempt's
 * answer. The duration is message-level — it opens when the model asked for
 * the call, so it carries whatever thinking preceded execution — and the view
 * says so rather than implying it measured the tool itself.
 */
export function toTrajectory(events: AgentJobEventApi[]): TrajectoryRow[] {
  const resultByCall = new Map<string, AgentJobEventApi>();
  for (const event of events) {
    if (event.event_type !== 'tool_result') continue;
    const id = asText(asRecord(event.payload).tool_use_id);
    if (id) resultByCall.set(`${event.attempt_id}:${id}`, event);
  }

  return events.map((event) => {
    const payload = asRecord(event.payload);
    const eventType = event.event_type;
    const callId = eventType === 'tool_use' ? asText(payload.id) : '';
    const result = callId ? resultByCall.get(`${event.attempt_id}:${callId}`) : undefined;
    const started = event.created_at ? Date.parse(event.created_at) : NaN;
    const ended = result?.created_at ? Date.parse(result.created_at) : NaN;
    const durationMs =
      Number.isFinite(started) && Number.isFinite(ended) && ended >= started
        ? ended - started
        : undefined;

    return {
      id: event.id,
      attemptId: event.attempt_id,
      seq: event.seq,
      eventType,
      actor: ACTOR_BY_TYPE[eventType] ?? 'system',
      lane: LANE_BY_TYPE[eventType] ?? 'other',
      label: labelOf(eventType, payload),
      detail: detailOf(eventType, payload),
      at: event.created_at ?? null,
      isError:
        payload.is_error === true ||
        eventType === 'error' ||
        (typeof payload.exit_code === 'number' && payload.exit_code !== 0),
      payload,
      ...(result ? { resultOf: result.id } : {}),
      ...(durationMs !== undefined ? { durationMs } : {}),
    };
  });
}

/**
 * Filter rows by a free-text query.
 *
 * Matches the label, the preview and the event type, which is what a reader
 * can see on the row. Searching the full payload would find rows whose match
 * is invisible until they open one, which reads as a broken filter.
 */
export function filterTrajectory(rows: TrajectoryRow[], query: string): TrajectoryRow[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return rows;
  return rows.filter((row) =>
    `${row.label} ${row.detail} ${row.eventType}`.toLowerCase().includes(needle),
  );
}

/** Serialize the stored stream for download, exactly as the platform holds it. */
export function toSessionLog(events: AgentJobEventApi[]): string {
  return events.map((event) => JSON.stringify(event)).join('\n');
}

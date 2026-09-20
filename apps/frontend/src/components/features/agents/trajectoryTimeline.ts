// The overview's domain model.
//
// The step list is an order; the overview is a picture of that order. This
// module is the projection between the two: every step becomes a span on one of
// the four lanes, either one operation wide (`steps`) or as wide as the platform
// recorded it took (`time`), plus the turn boundaries a reader navigates by.
//
// Two rules shape it. Nothing here invents a time: a step the platform did not
// time keeps its place in the order and says so through `timed`. And a span is
// never dropped for lack of a timestamp — a projection that silently loses rows
// is worse than one that draws a marker.

import type { TrajectoryLane } from './trajectory';
import type { StepKind, StepStatus, TrajectoryStep } from './trajectorySteps';

/** How the overview projects a step onto the track. */
export type TimelineMode = 'steps' | 'time';

/** A closed interval in the active projection's domain. */
export interface DomainRange {
  start: number;
  end: number;
}

/** One step, projected into the active domain. */
export interface TimelineSpan extends DomainRange {
  /** The step's stable key; selection is by this, never by position. */
  key: string;
  /** Position in the step list, for ordering the ledger's focus. */
  index: number;
  /** The number the reader sees, which is scoped to the turn. */
  stepNo: number;
  turnNo: number;
  lane: TrajectoryLane;
  label: string;
  kind: StepKind;
  status: StepStatus;
  isError: boolean;
  /** The recorded duration behind the span's width, when there is one. */
  durationMs: number | null;
  /** Whether the width came from a recorded duration rather than the order. */
  timed: boolean;
}

/** Where a turn starts in the active domain. */
export interface TimelineTurnBoundary {
  turnNo: number;
  at: number;
}

/** The whole run, projected. */
export interface TimelineModel extends DomainRange {
  /** The projection the spans were derived with; `time` falls back to `steps`. */
  mode: TimelineMode;
  spans: TimelineSpan[];
  turns: TimelineTurnBoundary[];
  /** Narrowest window a reader may zoom to, in domain units. */
  minimumWindow: number;
  /** Whether any span carries recorded time. */
  timed: boolean;
}

/**
 * Operations a reader may zoom to in `steps`, mirroring the desktop harness:
 * four operations across the card is as far in as the picture stays useful.
 */
export const MINIMUM_ZOOM_OPERATIONS = 4;

/** Recorded milliseconds a reader may zoom to in `time`, mirroring the harness. */
export const MINIMUM_ZOOM_MS = 20;

function isError(step: TrajectoryStep): boolean {
  return step.isError || step.status === 'failed';
}

function spanOf(
  step: TrajectoryStep,
  index: number,
  range: DomainRange,
  timed: boolean,
): TimelineSpan {
  return {
    key: step.key,
    index,
    stepNo: step.stepNo,
    turnNo: step.turnNo,
    lane: step.lane,
    label: step.label,
    kind: step.kind,
    status: step.status,
    isError: isError(step),
    durationMs: step.timing.durationMs,
    timed,
    ...range,
  };
}

function sequenceModel(steps: TrajectoryStep[]): TimelineModel {
  const spans = steps.map((step, index) =>
    spanOf(step, index, { start: index, end: index + 1 }, false),
  );
  const turns = turnBoundaries(spans);
  return {
    mode: 'steps',
    start: 0,
    end: Math.max(1, spans.length),
    spans,
    turns,
    minimumWindow: Math.min(MINIMUM_ZOOM_OPERATIONS, Math.max(1, spans.length)),
    timed: false,
  };
}

/**
 * Project the run by recorded time.
 *
 * A step's span starts at the time its first record carries and ends at the
 * recorded finish; the idle time between spans is compressed away, because an
 * agent that waited twenty minutes on a human would otherwise flatten every
 * span in the run to nothing. Returns `null` when no step carries any timing,
 * so the caller can fall back to the order rather than drawing a flat line.
 */
function timedModel(steps: TrajectoryStep[]): TimelineModel | null {
  let cursor: number | null = null;
  let timed = false;
  const raw: TimelineSpan[] = [];

  steps.forEach((step, index) => {
    const parsed = step.at === null ? Number.NaN : Date.parse(step.at);
    const startedAt = Number.isFinite(parsed) ? parsed : null;
    const durationMs =
      step.timing.durationMs !== null && step.timing.durationMs > 0 ? step.timing.durationMs : 0;
    if (startedAt === null && durationMs === 0 && cursor === null) return;
    const start = startedAt ?? cursor ?? 0;
    // A recorded start before the previous finish is not overwritten: work that
    // overlapped overlapped, and the lanes can show it.
    if (startedAt !== null || step.timing.durationMs !== null) timed = true;
    raw.push(spanOf(step, index, { start, end: start + durationMs }, durationMs > 0));
    cursor = Math.max(cursor ?? start, start + durationMs);
  });

  if (!timed || raw.length === 0) return null;

  // Idle time is removed once, over the spans in time order, so a span keeps
  // the offset of every gap that closed before it.
  const removedByKey = new Map<string, number>();
  let removed = 0;
  let coveredUntil: number | null = null;
  for (const span of [...raw].sort(
    (left, right) => left.start - right.start || left.end - right.end,
  )) {
    if (coveredUntil !== null && span.start > coveredUntil) removed += span.start - coveredUntil;
    removedByKey.set(span.key, removed);
    coveredUntil = coveredUntil === null ? span.end : Math.max(coveredUntil, span.end);
  }
  const spans = raw.map((span) => {
    const offset = removedByKey.get(span.key) ?? 0;
    return { ...span, start: span.start - offset, end: span.end - offset };
  });

  const start = Math.min(...spans.map((span) => span.start));
  const end = Math.max(...spans.map((span) => span.end));
  return {
    mode: 'time',
    start,
    end: Math.max(end, start + 1),
    spans,
    turns: turnBoundaries(spans),
    minimumWindow: Math.min(MINIMUM_ZOOM_MS, Math.max(1, end - start)),
    timed: true,
  };
}

function turnBoundaries(spans: TimelineSpan[]): TimelineTurnBoundary[] {
  const boundaries: TimelineTurnBoundary[] = [];
  const seen = new Set<number>();
  for (const span of spans) {
    if (seen.has(span.turnNo)) continue;
    seen.add(span.turnNo);
    boundaries.push({ turnNo: span.turnNo, at: span.start });
  }
  return boundaries;
}

/**
 * Project the run for the overview.
 *
 * `time` falls back to `steps` when the platform recorded no timing at all, so
 * the toggle can never produce an empty picture.
 */
export function deriveTimeline(steps: TrajectoryStep[], mode: TimelineMode): TimelineModel {
  if (mode === 'time') {
    const timed = timedModel(steps);
    if (timed !== null) return timed;
  }
  return sequenceModel(steps);
}

/** Whether this run carries any timing a `time` projection could use. */
export function hasRecordedTiming(steps: TrajectoryStep[]): boolean {
  return steps.some(
    (step) =>
      (step.at !== null && Number.isFinite(Date.parse(step.at))) ||
      (step.timing.durationMs !== null && step.timing.durationMs > 0),
  );
}

/** The steps whose spans intersect an inclusive selection. */
export function focusKeys(model: TimelineModel, range: DomainRange): Set<string> {
  return new Set(
    model.spans
      .filter((span) => span.start <= range.end && span.end >= range.start)
      .map((span) => span.key),
  );
}

/** The span nearest a domain value, for a click that landed between marks. */
export function nearestSpan(model: TimelineModel, value: number): TimelineSpan | null {
  let nearest: TimelineSpan | null = null;
  let distance = Number.POSITIVE_INFINITY;
  for (const span of model.spans) {
    const gap = value < span.start ? span.start - value : value > span.end ? value - span.end : 0;
    if (gap < distance) {
      distance = gap;
      nearest = span;
    }
  }
  return nearest;
}

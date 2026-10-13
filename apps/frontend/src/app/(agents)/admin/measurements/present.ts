// Pure presentation for the Admin measurements page.
//
// Separate from `page.tsx` because Next type-checks a page's exports, and
// tested directly rather than only through the rendered page. Nothing here
// fetches or renders: it turns stored records into the words and rows the page
// shows, and keeps an unknown value unknown instead of rendering it as zero.
//
// The turn page shows a turn's startup as one line that links to its sample
// here, so the breakdown is read in one place only.

import type {
  MeasurementRecord,
  MeasurementSummary,
  MeasurementSummaryResponse,
  ProfilingRun,
} from '@/lib/api/adminMeasurements';

import { number } from '../runs/shared';

/** The route this page owns, for links and for writing its own selection back. */
export const MEASUREMENTS_PATH = '/admin/measurements';

/**
 * What every number on the page covers, said once, in the page's tooltip.
 *
 * There is no browser-side producer, so every stored record stops at the
 * server: none of these durations is the wait a user saw.
 */
export const SERVER_SIDE_NOTE =
  'Timed on the server only. A sandbox startup runs from the runner claiming the turn to the ' +
  'prompt reaching the agent; a terminal or file request runs from the request being ' +
  'authorized to its result being ready. Network, response transfer and browser time are not ' +
  'included, so none of these is the wait a user saw.';

/** The minimum eligible sample a percentile needs before it is worth reading. */
export const MIN_PERCENTILE_SAMPLES = 20;

/** Present the stable telemetry ID using the service's current name. */
export function producerLabel(producer: string): string {
  return producer === 'broker' || producer === 'sandbox_manager' ? 'Sandbox Manager' : producer;
}

// ---------------------------------------------------------------------------
// Durations and times
// ---------------------------------------------------------------------------

export interface FormattedDuration {
  /** Rendered value; `—` for an unreported duration, never `0`. */
  display: string;
  /** The exact recorded microseconds, for a tooltip: the display rounds, the record does not. */
  exact: string | null;
}

/**
 * Format recorded microseconds in the unit a reader compares in.
 *
 * Microseconds are what the producer recorded and stay exact in `exact`; the
 * display switches to milliseconds and then seconds so a 12-second startup
 * reads as `12.40 s` rather than `12,401.79 ms`. A missing value is `—`: an
 * unreported duration is unknown, and a zero would be a claim the record never
 * made.
 */
export function duration(us: number | null | undefined): FormattedDuration {
  if (typeof us !== 'number' || !Number.isFinite(us)) return { display: '—', exact: null };
  const exact = `${number(us)} µs`;
  const size = Math.abs(us);
  if (size < 1_000) return { display: exact, exact };
  if (size < 1_000_000) {
    const ms = (us / 1_000).toLocaleString('en-US', {
      maximumFractionDigits: size < 10_000 ? 1 : 0,
    });
    return { display: `${ms} ms`, exact };
  }
  if (size < 60_000_000) {
    const seconds = (us / 1_000_000).toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    return { display: `${seconds} s`, exact };
  }
  const minutes = Math.floor(size / 60_000_000);
  const seconds = Math.round((size % 60_000_000) / 1_000_000);
  return { display: `${us < 0 ? '-' : ''}${minutes} min ${seconds} s`, exact };
}

/**
 * One value's share of another, as a percentage.
 *
 * Null when the whole is zero or unreported: a share of nothing is not 0%, and
 * certainly not 100%. Callers render null as unknown rather than as a number.
 */
export function shareOf(
  part: number | null | undefined,
  whole: number | null | undefined,
): string | null {
  if (typeof part !== 'number' || !Number.isFinite(part)) return null;
  if (typeof whole !== 'number' || !Number.isFinite(whole) || whole <= 0) return null;
  const percent = (part / whole) * 100;
  return `${percent.toLocaleString('en-US', { maximumFractionDigits: 1 })}%`;
}

/** An instant as `Sep 22, 08:00 UTC`, for labels where the full form is too long. */
export function shortTime(value: string | null | undefined): string {
  if (!value || !Number.isFinite(Date.parse(value))) return '—';
  return `${new Date(value).toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
    timeZone: 'UTC',
  })} UTC`;
}

// ---------------------------------------------------------------------------
// Names
// ---------------------------------------------------------------------------

const OPERATION_NAMES: Record<string, string> = {
  'attempt.startup': 'Sandbox startup',
  'terminal.open': 'Terminal opening',
  'files.list': 'File listing',
  'files.open': 'File opening',
};

/** The operations an admin can filter the sample list by, in the order they are offered. */
export const LISTED_OPERATIONS = Object.keys(OPERATION_NAMES);

/**
 * The requests one operation is made of, where there is more than one kind,
 * in the order they happen.
 */
const REQUEST_KIND_NAMES: Record<string, string> = {
  'terminal.list': 'list terminals',
  'terminal.create': 'create terminal',
  'terminal.stream_attach': 'attach stream',
  'files.read': 'read file',
};

/**
 * The four startup paths the runner classifies, by what they had to do.
 *
 * They differ by the work they skipped, so they are never folded together: a
 * new sandbox and a reused live one are different operations.
 */
const STARTUP_PATH_NAMES: Record<string, string> = {
  new_group: 'new sandbox',
  resume_stopped: 'stopped sandbox restarted',
  resume_running: 'running sandbox, agent restarted',
  reuse_live: 'live sandbox reused',
  unknown: 'path unclassified',
};

/** An operation in the words an operator uses, or the raw name when this page does not know it. */
export function operationName(operation: string): string {
  return OPERATION_NAMES[operation] ?? operation;
}

/**
 * One line naming what a sample measured.
 *
 * The operation, then which request of it, then which startup path. A coverage
 * scope is added only when it is not one this page knows: every known scope
 * follows from the operation, while an unknown one may cover something the
 * page's server-side note does not, and must not be read under it.
 */
export function operationLabel(item: {
  operation: string;
  request_kind?: string | null;
  startup_path?: string | null;
  coverage_scope?: string | null;
}): string {
  const parts = [operationName(item.operation)];
  const kind = item.request_kind;
  if (kind && kind !== item.operation) parts.push(REQUEST_KIND_NAMES[kind] ?? kind);
  const path = item.startup_path;
  if (path) parts.push(STARTUP_PATH_NAMES[path] ?? path);
  const scope = item.coverage_scope;
  if (scope && !SCOPES[scope]) parts.push(coverageScope(scope).label);
  return parts.join(' · ');
}

/** A stage or span name as words: `supervisor_acquire` reads as `Supervisor acquire`. */
export function stageName(name: string): string {
  const words = name
    .replace(/^(?:broker|sandbox_manager)(?=[_.]|$)/, 'Sandbox Manager')
    .replace(/[_.]+/g, ' ')
    .trim();
  return words ? `${words.charAt(0).toUpperCase()}${words.slice(1)}` : name;
}

// ---------------------------------------------------------------------------
// Outcome and completeness
// ---------------------------------------------------------------------------

/** The outcomes the server counts as eligible for a distribution. */
const COMPLETE_OUTCOMES = new Set(['completed', 'dispatched']);

const OUTCOME_LABELS: Record<string, string> = {
  dispatched: 'Dispatched',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
  lease_lost: 'Lease lost',
  incomplete: 'Incomplete',
  superseded: 'Superseded',
  ok: 'OK',
  error: 'Error',
};

/** Whether this outcome is one the summary's percentiles include. */
export function isCompleteOutcome(outcome: string): boolean {
  return COMPLETE_OUTCOMES.has(outcome);
}

/** A recorded outcome, or the raw value when this page does not know the word. */
export function outcomeLabel(outcome: string | null | undefined): string {
  if (!outcome) return 'Outcome not reported';
  return OUTCOME_LABELS[outcome] ?? `${outcome} (unrecognised outcome)`;
}

export interface SampleResult {
  label: string;
  /** `ok` is quiet; the other two are the only results a reader has to act on. */
  tone: 'ok' | 'warn' | 'bad';
  title: string;
}

/**
 * What a list row says about how its operation ended.
 *
 * Two different problems, said apart: an operation that did not succeed, and a
 * successful one whose collection did not close -- its duration may stop short
 * of the operation's end. A summary row carries no truncation flag, so a row
 * can be called partial here but never whole; the record says the rest.
 */
export function sampleResult(
  row: Pick<MeasurementSummary, 'outcome' | 'collection_complete'>,
): SampleResult {
  if (!isCompleteOutcome(row.outcome)) {
    return {
      label: outcomeLabel(row.outcome),
      tone: 'bad',
      title: 'The operation did not complete, so this sample is kept out of the timings.',
    };
  }
  if (!row.collection_complete) {
    return {
      label: 'Partly measured',
      tone: 'warn',
      title:
        'The operation completed, but collection did not close: the duration may stop short, ' +
        'so this sample is kept out of the timings.',
    };
  }
  return { label: 'OK', tone: 'ok', title: outcomeLabel(row.outcome) };
}

/**
 * Every reason this record is not a whole measurement.
 *
 * Empty only when nothing in the record says it is partial. A failure and an
 * incomplete collection are different reasons and are listed separately,
 * because "the operation failed" and "we stopped looking" need different
 * follow-ups.
 */
export function partialReasons(record: MeasurementRecord): string[] {
  const reasons: string[] = [];
  if (!record.collection_complete) {
    reasons.push(
      'Collection did not close: at least one expected boundary was never answered, or the ' +
        'record was dropped, expired or truncated before the end.',
    );
  }
  if (!isCompleteOutcome(record.outcome)) {
    reasons.push(
      record.failure_stage
        ? `Outcome ${outcomeLabel(record.outcome)} at stage “${record.failure_stage}”.`
        : `Outcome ${outcomeLabel(record.outcome)}: this operation did not complete.`,
    );
  }
  if (record.truncated) {
    reasons.push(
      'A producer stopped at its own deadline, so the durations it did report are floors.',
    );
  }
  const truncated = record.truncated_fragments ?? [];
  if (truncated.length) {
    reasons.push(
      `${number(truncated.length)} fragment(s) answered but were cut short: ${truncated.join(', ')}.`,
    );
  }
  const missing = record.missing_fragments ?? [];
  if (missing.length) {
    reasons.push(
      `${number(missing.length)} expected fragment(s) never answered: ${missing.join(', ')}.`,
    );
  }
  const producers = record.missing_producers ?? [];
  if (producers.length) {
    reasons.push(`Producer(s) that never reported: ${producers.map(producerLabel).join(', ')}.`);
  }
  if (typeof record.dropped_spans === 'number' && record.dropped_spans > 0) {
    reasons.push(
      `${number(record.dropped_spans)} span(s) were dropped under the record's bound and are not here.`,
    );
  }
  return reasons;
}

// ---------------------------------------------------------------------------
// Coverage
// ---------------------------------------------------------------------------

export interface CoverageLabel {
  label: string;
  basis: string;
}

const SCOPES: Record<string, CoverageLabel> = {
  server_request: {
    label: 'Server request',
    basis:
      'Control-plane-rooted: starts after authentication, ownership and sandbox manager resolution and ' +
      'ends when the business result is validated. Response encoding, transfer and browser work ' +
      'are outside it.',
  },
  host_startup: {
    label: 'Host startup',
    basis:
      'Runner-rooted: starts at the claim and ends when the supervisor dispatches the prompt. ' +
      'Control-plane queueing, first model output and browser wait are outside it.',
  },
};

/**
 * What a coverage scope covers, without inventing one for a value the server
 * has never sent this page.
 */
export function coverageScope(scope: string | null | undefined): CoverageLabel {
  if (!scope) {
    return {
      label: 'Scope not reported',
      basis:
        'This record does not name a coverage scope, so nothing can be said about what it covers.',
    };
  }
  const known = SCOPES[scope];
  if (known) return known;
  return {
    label: `${scope} (unknown scope)`,
    basis:
      'The server reported a coverage scope this page does not know. It is not evidence of ' +
      'end-to-end coverage, and the page will not guess at its boundaries.',
  };
}

/** One line naming the scope of a record or row. */
export function coverageLine(scope: string | null | undefined): string {
  const { label, basis } = coverageScope(scope);
  return `${label} — ${basis}`;
}

// ---------------------------------------------------------------------------
// One record's timeline
// ---------------------------------------------------------------------------

/** What an attached duration is and is not, for the tooltip where one is shown. */
export const ATTACHED_STAGE_NOTE =
  'Measured by another process and attached to the stage above. It has no position on this ' +
  'timeline and is not added to the total.';

/** What a truncated duration is, for the tooltip where one is shown. */
export const TRUNCATED_STAGE_NOTE =
  'Observed up to the collection deadline: a floor, not the stage’s full duration.';

/** The name of an interval no stage or span covers. */
export const GAP_NAME = 'No stage recorded';

/** What such an interval is, for its tooltip. */
export const GAP_NOTE =
  'Part of the total, but no stage or span on this record covers this interval, so nothing ' +
  'says what happened in it.';

export interface TimelineRow {
  key: string;
  /**
   * `stage` and `span` sit on the record's own clock; `gap` is an interval of
   * it nothing covers; `attached` is another process's duration, with no
   * position on this clock.
   */
  kind: 'stage' | 'span' | 'gap' | 'attached';
  depth: number;
  name: string;
  /** The recorded name, when the display name was made from it. */
  rawName: string | null;
  /** Offset on the record's timeline; null where the record gives none. */
  startUs: number | null;
  durationUs: number | null;
  /** Share of the record's total; null off the timeline. */
  share: string | null;
  /** The process that measured an attached duration. */
  source: string | null;
  /** An outcome worth showing: anything but success. */
  problem: string | null;
  /** A floor rather than a duration. */
  truncated: boolean;
}

function problemOutcome(outcome: string | null | undefined): string | null {
  if (!outcome || outcome === 'ok' || isCompleteOutcome(outcome)) return null;
  return outcomeLabel(outcome);
}

/** Whether a stage row and an interval share any time on the record's timeline. */
function overlaps(
  row: TimelineRow,
  startUs: number | null | undefined,
  durationUs: number | null | undefined,
): boolean {
  if (row.startUs === null || typeof startUs !== 'number') return false;
  const rowEnd = row.startUs + (row.durationUs ?? 0);
  const end = startUs + (typeof durationUs === 'number' ? durationUs : 0);
  return startUs < rowEnd && row.startUs < end;
}

/**
 * One record as a waterfall: where its time went, in time order.
 *
 * Stages and root spans are placed by their offset on the record's clock. An
 * interval none of them covers is shown as its own row, because "the first
 * stage began five seconds in" is the finding, and a list of stage durations
 * hides it. Gaps smaller than 1% of the total (and under a millisecond) are
 * boundary noise and are not shown.
 *
 * An attached child -- another process's duration -- goes under its stage with
 * no bar and no share: it is not on this clock and is never part of the total.
 * A stage without an offset cannot be placed; it is listed after the placed
 * ones, without a bar, rather than given a position it never recorded.
 */
export function timelineRows(
  record: Pick<MeasurementRecord, 'stages' | 'spans' | 'duration_us'>,
): TimelineRow[] {
  const total = record.duration_us;
  interface Entry {
    row: TimelineRow;
    children: TimelineRow[];
    order: number;
  }
  const placed: Entry[] = [];
  const unplaced: Entry[] = [];
  let order = 0;

  (record.stages ?? []).forEach((stage, index) => {
    const start = typeof stage.start_offset_us === 'number' ? stage.start_offset_us : null;
    const entry: Entry = {
      order: order++,
      row: {
        key: `stage-${index}`,
        kind: 'stage',
        depth: 0,
        name: stageName(stage.stage),
        rawName: stage.stage,
        startUs: start,
        durationUs: stage.duration_us,
        share: start === null ? null : shareOf(stage.duration_us, total),
        source: null,
        problem: null,
        truncated: false,
      },
      children: (stage.children ?? []).map((child, childIndex) => ({
        key: `stage-${index}-child-${childIndex}`,
        kind: 'attached' as const,
        depth: 1,
        name: stageName(child.stage),
        rawName: child.stage,
        startUs: null,
        durationUs: child.duration_us,
        share: null,
        source: child.source ?? null,
        problem: problemOutcome(child.outcome),
        truncated: Boolean(child.truncated),
      })),
    };
    (start === null ? unplaced : placed).push(entry);
  });

  const spans = record.spans ?? [];
  const spanIds = new Set(spans.map((span) => span.span_id));
  const spanEntries = new Map<string, Entry>();
  const spanRow = (span: (typeof spans)[number], depth: number): TimelineRow => ({
    key: `span-${span.span_id}`,
    kind: 'span',
    depth,
    name: stageName(span.name),
    rawName: span.name,
    startUs: span.start_offset_us,
    durationUs: typeof span.duration_us === 'number' ? span.duration_us : null,
    share: shareOf(span.duration_us, total),
    source: null,
    problem: problemOutcome(span.outcome),
    truncated: Boolean(span.truncated),
  });
  const hasChildren = new Set(spans.map((span) => span.parent_span_id).filter(Boolean));
  for (const span of spans) {
    // A child whose parent is not on this record is shown as a root rather
    // than dropped: it was recorded, and hiding it would shorten the record.
    if (span.parent_span_id && spanIds.has(span.parent_span_id)) continue;
    // The runner records some steps (preflight) as both a stage and a span
    // over the same interval. One row says it; the span only adds a problem.
    const twin = placed.find(
      (entry) =>
        !hasChildren.has(span.span_id) &&
        entry.row.kind === 'stage' &&
        entry.row.rawName === span.name &&
        overlaps(entry.row, span.start_offset_us, span.duration_us),
    );
    if (twin) {
      twin.row.problem ??= problemOutcome(span.outcome);
      twin.row.truncated ||= Boolean(span.truncated);
      continue;
    }
    const entry: Entry = { order: order++, row: spanRow(span, 0), children: [] };
    spanEntries.set(span.span_id, entry);
    placed.push(entry);
  }
  for (const span of spans) {
    if (!span.parent_span_id || !spanIds.has(span.parent_span_id)) continue;
    spanEntries.get(span.parent_span_id)?.children.push(spanRow(span, 1));
  }

  placed.sort((a, b) => (a.row.startUs ?? 0) - (b.row.startUs ?? 0) || a.order - b.order);

  const rows: TimelineRow[] = [];
  const threshold = Math.max(1_000, (Number.isFinite(total) ? total : 0) * 0.01);
  let covered = 0;
  const gap = (from: number, to: number) => {
    if (to - from <= threshold) return;
    rows.push({
      key: `gap-${from}`,
      kind: 'gap',
      depth: 0,
      name: GAP_NAME,
      rawName: null,
      startUs: from,
      durationUs: to - from,
      share: shareOf(to - from, total),
      source: null,
      problem: null,
      truncated: false,
    });
  };
  for (const entry of placed) {
    const start = entry.row.startUs ?? 0;
    gap(covered, start);
    rows.push(entry.row, ...entry.children);
    covered = Math.max(covered, start + (entry.row.durationUs ?? 0));
  }
  if (placed.length && Number.isFinite(total)) gap(covered, total);
  for (const entry of unplaced) rows.push(entry.row, ...entry.children);
  return rows;
}

/** The first and last boundary a record was measured between, in words. */
export function measuredBetween(
  record: Pick<MeasurementRecord, 'coverage_scope' | 'start_boundary' | 'end_boundary'>,
): string | null {
  if (record.coverage_scope === 'host_startup') {
    return 'From the runner claiming the turn to the prompt reaching the agent';
  }
  if (record.coverage_scope === 'server_request') {
    return 'From the request being authorized to its result being ready';
  }
  if (record.start_boundary && record.end_boundary) {
    return `From ${record.start_boundary} to ${record.end_boundary}`;
  }
  return null;
}

// ---------------------------------------------------------------------------
// Timings over a whole run
// ---------------------------------------------------------------------------

export interface TimingRow {
  key: string;
  label: string;
  /** What else tells this row apart from its neighbours, when anything does. */
  detail: string | null;
  /** Every dimension the server grouped by, exactly as named, for a tooltip. */
  title: string;
  samples: number;
  eligible: number;
  /** Stored samples kept out of the percentiles, and why, when there are any. */
  excluded: { count: number; label: string; title: string } | null;
  min: FormattedDuration;
  p50: FormattedDuration;
  p95: FormattedDuration;
  max: FormattedDuration;
  /** Fewer eligible samples than a percentile needs to be read as a distribution. */
  few: boolean;
}

/**
 * Dimensions a timing row never adds as detail: the label already says the
 * first four, and the runtime version is said together with the runtime.
 */
const LABELLED_DIMENSIONS = new Set([
  'operation',
  'request_kind',
  'startup_path',
  'coverage_scope',
  'runtime_version',
]);

function dimensionText(name: string, group: Record<string, string | number | null>): string {
  const value = group[name];
  if (name === 'runtime_id') {
    const version = group.runtime_version;
    if (value === null || value === undefined || value === '') return 'runtime not reported';
    return version ? `${value} ${version}` : String(value);
  }
  if (value === null || value === undefined || value === '') return `${name} not reported`;
  if (name === 'boundary_version') return `boundary v${value}`;
  if (name === 'execution_profile') return String(value);
  return `${name} ${value}`;
}

/**
 * A run's summary as one row per comparable group.
 *
 * The server groups by every dimension that decides comparability, so two
 * rows can share an operation and differ only by runtime version. The label
 * names the operation, request and startup path; any other dimension is added
 * only when it actually differs between rows of the same operation, so the
 * common case reads as one short line and the rare one still tells its rows
 * apart. A dimension this page has never heard of is treated the same way
 * rather than dropped.
 *
 * Percentiles cover only the eligible samples -- completed, with collection
 * closed -- and the rest are counted beside them rather than hidden. With no
 * eligible sample a percentile is `—`: there is nothing to compute it from.
 */
export function timingRows(response: MeasurementSummaryResponse): TimingRow[] {
  // operation -> dimension -> the values its groups carry.
  const seen = new Map<string, Map<string, Set<string>>>();
  for (const group of response.groups) {
    const dims = group.group ?? {};
    const operation = String(dims.operation ?? '');
    const values = seen.get(operation) ?? new Map<string, Set<string>>();
    seen.set(operation, values);
    for (const name of Object.keys(dims)) {
      if (LABELLED_DIMENSIONS.has(name)) continue;
      values.set(name, (values.get(name) ?? new Set<string>()).add(dimensionText(name, dims)));
    }
  }
  const varying = (operation: string) =>
    [...(seen.get(operation) ?? new Map<string, Set<string>>())]
      .filter(([, values]) => values.size > 1)
      .map(([name]) => name)
      .sort();

  // The server orders groups alphabetically by dimension; a reader expects the
  // order things happen in: startup first, a terminal's list before its create.
  const rank = (names: Record<string, string>, value: unknown) => {
    const index = Object.keys(names).indexOf(String(value ?? ''));
    return index === -1 ? Number.MAX_SAFE_INTEGER : index;
  };
  const ordered = response.groups
    .map((group, index) => ({ group, index }))
    .sort(
      (a, b) =>
        rank(OPERATION_NAMES, a.group.group?.operation) -
          rank(OPERATION_NAMES, b.group.group?.operation) ||
        rank(REQUEST_KIND_NAMES, a.group.group?.request_kind) -
          rank(REQUEST_KIND_NAMES, b.group.group?.request_kind) ||
        rank(STARTUP_PATH_NAMES, a.group.group?.startup_path) -
          rank(STARTUP_PATH_NAMES, b.group.group?.startup_path) ||
        a.index - b.index,
    );

  return ordered.map(({ group, index }) => {
    const dims = group.group ?? {};
    const operation = String(dims.operation ?? '');
    const detail = varying(operation).map((name) => dimensionText(name, dims));
    const excludedCount = group.sample_count - group.eligible_count;
    const reasons: string[] = [];
    if (group.incomplete_count) {
      reasons.push(`${number(group.incomplete_count)} with collection incomplete`);
    }
    if (group.failed_count) reasons.push(`${number(group.failed_count)} not successful`);
    // The two counts are conditions, not categories: a sample that failed and
    // whose collection did not close is in both, so they are not added up.
    const overlap = reasons.length > 1 ? '; one sample can be both' : '';
    const excludedTitle =
      `${reasons.join(', ')}. The timings use the ${number(group.eligible_count)} that ` +
      `completed with collection closed${overlap}.`;
    // Say the one reason when there is one: "1 failed" reads, "1 not counted"
    // sends the reader to the tooltip.
    const excludedLabel =
      group.failed_count === excludedCount && !group.incomplete_count
        ? `${number(excludedCount)} failed`
        : group.incomplete_count === excludedCount && !group.failed_count
          ? `${number(excludedCount)} incomplete`
          : `${number(excludedCount)} not counted`;
    return {
      key: `${index}:${Object.entries(dims)
        .map(([name, value]) => `${name}=${value}`)
        .join('|')}`,
      label: operationLabel({
        operation,
        request_kind: dims.request_kind === null ? null : String(dims.request_kind ?? ''),
        startup_path: dims.startup_path === null ? null : String(dims.startup_path ?? '') || null,
        coverage_scope: dims.coverage_scope === null ? null : String(dims.coverage_scope ?? ''),
      }),
      detail: detail.length ? detail.join(' · ') : null,
      title: Object.entries(dims)
        .map(([name, value]) => `${name}=${value ?? 'not reported'}`)
        .join('\n'),
      samples: group.sample_count,
      eligible: group.eligible_count,
      excluded:
        excludedCount > 0
          ? { count: excludedCount, label: excludedLabel, title: excludedTitle }
          : null,
      min: duration(group.min_us),
      p50: duration(group.p50_us),
      p95: duration(group.p95_us),
      max: duration(group.max_us),
      few: group.eligible_count < MIN_PERCENTILE_SAMPLES,
    };
  });
}

// ---------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------

/**
 * A measurement run -- one switch-on to switch-off -- as a reader picks it:
 * when it started producing, and where.
 *
 * The catalogue row is created by the first delivered sample, so its creation
 * is when the run began producing, which is what a reader recognizes; the run
 * id stays in the tooltip.
 */
export function runLabel(run: Pick<ProfilingRun, 'created_at' | 'target'>): string {
  const host = run.target?.host;
  return `${shortTime(run.created_at)} · ${host ? host : 'all hosts'}`;
}

// ---------------------------------------------------------------------------
// Selection: the URL
// ---------------------------------------------------------------------------

export interface MeasurementSelection {
  /**
   * The run the page is about; null means the newest one, or every run for a
   * link that names records.
   */
  run: string | null;
  /** The one sample expanded in the list. */
  measurement: string | null;
  operation: string[];
  request_kind: string[];
  coverage_scope: string[];
  outcome: string | null;
  /** null means either state; false means partial only and is significant. */
  collection_complete: boolean | null;
  job_id: string | null;
  thread_id: string | null;
  /**
   * One execution's startup records. Kept as the raw text the URL carries,
   * because an unparseable value is a link this page cannot serve and must
   * say so rather than quietly reading every attempt.
   */
  attempt_id: string | null;
  /** Every request of one logical interaction, such as a single terminal open. */
  interaction_id: string | null;
}

export const EMPTY_SELECTION: MeasurementSelection = {
  run: null,
  measurement: null,
  operation: [],
  request_kind: [],
  coverage_scope: [],
  outcome: null,
  collection_complete: null,
  job_id: null,
  thread_id: null,
  attempt_id: null,
  interaction_id: null,
};

/** The one-value query parameters of the page's own selection. */
const SINGLE_KEYS = [
  'run',
  'measurement',
  'outcome',
  'collection_complete',
  'job_id',
  'thread_id',
  'attempt_id',
  'interaction_id',
] as const;

/** The repeatable ones. A comma-joined value would name one operation that does not exist. */
const REPEATED_KEYS = ['operation', 'request_kind', 'coverage_scope'] as const;

/**
 * Accepted and dropped. The list used to page through the URL; a cursor is a
 * position, not a filter, so an old link that carries one still opens -- at
 * the first page -- rather than failing.
 */
const IGNORED_KEYS = ['after'];

const QUERY_KEYS: readonly string[] = [...SINGLE_KEYS, ...REPEATED_KEYS, ...IGNORED_KEYS];

/**
 * Parse one attempt id, or explain why this page will not read it.
 *
 * The API takes a non-negative integer and this page must not send anything
 * else: an unparseable value dropped on the way out would read every attempt
 * while the address bar still said one.
 */
export function parseAttemptId(value: string): { attemptId: number | null; error: string | null } {
  const trimmed = value.trim();
  if (!trimmed) return { attemptId: null, error: null };
  const parsed = Number(trimmed);
  if (!/^\d+$/.test(trimmed) || !Number.isSafeInteger(parsed)) {
    return {
      attemptId: null,
      error: `This link asks for attempt_id=${trimmed}, which is not an attempt number.`,
    };
  }
  return { attemptId: parsed, error: null };
}

/**
 * Read the page's selection out of its URL.
 *
 * A parameter this page cannot express is an error, not something to ignore:
 * dropping it would answer a wider question than the link asked, and the
 * reader would have no way to see that had happened.
 */
export function selectionFromQuery(query: string): {
  selection: MeasurementSelection;
  error: string | null;
} {
  const params = new URLSearchParams(query);
  const unknown = [...params.keys()].filter((key) => !QUERY_KEYS.includes(key));
  if (unknown.length) {
    return { selection: EMPTY_SELECTION, error: `Unsupported filter: ${unknown[0]}.` };
  }
  const complete = params.get('collection_complete');
  if (complete !== null && complete !== 'true' && complete !== 'false') {
    return {
      selection: EMPTY_SELECTION,
      error: `This link asks for collection_complete=${complete}, which is not true or false.`,
    };
  }
  const attempt = parseAttemptId(params.get('attempt_id') ?? '');
  if (attempt.error) return { selection: EMPTY_SELECTION, error: attempt.error };
  const selection: MeasurementSelection = {
    ...EMPTY_SELECTION,
    run: params.get('run'),
    measurement: params.get('measurement'),
    operation: params.getAll('operation').filter(Boolean),
    request_kind: params.getAll('request_kind').filter(Boolean),
    coverage_scope: params.getAll('coverage_scope').filter(Boolean),
    outcome: params.get('outcome'),
    collection_complete: complete === null ? null : complete === 'true',
    job_id: params.get('job_id'),
    thread_id: params.get('thread_id'),
    attempt_id: params.get('attempt_id'),
    interaction_id: params.get('interaction_id'),
  };
  // An empty string is how a URL spells "absent"; keep it out of the selection
  // so a link with `?run=` cannot become a run named "".
  for (const key of SINGLE_KEYS) {
    if (selection[key] === '') selection[key] = null;
  }
  return { selection, error: null };
}

/** A selection as a query string, in one canonical order, without the `?`. */
export function selectionQuery(selection: MeasurementSelection): string {
  const params = new URLSearchParams();
  for (const key of SINGLE_KEYS) {
    const value = selection[key];
    if (value === null || value === '') continue;
    // `collection_complete` is two states rather than free text, and `false`
    // is a selection: it is written, not dropped as a falsy value.
    params.set(key, typeof value === 'boolean' ? String(value) : value);
  }
  for (const key of REPEATED_KEYS) {
    for (const value of selection[key]) {
      if (value) params.append(key, value);
    }
  }
  return params.toString();
}

/**
 * Serialize a selection into a link.
 *
 * The pathname is passed rather than assumed: a deployment mounted under a
 * base path keeps its prefix, which native history does not add.
 */
export function selectionHref(pathname: string, selection: MeasurementSelection): string {
  const query = selectionQuery(selection);
  return query ? `${pathname}?${query}` : pathname;
}

/**
 * Whether the selection names particular records rather than a run.
 *
 * Such a link -- the turn page's "every record for this attempt" -- is read
 * across every run, because the reader asked for those records wherever they
 * are, not for the newest run's.
 */
export function namesRecords(selection: MeasurementSelection): boolean {
  return Boolean(
    selection.job_id || selection.thread_id || selection.attempt_id || selection.interaction_id,
  );
}

export interface FilterChip {
  /** The selection fields removing this chip clears. */
  clear: Partial<MeasurementSelection>;
  label: string;
}

/**
 * Every filter the operation menu does not show, as a removable chip.
 *
 * These arrive by link -- from the turn page, or from a sample's own "every
 * request of this terminal opening" -- and must stay visible for as long as
 * they narrow the list, or the list would look like the whole run.
 */
export function filterChips(selection: MeasurementSelection): FilterChip[] {
  const chips: FilterChip[] = [];
  if (selection.job_id) chips.push({ clear: { job_id: null }, label: `Turn ${selection.job_id}` });
  if (selection.attempt_id) {
    chips.push({ clear: { attempt_id: null }, label: `Startup #${selection.attempt_id}` });
  }
  if (selection.thread_id) {
    chips.push({ clear: { thread_id: null }, label: `Session ${selection.thread_id}` });
  }
  if (selection.interaction_id) {
    chips.push({
      clear: { interaction_id: null },
      label: `Interaction ${selection.interaction_id}`,
    });
  }
  if (selection.request_kind.length) {
    chips.push({
      clear: { request_kind: [] },
      label: `Request ${selection.request_kind.join(', ')}`,
    });
  }
  if (selection.coverage_scope.length) {
    chips.push({
      clear: { coverage_scope: [] },
      label: `Scope ${selection.coverage_scope.join(', ')}`,
    });
  }
  if (selection.outcome) {
    chips.push({ clear: { outcome: null }, label: `Result ${outcomeLabel(selection.outcome)}` });
  }
  if (selection.collection_complete !== null) {
    chips.push({
      clear: { collection_complete: null },
      label: selection.collection_complete ? 'Fully measured only' : 'Partly measured only',
    });
  }
  return chips;
}

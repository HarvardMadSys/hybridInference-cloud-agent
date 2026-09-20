// Pure presentation for the Admin measurements page.
//
// Separate from `page.tsx` because Next type-checks a page's exports, and
// tested directly rather than only through the rendered tables. Nothing here
// fetches or renders: it decides what a stored number means, which is the part
// that has to stay honest when the page changes.

import type {
  MeasurementDetail,
  MeasurementPage,
  MeasurementRecord,
  MeasurementStage,
  MeasurementSummary,
  MeasurementSummaryGroup,
  MeasurementSummaryResponse,
} from '@/lib/api/adminMeasurements';

import { number } from '../runs/shared';

/** The route this page owns, for links and for writing its own selection back. */
export const MEASUREMENTS_PATH = '/admin/measurements';

/**
 * The notice this whole surface is read under.
 *
 * A server-side duration is not the user's wait, and the difference is not a
 * detail a reader can be left to infer: today there is no browser-side
 * producer, so every stored record stops at the server.
 */
export const SERVER_ONLY_NOTICE =
  'Server-side measurements only. Coverage is either the host startup (runner-rooted: ' +
  'from the runner claiming an attempt to the supervisor dispatching its prompt) or a ' +
  'server request (control-plane-rooted: after authentication, ownership and broker ' +
  'resolution, up to the business result being validated). Response encoding and transfer ' +
  'are not measured. Browser-to-server time is not measured, and end_to_end_complete is ' +
  'false for every record stored so far because ' +
  'no browser-side producer exists. Nothing here is an end-to-end latency.';

/** The minimum eligible sample a percentile needs before it is worth reading. */
export const MIN_PERCENTILE_SAMPLES = 20;

// ---------------------------------------------------------------------------
// Durations
// ---------------------------------------------------------------------------

export interface FormattedDuration {
  /** Rendered value. Never `0 ms` in place of an unreported duration. */
  display: string;
  /** The exact recorded microseconds, for a tooltip: the display rounds, the record does not. */
  exact: string | null;
}

/**
 * Format recorded microseconds.
 *
 * Microseconds are what the producer recorded and are what stay exact; the
 * conversion happens only for the reader. A missing or non-numeric value is
 * `Unavailable` -- an unreported duration is unknown, and a zero would be a
 * claim the record never made.
 */
export function duration(us: number | null | undefined): FormattedDuration {
  if (typeof us !== 'number' || !Number.isFinite(us)) {
    return { display: 'Unavailable', exact: null };
  }
  const exact = `${number(us)} µs`;
  if (Math.abs(us) < 1000) return { display: exact, exact };
  const ms = us / 1000;
  return {
    display: `${ms.toLocaleString('en-US', { maximumFractionDigits: 2 })} ms`,
    exact,
  };
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

// ---------------------------------------------------------------------------
// Stages
// ---------------------------------------------------------------------------

export interface StageRow {
  /** Unique per recorded position: stage names repeat within one record. */
  key: string;
  /** 0 for a local stage, 1 for a duration another producer attached to it. */
  depth: number;
  name: string;
  /** Offset on the record's own timeline; null for an attached child, which has none. */
  startUs: number | null;
  durationUs: number | null;
  /** Share of the record's total; null for an attached child, which is not on that timeline. */
  share: string | null;
  outcome: string | null;
  /** The producer that reported this duration, for an attached child. */
  source: string | null;
  callId: string | null;
  /** The reporter stopped at its own deadline, so this duration is a floor. */
  truncated: boolean;
}

/**
 * Flatten a record's stages for display, in the order they were recorded.
 *
 * An attached child is a duration another producer measured; it is shown under
 * the stage it was attached to and never added to the parent's duration or to
 * the record's total. Two producers on two clocks can overlap, so a sum would
 * be a number no one measured.
 */
export function stageRows(record: Pick<MeasurementRecord, 'stages' | 'duration_us'>): StageRow[] {
  const stages: MeasurementStage[] = record.stages ?? [];
  const rows: StageRow[] = [];
  stages.forEach((stage, index) => {
    rows.push({
      key: `stage-${index}`,
      depth: 0,
      name: stage.stage,
      startUs: stage.start_offset_us ?? null,
      durationUs: stage.duration_us,
      share: shareOf(stage.duration_us, record.duration_us),
      outcome: null,
      source: null,
      callId: null,
      truncated: false,
    });
    (stage.children ?? []).forEach((child, childIndex) => {
      rows.push({
        key: `stage-${index}-child-${childIndex}`,
        depth: 1,
        name: child.stage,
        startUs: null,
        durationUs: child.duration_us,
        share: null,
        outcome: child.outcome ?? null,
        source: child.source ?? null,
        callId: child.call_id ?? null,
        truncated: Boolean(child.truncated),
      });
    });
  });
  return rows;
}

/** What an attached child's duration is and is not, stated where it is shown. */
export const ATTACHED_STAGE_NOTE =
  'Attached from another producer. It carries a duration but no offset on this record’s ' +
  'timeline, and it is not part of the total above.';

/** What a truncated duration is, stated wherever one is shown. */
export const TRUNCATED_STAGE_NOTE =
  'Observed up to the collection deadline: a floor, not the stage’s full duration.';

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

export interface CompletenessLine {
  label: string;
  value: string;
  /** True when this line is the reason a record is not whole. */
  warn: boolean;
}

/**
 * The two completeness questions, never collapsed into one.
 *
 * `collection_complete` is about this producer's own collection closing;
 * `end_to_end_complete` is about a browser being joined to the record. A
 * record can be the first and not the second, which is the state of every
 * record today -- so neither may be rendered as plain "complete".
 */
export function completenessLines(
  record: Pick<MeasurementRecord, 'collection_complete' | 'end_to_end_complete'>,
): CompletenessLine[] {
  return [
    {
      label: 'Collection',
      value: record.collection_complete
        ? 'Complete: every boundary this record expected was answered.'
        : 'Incomplete: at least one expected boundary was never answered, or collection was cut short.',
      warn: !record.collection_complete,
    },
    {
      label: 'End to end',
      value: record.end_to_end_complete
        ? 'The record claims a browser producer was joined to it.'
        : 'Not covered: server-side only, so the time between the browser and the server is not in this duration.',
      warn: !record.end_to_end_complete,
    },
  ];
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
    reasons.push(`Producer(s) that never reported: ${producers.join(', ')}.`);
  }
  if (typeof record.dropped_spans === 'number' && record.dropped_spans > 0) {
    reasons.push(
      `${number(record.dropped_spans)} span(s) were dropped under the record's bound and are not here.`,
    );
  }
  return reasons;
}

/**
 * Whether a list row is anything less than a whole, successful measurement.
 *
 * A summary row carries no truncation flag, so this is deliberately narrower
 * than `partialReasons`: it can say a row is partial, not that it is whole.
 */
export function rowIsPartial(
  row: Pick<MeasurementSummary, 'collection_complete' | 'outcome'>,
): boolean {
  return !row.collection_complete || !isCompleteOutcome(row.outcome);
}

/** What a list row says about its own completeness, without expanding it. */
export function rowCompleteness(
  row: Pick<MeasurementSummary, 'collection_complete' | 'end_to_end_complete'>,
): string {
  const collection = row.collection_complete ? 'Collection closed' : 'Collection incomplete';
  const endToEnd = row.end_to_end_complete ? 'End-to-end claimed' : 'Server-side only';
  return `${collection} · ${endToEnd}`;
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
      'Control-plane-rooted: starts after authentication, ownership and broker resolution and ' +
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
// Empty pages and paging
// ---------------------------------------------------------------------------

/**
 * Why an empty page cannot be read as "nothing was slow".
 *
 * The four causes are named because the page genuinely cannot tell them apart:
 * the data that would distinguish them (was collection on, was this operation
 * selected, did the producer deliver) is not in the response.
 */
export const EMPTY_BASIS =
  'An empty page is not zero latency. The experiment may have collection switched off, this ' +
  'operation may not be selected by it, a delivered sample may have been dropped, or the ' +
  'producer may be an older build that does not report measurements at all. This page cannot ' +
  'tell those apart.';

/** The empty state, using the server's own wording when it sent one. */
export function emptyPageNotice(page: Pick<MeasurementPage, 'empty_reason'>): string {
  const reason = page.empty_reason?.trim().replace(/\.$/, '');
  return reason
    ? `${reason.charAt(0).toUpperCase()}${reason.slice(1)}. ${EMPTY_BASIS}`
    : EMPTY_BASIS;
}

/**
 * What one page of the list supports, and what only the summary does.
 *
 * The list route returns no total, so this cannot say how many samples the
 * experiment holds -- only that the page is not it.
 */
export function listNotice(page: MeasurementPage): string {
  if (page.count === 0) return emptyPageNotice(page);
  const more = page.next_cursor
    ? ' More rows follow, so this is not the end of the list either.'
    : ' This is the last page of this selection.';
  return (
    `${number(page.count)} of at most ${number(page.limit)} rows, newest first. A page is not ` +
    'the population: counts and percentiles over it would describe the page, so the experiment ' +
    `summary below is what aggregates in SQL over every stored sample.${more}`
  );
}

// ---------------------------------------------------------------------------
// Distribution
// ---------------------------------------------------------------------------

export interface GroupRow {
  key: string;
  /** The dimensions the server folded by, in the order it named them. */
  dimensions: { name: string; value: string }[];
  sampleCount: number;
  interactionCount: number;
  eligibleCount: number;
  p50: FormattedDuration;
  p95: FormattedDuration;
  min: FormattedDuration;
  max: FormattedDuration;
  /** Fewer eligible samples than a percentile needs to be read as a distribution. */
  insufficient: boolean;
  /** What was stored but kept out of the percentiles, and why. */
  excluded: string | null;
}

/** A dimension value, with the two ways one can be absent said out loud. */
export function dimensionValue(name: string, value: string | number | null): string {
  if (value === null || value === '') {
    return name === 'startup_path' ? 'Not a startup' : 'Not reported';
  }
  if (name === 'startup_path' && value === 'unknown') return 'Unclassified';
  return String(value);
}

/** What the percentiles in a group were computed over, and what was left out. */
export function groupBasis(group: MeasurementSummaryGroup): string {
  const excluded: string[] = [];
  if (group.incomplete_count) {
    excluded.push(`${number(group.incomplete_count)} with collection incomplete`);
  }
  if (group.failed_count) excluded.push(`${number(group.failed_count)} not successful`);
  const stored = `${number(group.sample_count)} stored sample(s), ${number(
    group.interaction_count,
  )} interaction(s), ${number(group.eligible_count)} eligible for the percentiles`;
  if (!excluded.length) return `${stored}; nothing was excluded.`;
  // The two counts are conditions, not categories: a sample whose collection
  // did not close *and* whose outcome failed is in both, so their sum is an
  // upper bound on the exclusion rather than a partition of the stored rows.
  // Saying so here is what keeps a reader from adding them up.
  return `${stored}; excluded: ${excluded.join(', ')}. A sample can be both, so these may overlap.`;
}

/** The caution a small sample needs before anyone reads a percentile into it. */
export function sampleCaution(eligibleCount: number): string | null {
  if (eligibleCount >= MIN_PERCENTILE_SAMPLES) return null;
  return `Fewer than ${number(
    MIN_PERCENTILE_SAMPLES,
  )} eligible samples: read the percentiles as an indication, not as a distribution to optimize against.`;
}

/**
 * Fold the summary response into rows.
 *
 * The grouping dimensions are taken from the response's own `group` object, so
 * a dimension this page has never heard of is still shown rather than dropped.
 * An absent percentile stays `Unavailable` -- with no eligible sample there is
 * no distribution, and a zero would be a fabricated measurement.
 */
export function groupRows(response: MeasurementSummaryResponse): GroupRow[] {
  return response.groups.map((group, index) => {
    const dimensions = Object.entries(group.group ?? {}).map(([name, value]) => ({
      name,
      value: dimensionValue(name, value),
    }));
    return {
      key: `${index}:${dimensions.map((dimension) => `${dimension.name}=${dimension.value}`).join('|')}`,
      dimensions,
      sampleCount: group.sample_count,
      interactionCount: group.interaction_count,
      eligibleCount: group.eligible_count,
      p50: duration(group.p50_us),
      p95: duration(group.p95_us),
      min: duration(group.min_us),
      max: duration(group.max_us),
      insufficient: group.eligible_count < MIN_PERCENTILE_SAMPLES,
      excluded: groupBasis(group),
    };
  });
}

/** The sentence the distribution table may never be read without. */
export const DISTRIBUTION_BASIS =
  'Computed in SQL over the whole experiment, not over the page above. Percentiles include ' +
  'only samples whose collection closed and whose outcome was completed or dispatched; each ' +
  'group states what was stored and excluded. A percentile is not an end-to-end latency.';

// ---------------------------------------------------------------------------
// Selection: the URL, links, and the filter form
// ---------------------------------------------------------------------------

export interface MeasurementSelection {
  /** The experiment the list is scoped to; null reads across experiments. */
  run: string | null;
  /** The one record expanded below the list. */
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
   * One execution's startup records. Kept as the raw text the URL and the form
   * carry, because an unparseable value is a link this page cannot serve and
   * must say so rather than quietly reading every attempt.
   */
  attempt_id: string | null;
  /** Every request of one logical interaction, such as a single terminal open. */
  interaction_id: string | null;
  /** The opaque cursor. Never carried across a filter change. */
  after: string | null;
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
  after: null,
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
  'after',
] as const;

/** The repeatable ones. A comma-joined value would name one operation that does not exist. */
const REPEATED_KEYS = ['operation', 'request_kind', 'coverage_scope'] as const;

const QUERY_KEYS: readonly string[] = [...SINGLE_KEYS, ...REPEATED_KEYS];

/** The filter fields the form submits. */
export const FORM_KEYS = [
  'operation',
  'request_kind',
  'coverage_scope',
  'outcome',
  'collection_complete',
  'job_id',
  'thread_id',
  'attempt_id',
  'interaction_id',
] as const;

export type FormKey = (typeof FORM_KEYS)[number];

/**
 * Parse one attempt id, or explain why this page will not read it.
 *
 * The API takes a non-negative integer and this page must not send anything
 * else: an unparseable value dropped on the way out would read the whole
 * experiment while the address bar still said one attempt.
 */
export function parseAttemptId(value: string): { attemptId: number | null; error: string | null } {
  const trimmed = value.trim();
  if (!trimmed) return { attemptId: null, error: null };
  if (!/^\d+$/.test(trimmed)) {
    return {
      attemptId: null,
      error: `This link asks for attempt_id=${trimmed}, which is not an attempt number.`,
    };
  }
  const parsed = Number(trimmed);
  if (!Number.isSafeInteger(parsed)) {
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
    return { selection: EMPTY_SELECTION, error: `Unsupported filter: ${unknown[0]}` };
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
    // Stored as the text the URL carries; `listMeasurements` parses it once,
    // through the same function, so the page and the query cannot disagree.
    attempt_id: params.get('attempt_id'),
    interaction_id: params.get('interaction_id'),
    after: params.get('after'),
  };
  // An empty string is how a URL spells "absent"; keep it out of the selection
  // so a link with `?run=` cannot become a run named "".
  for (const key of SINGLE_KEYS) {
    if (selection[key] === '') selection[key] = null;
  }
  return { selection, error: null };
}

/**
 * Serialize a selection into a link, or into this page's own address bar.
 *
 * The pathname is passed rather than assumed: a deployment mounted under a
 * base path keeps its prefix, which native history does not add.
 */
export function selectionHref(pathname: string, selection: MeasurementSelection): string {
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
  const query = params.toString();
  return query ? `${pathname}?${query}` : pathname;
}

/** Split a filter field into the values it names, without duplicates. */
export function splitValues(value: string): string[] {
  return [
    ...new Set(
      value
        .split(/[\s,]+/)
        .map((item) => item.trim())
        .filter(Boolean),
    ),
  ];
}

/**
 * The selection an applied filter form asks for.
 *
 * The form is the authority: a cleared field disappears rather than keeping
 * its previous value. The cursor is dropped because it is a position in one
 * ordering -- resuming it under different filters would splice two different
 * selections into one list. The expanded record is dropped for the same
 * reason: it belongs to the selection that listed it.
 */
export function selectionFromForm(
  read: (key: FormKey) => string,
  run: string | null,
): MeasurementSelection {
  const complete = read('collection_complete');
  return {
    ...EMPTY_SELECTION,
    run,
    operation: splitValues(read('operation')),
    request_kind: splitValues(read('request_kind')),
    coverage_scope: splitValues(read('coverage_scope')),
    outcome: read('outcome').trim() || null,
    collection_complete: complete === 'true' ? true : complete === 'false' ? false : null,
    job_id: read('job_id').trim() || null,
    thread_id: read('thread_id').trim() || null,
    attempt_id: read('attempt_id').trim() || null,
    interaction_id: read('interaction_id').trim() || null,
  };
}

/** The selection as one line, so a reader can see what the list was filtered by. */
export function selectionSummary(selection: MeasurementSelection): string {
  const parts: string[] = [];
  if (selection.run) parts.push(`experiment ${selection.run}`);
  else parts.push('all experiments');
  if (selection.operation.length) parts.push(`operation in ${selection.operation.join(', ')}`);
  if (selection.request_kind.length)
    parts.push(`request kind in ${selection.request_kind.join(', ')}`);
  if (selection.coverage_scope.length)
    parts.push(`scope in ${selection.coverage_scope.join(', ')}`);
  if (selection.outcome) parts.push(`outcome ${selection.outcome}`);
  if (selection.collection_complete !== null) {
    parts.push(selection.collection_complete ? 'collection closed' : 'collection incomplete only');
  }
  if (selection.job_id) parts.push(`job ${selection.job_id}`);
  if (selection.thread_id) parts.push(`thread ${selection.thread_id}`);
  if (selection.attempt_id) parts.push(`attempt ${selection.attempt_id}`);
  if (selection.interaction_id) parts.push(`interaction ${selection.interaction_id}`);
  // An attempt and an interaction select different records, so a selection
  // carrying both is stated as the intersection it is rather than as one key.
  if (selection.attempt_id && selection.interaction_id) {
    parts.push('both, so only a record carrying each is listed');
  }
  return `Showing ${parts.join(' · ')}.`;
}

/** Whether a selection asks for something the wide-open default does not. */
export function isFiltered(selection: MeasurementSelection): boolean {
  return (
    selection.operation.length > 0 ||
    selection.request_kind.length > 0 ||
    selection.coverage_scope.length > 0 ||
    selection.outcome !== null ||
    selection.collection_complete !== null ||
    selection.job_id !== null ||
    selection.thread_id !== null ||
    selection.attempt_id !== null ||
    selection.interaction_id !== null
  );
}

// ---------------------------------------------------------------------------
// Record detail odds and ends
// ---------------------------------------------------------------------------

/** The record's facts as displayable pairs, sorted so two reads agree. */
export function factLines(
  facts: Record<string, unknown> | undefined,
): { name: string; value: string }[] {
  if (!facts) return [];
  return Object.entries(facts)
    .map(([name, value]) => ({ name, value: factValue(value) }))
    .sort((left, right) => left.name.localeCompare(right.name));
}

/** A fact value, with a nested value shown rather than flattened into "[object]". */
export function factValue(value: unknown): string {
  if (value === null || value === undefined) return 'Not reported';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  try {
    return JSON.stringify(value) ?? 'Not reported';
  } catch {
    // A fact that cannot be serialized is still a fact; say so rather than
    // throwing away the row that held it.
    return 'Unrenderable value';
  }
}

/** The run's target selectors as displayable pairs. */
export function targetLines(
  target: Record<string, string> | undefined,
): { name: string; value: string }[] {
  if (!target) return [];
  return Object.entries(target).map(([name, value]) => ({ name, value: value || 'Not reported' }));
}

/** How many records a run's summary was folded from, across every group. */
export function summaryTotals(response: MeasurementSummaryResponse): {
  samples: number;
  eligible: number;
  incomplete: number;
  failed: number;
  interactions: number;
} {
  return response.groups.reduce(
    (total, group) => ({
      samples: total.samples + group.sample_count,
      eligible: total.eligible + group.eligible_count,
      incomplete: total.incomplete + group.incomplete_count,
      failed: total.failed + group.failed_count,
      // Distinct interactions are not additive across groups -- one interaction
      // can appear in several -- so the largest group's count is reported as a
      // floor rather than a total this page cannot compute.
      interactions: Math.max(total.interactions, group.interaction_count),
    }),
    { samples: 0, eligible: 0, incomplete: 0, failed: 0, interactions: 0 },
  );
}

/** The one-line header for a detail read, naming what it is and is not. */
export function detailHeading(
  detail: Pick<MeasurementDetail, 'operation' | 'request_kind'>,
): string {
  return detail.request_kind && detail.request_kind !== detail.operation
    ? `${detail.operation} · ${detail.request_kind}`
    : detail.operation;
}

// Admin reads of stored platform operation measurements.
//
// Four routes, one per question the admin surface answers: which experiments
// were catalogued, what one page of their samples says, what a single frozen
// record says, and what the experiment as a whole distributes like. The fifth
// question -- why an experiment has no samples -- is deliberately left
// unanswered here, and the empty page carries the server's own wording for it
// rather than a client guess.
//
// `no-store` is not a default that can be dropped: the control plane computes
// ages and completeness from a clock, so a cached page would describe a moment
// nobody chose.

import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from './client';

const RUNS = '/admin/agent/profiling-runs';
const MEASUREMENTS = '/admin/agent/measurements';
const WRITER = '/admin/agent/measurement-writer';

/**
 * The dimensions the summary groups by.
 *
 * The server cannot be talked out of the ones that decide comparability --
 * operation, request kind, boundary version and coverage scope are always in
 * the result, and a startup path is added whenever the samples are startups --
 * so naming one here only asks for it explicitly. A name outside this set is
 * refused by the server rather than silently broadened, which is why the page
 * never invents one.
 */
export type MeasurementGroupBy =
  | 'operation'
  | 'request_kind'
  | 'boundary_version'
  | 'coverage_scope'
  | 'startup_path'
  | 'runtime_id'
  | 'runtime_version'
  | 'execution_profile';

/** One admission instance of an experiment: the budget one replica enforced. */
export interface ProfilingAdmissionInstance {
  instance?: string;
  [key: string]: unknown;
}

/** One experiment catalogue entry, as the delivery that created it recorded it. */
export interface ProfilingRun {
  run_id: string;
  /** The operations this experiment selects. */
  operations: string[];
  /** The target selectors the manifest scoped itself to. */
  target: Record<string, string>;
  /** The configured absolute expiry, not the deadline a producer has left. */
  expires_at: string;
  config_version: number;
  admission_instances: ProfilingAdmissionInstance[];
  /** When the row was written, and when it was last received. */
  created_at: string;
  received_at: string;
}

export interface ProfilingRunCatalogue {
  runs: ProfilingRun[];
  /** How many runs this page holds; the catalogue is bounded by `limit`. */
  count: number;
  limit: number;
}

/**
 * One stored measurement's summary columns.
 *
 * The frozen record is deliberately absent: a list needs none of it, and the
 * detail read is the only one that carries it.
 */
export interface MeasurementSummary {
  measurement_id: string;
  interaction_id: string;
  profiling_run_id: string;
  schema_version: number;
  owner_id: string | null;
  job_id: string | null;
  thread_id: string | null;
  sandbox_id: string | null;
  host_id: string | null;
  attempt_id: number | null;
  lease_generation: number | null;
  operation: string;
  request_kind: string;
  boundary_version: number;
  /** `server_request` or `host_startup` today; a new producer may add another. */
  coverage_scope: string;
  producer: string;
  producer_instance_id: string;
  duration_us: number;
  outcome: string;
  /** Whether every boundary this record expected closed. */
  collection_complete: boolean;
  /** Whether a browser-side producer was joined to this record. Always false today. */
  end_to_end_complete: boolean;
  /** One of the four startup paths, or `unknown` when the actions did not say. */
  startup_path: string | null;
  runtime_id: string | null;
  runtime_version: string | null;
  execution_profile: string | null;
  observed_at: string;
  received_at: string;
}

/** A mark on the local timeline, named by whoever produced it. */
export interface MeasurementMilestone {
  name: string;
  offset_us: number;
  source: string;
}

/**
 * A duration another producer measured and attached to a local stage.
 *
 * It carries no offset: a different process has a different monotonic clock,
 * so only the duration travels. A child can overlap or outlast its parent, and
 * the two are never added together into a total.
 */
export interface MeasurementChildStage {
  stage: string;
  duration_us: number;
  outcome?: string;
  source?: string;
  call_id?: string;
  /** True when the producer stopped at its own deadline: a floor, not a duration. */
  truncated?: boolean;
}

/** One local stage, with the remote durations attached to it. */
export interface MeasurementStage {
  stage: string;
  start_offset_us?: number;
  duration_us: number;
  children?: MeasurementChildStage[];
}

export interface MeasurementSpan {
  span_id: string;
  name: string;
  ordinal: number;
  start_offset_us: number;
  outcome: string;
  parent_span_id?: string;
  duration_us?: number;
  truncated?: boolean;
  attributes?: Record<string, unknown>;
}

/**
 * The frozen record a measurement was stored from.
 *
 * Every field below the identity block is optional on purpose: the record is
 * frozen producer output, and a field a producer did not send is *absent*, not
 * zero. The page must not fill one in.
 */
export interface MeasurementRecord {
  schema_version: number;
  measurement_id: string;
  interaction_id: string;
  profiling_run_id: string;
  operation: string;
  boundary_version: number;
  coverage_scope: string;
  start_boundary?: string;
  end_boundary?: string;
  /** Stated by the record itself; false wherever no browser producer was joined. */
  end_to_end_complete: boolean;
  producer: string;
  producer_instance_id: string;
  clock_domain_id?: string;
  /** Who contributed to this record, and who among them timed a stage. */
  producers?: string[];
  timing_sources?: string[];
  outcome: string;
  duration_us: number;
  collection_complete: boolean;
  /** The v2 name for the same judgement; not a second, stronger claim. */
  measurement_complete?: boolean;
  boundary_frozen?: boolean;
  dropped_spans?: number;
  truncated?: boolean;
  truncated_fragments?: string[];
  missing_fragments?: string[];
  missing_producers?: string[];
  failure_stage?: string;
  startup_path?: string | null;
  run_expires_at?: string;
  observed_at: string;
  spans?: MeasurementSpan[];
  milestones?: MeasurementMilestone[];
  stages?: MeasurementStage[];
  facts?: Record<string, unknown>;
  /** The runner's alias for `outcome`, written only for its own startup operation. */
  startup_outcome?: string;
}

/** One measurement with the record it was frozen from. */
export interface MeasurementDetail extends MeasurementSummary {
  record: MeasurementRecord;
  payload_hash: string;
}

/**
 * One distribution group, computed in SQL over the whole experiment.
 *
 * The grouping dimensions are re-read from `group`, so a server that starts
 * returning a dimension this page has never heard of still shows it rather
 * than dropping it.
 */
export interface MeasurementSummaryGroup {
  /** The dimensions this group was folded by, exactly as the server named them. */
  group: Record<string, string | number | null>;
  sample_count: number;
  /** Distinct interactions, which is not the same as the number of samples. */
  interaction_count: number;
  /** Samples the percentiles were computed from. */
  eligible_count: number;
  /** Stored samples excluded from the distribution because collection did not close. */
  incomplete_count: number;
  /** Stored samples excluded because their outcome was not a success. */
  failed_count: number;
  /** Null when no sample was eligible: an unknown percentile, never a zero. */
  p50_us: number | null;
  p95_us: number | null;
  min_us: number | null;
  max_us: number | null;
}

export interface MeasurementSummaryResponse {
  run_id: string;
  groups: MeasurementSummaryGroup[];
}

export interface MeasurementWriterStatus {
  scope: 'this_control_plane_process';
  running: boolean;
  pending: number;
  in_flight: number;
  queued_bytes: number;
  counts: {
    stored: number;
    duplicate: number;
    rejected: number;
    conflict: number;
    dropped_full: number;
    dropped_timeout: number;
    dropped_shutdown: number;
    failed: number;
    reasons: Record<string, number>;
  };
}

/** One page of measurement summaries, newest first. */
export interface MeasurementPage {
  measurements: MeasurementSummary[];
  /** Rows on this page. Not the experiment's sample count. */
  count: number;
  limit: number;
  /** The id to continue after, or null when this page is the last one. */
  next_cursor: string | null;
  /** Why the server cannot say why this page is empty. Present only when empty. */
  empty_reason?: string;
}

export interface MeasurementFilters {
  run_id?: string;
  /** Repeatable: a sample matches when its operation is any of these. */
  operation?: string[];
  request_kind?: string[];
  coverage_scope?: string[];
  outcome?: string;
  /** `false` means "partial only" and is sent, not dropped as an empty value. */
  collection_complete?: boolean;
  job_id?: string;
  thread_id?: string;
  /**
   * One execution's startup samples. Only `attempt.startup` records carry an
   * attempt, so this selects a startup and never a file or terminal request.
   */
  attempt_id?: number;
  /**
   * Every request of one logical interaction -- the list, create and
   * stream-attach of a single terminal open. The key to read an operation that
   * has no attempt at all.
   */
  interaction_id?: string;
  /** Opaque cursor: the measurement id to continue after. */
  after?: string;
  limit?: number;
}

function appendRepeated(params: URLSearchParams, key: string, values: string[] | undefined): void {
  for (const value of values ?? []) {
    if (value) params.append(key, value);
  }
}

export async function listProfilingRuns(
  filters: { since?: string; limit?: number } = {},
  signal?: AbortSignal,
): Promise<ProfilingRunCatalogue> {
  const params = new URLSearchParams();
  if (filters.since) params.set('since', filters.since);
  if (filters.limit !== undefined) params.set('limit', String(filters.limit));
  const query = params.size ? `?${params}` : '';
  return jsonOrThrow<ProfilingRunCatalogue>(
    await fetchWithAuth(config.apiBase, `${RUNS}${query}`, { signal, cache: 'no-store' }),
  );
}

export async function summarizeProfilingRun(
  runId: string,
  groupBy: MeasurementGroupBy[] = [],
  signal?: AbortSignal,
): Promise<MeasurementSummaryResponse> {
  const params = new URLSearchParams();
  appendRepeated(params, 'group_by', groupBy);
  const query = params.size ? `?${params}` : '';
  return jsonOrThrow<MeasurementSummaryResponse>(
    await fetchWithAuth(config.apiBase, `${RUNS}/${encodeURIComponent(runId)}/summary${query}`, {
      signal,
      cache: 'no-store',
    }),
  );
}

export async function listMeasurements(
  filters: MeasurementFilters,
  signal?: AbortSignal,
): Promise<MeasurementPage> {
  const params = new URLSearchParams();
  if (filters.run_id) params.set('run_id', filters.run_id);
  appendRepeated(params, 'operation', filters.operation);
  appendRepeated(params, 'request_kind', filters.request_kind);
  appendRepeated(params, 'coverage_scope', filters.coverage_scope);
  if (filters.outcome) params.set('outcome', filters.outcome);
  // A boolean filter is two states. Only `undefined` means "either".
  if (filters.collection_complete !== undefined) {
    params.set('collection_complete', String(filters.collection_complete));
  }
  if (filters.job_id) params.set('job_id', filters.job_id);
  if (filters.thread_id) params.set('thread_id', filters.thread_id);
  // Attempt 0 is not a real attempt, but the number is sent whenever it is
  // defined so `undefined` stays the only "either" state.
  if (filters.attempt_id !== undefined) params.set('attempt_id', String(filters.attempt_id));
  if (filters.interaction_id) params.set('interaction_id', filters.interaction_id);
  if (filters.after) params.set('after', filters.after);
  if (filters.limit !== undefined) params.set('limit', String(filters.limit));
  const query = params.size ? `?${params}` : '';
  return jsonOrThrow<MeasurementPage>(
    await fetchWithAuth(config.apiBase, `${MEASUREMENTS}${query}`, { signal, cache: 'no-store' }),
  );
}

export async function getMeasurement(
  measurementId: string,
  signal?: AbortSignal,
): Promise<MeasurementDetail> {
  return jsonOrThrow<MeasurementDetail>(
    await fetchWithAuth(config.apiBase, `${MEASUREMENTS}/${encodeURIComponent(measurementId)}`, {
      signal,
      cache: 'no-store',
    }),
  );
}

export async function getMeasurementWriterStatus(
  signal?: AbortSignal,
): Promise<MeasurementWriterStatus> {
  return jsonOrThrow<MeasurementWriterStatus>(
    await fetchWithAuth(config.apiBase, WRITER, { signal, cache: 'no-store' }),
  );
}

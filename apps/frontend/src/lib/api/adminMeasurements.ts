// Admin reads of stored platform operation measurements.
//
// Four routes, one per question the admin surface answers: which experiments
// were catalogued, what one page of their samples says, what a single frozen
// record says, and what the experiment as a whole distributes like. The fifth
// question -- why an experiment has no samples -- is deliberately left
// unanswered here, and the empty page carries the server's own wording for it
// rather than a client guess. The runtime-control routes at the end of the file
// are the only writes on this surface: they answer whether anything is being
// collected at all, which is a different question from what was collected.
//
// `no-store` is not a default that can be dropped: the control plane computes
// ages and completeness from a clock, so a cached page would describe a moment
// nobody chose.

import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from './client';

const RUNS = '/admin/agent/profiling-runs';
const MEASUREMENTS = '/admin/agent/measurements';
const WRITER = '/admin/agent/measurement-writer';
const CONTROL = '/admin/agent/profiling-control';

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

// ---------------------------------------------------------------------------
// Runtime control of collection
// ---------------------------------------------------------------------------
//
// The reads above describe what was collected. These describe whether anything
// is being collected at all, and are the only writes on this surface.
//
// A write carries the revision the admin was looking at, so a command built
// from a stale page is refused rather than applied to a run nobody chose, and a
// client-generated request id, so a retry after a lost response is the same
// command instead of a second run. Both travel in the body; neither is
// recoverable from the response of the request that failed.

/**
 * What the deployment's *configuration* says it is doing.
 *
 * Four values, and they are not the same fact: `off` is a state somebody chose,
 * `on` is an experiment in force, and the other two are statements about
 * whether this deployment can be commanded from the Admin API at all.
 * `unavailable` covers both "profiling is disabled" and "there is no control
 * row", and `unmanaged` means the deployment's own configuration decides.
 * Neither of those is off, and a page that rendered one as off would offer a
 * switch that does nothing.
 */
export type ProfilingControlStatus = 'off' | 'on' | 'unavailable' | 'unmanaged';

/**
 * Which of the three questions an operator is asking.
 *
 * The grouping is this client's, not the wire's: the server serves root
 * operation names and each group is the set of names one checkbox stands for.
 * The names behind a group are read from the server rather than hard-coded
 * here, because what a deployment can select is exactly what it must be asked
 * for.
 */
export type MeasurementOperationGroup = 'sandbox_startup' | 'terminal_open' | 'file_access';

export interface ProfilingControlRun {
  run_id: string;
  /** The root operation names this run selects. */
  operations: string[];
  target: { host?: string };
  expires_at: string;
  started_at: string | null;
  stopped_at: string | null;
  stop_reason: string | null;
  lifecycle: string | null;
  source: string | null;
  created_by: string | null;
  control_revision: number | null;
  /**
   * What the authority's clock had left when it answered. A countdown between
   * polls is measured from this, never from `expires_at`, which is a wall-clock
   * instant two clocks would have to agree on.
   */
  seconds_remaining: number;
}

/**
 * One deployment's control state, as the server reads it at one instant.
 *
 * Everything here describes the configuration: what the deployment is
 * currently set to collect, until when, and what that configuration has stored.
 * It says nothing about which processes have applied it -- sync lags by design,
 * and `sync_note` states the lag instead of pretending to have measured it.
 */
export interface ProfilingControlSnapshot {
  /** Which deployment this control row belongs to; never chosen by a client. */
  deployment: string;
  /**
   * Who may control this deployment.
   *
   * `admin` is the only mode in which a command from this page means anything:
   * a deployment configured by its environment answers `unmanaged`, and one
   * with profiling disabled answers `unavailable`. Both still answer, rather
   * than 404, so the card can say why it offers no switch.
   */
  mode: 'disabled' | 'environment' | 'admin';
  /** The control revision a write must echo back as `expected_revision`. */
  revision: number;
  /** False when this deployment has no Admin control state to command. */
  configured: boolean;
  /** ISO-8601 UTC, by the server's clock. */
  server_time: string;
  status: ProfilingControlStatus;
  /**
   * The experiment **in force**, or null when the deployment is not collecting.
   *
   * A stopped experiment is reported in `last_stopped_run` rather than here, so
   * a reader can never mistake a tombstone for a live command.
   */
  run: ProfilingControlRun | null;
  /**
   * Whether `run` is an experiment that is actually collecting.
   *
   * Stated by the server instead of inferred from `run !== null` because the
   * two are separate questions, and the one wrong answer would keep a page
   * offering a Stop for an experiment that already ended.
   */
  run_active: boolean;
  /**
   * The experiment the most recent Stop ended, for the status text.
   *
   * Context only: never a run to stop, and cleared by the next Start.
   */
  last_stopped_run: ProfilingControlRun | null;
  /** Real stored rows for the active run; 0 when there is no active run. */
  stored_samples: number;
  budget_scope: 'producer_instance';
  /** The server's own statement of what the bound does and does not cover. */
  budget_note: string;
  /**
   * How long a change takes to reach every process, in the server's words.
   *
   * Rendered as written: a card that implied instant agreement would be
   * describing a consensus it never measured.
   */
  sync_note: string;
  operations: {
    /** The root operation names each group selects, as this deployment offers them. */
    selectable: {
      sandbox_startup: string[];
      terminal_open: string[];
      file_access: string[];
    };
    all: string[];
  };
  bounds: {
    min_duration_seconds: number;
    max_duration_seconds: number;
    default_max_operations: number;
    hard_max_operations: number;
  };
}

export interface StartMeasurementRequest {
  /**
   * Client-generated and stable across retries of the same command, so a
   * response lost in transit does not create a second run.
   */
  request_id: string;
  /** The revision the admin was looking at, not necessarily the current one. */
  expected_revision: number;
  operations: string[];
  /** `null` means every host. */
  target: { host: string | null };
  duration_seconds: number;
}

export interface StartMeasurementResponse {
  /** The control state the command produced; the page renders this, not a guess. */
  control: ProfilingControlSnapshot;
  run_id: string;
  revision: number;
}

export interface StopMeasurementResponse {
  control: ProfilingControlSnapshot;
  run_id: string;
  revision: number;
}

export async function getProfilingControl(signal?: AbortSignal): Promise<ProfilingControlSnapshot> {
  return jsonOrThrow<ProfilingControlSnapshot>(
    await fetchWithAuth(config.apiBase, CONTROL, { signal, cache: 'no-store' }),
  );
}

export async function startProfilingRun(
  body: StartMeasurementRequest,
  signal?: AbortSignal,
): Promise<StartMeasurementResponse> {
  return jsonOrThrow<StartMeasurementResponse>(
    await fetchWithAuth(config.apiBase, RUNS, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
      cache: 'no-store',
    }),
  );
}

export async function stopProfilingRun(
  runId: string,
  body: { request_id: string },
  signal?: AbortSignal,
): Promise<StopMeasurementResponse> {
  return jsonOrThrow<StopMeasurementResponse>(
    await fetchWithAuth(config.apiBase, `${RUNS}/${encodeURIComponent(runId)}/stop`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
      cache: 'no-store',
    }),
  );
}

/**
 * The root operation names the checked groups stand for.
 *
 * The mapping is read from the snapshot, so a deployment that offers nothing
 * for a group is asked for nothing; the caller's order is kept and a name two
 * groups share is sent once. An empty result is a real answer -- there is
 * nothing this deployment can collect for that selection -- and a caller that
 * posts it would be refused, which is why the page refuses first.
 */
export function measurementOperationsFor(
  groups: MeasurementOperationGroup[],
  snapshot: ProfilingControlSnapshot,
): string[] {
  const operations: string[] = [];
  for (const group of groups) {
    for (const operation of snapshot.operations.selectable[group] ?? []) {
      if (!operations.includes(operation)) operations.push(operation);
    }
  }
  return operations;
}

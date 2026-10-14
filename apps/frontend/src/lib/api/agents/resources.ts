import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

/** One tool call's timing and attributable resource evidence. */
export interface ToolCallResourcesApi {
  call_seq: number;
  tool_use_id: string;
  tool_name: string;
  /** `exact` = the sandbox's own hooks; `message` = bracketed from outside. */
  timing_source: string;
  duration_ms: number | null;
  /** Historical sampled readings; current shared-member rows always carry 0. */
  sample_count: number;
  /** Calls that shared these readings, including this one. */
  overlap_n: number;
  measured_span_ms: number | null;
  cpu_usec_exclusive: number | null;
  cpu_usec_shared: number | null;
  mem_peak_exclusive_bytes: number | null;
  mem_peak_shared_bytes: number | null;
  /** Guest-kernel process starts plausibly belonging to this call. */
  process_count?: number;
  /**
   * What the processes this call alone started consumed, from the guest
   * kernel's exit-time accounting. Sums for CPU and I/O, the largest single
   * process for the peak. Null where nothing exited that belonged to this
   * call alone, or where the kernel does not account that counter.
   */
  process_cpu_usec?: number | null;
  process_mem_peak_bytes?: number | null;
  process_io_read_bytes?: number | null;
  process_io_write_bytes?: number | null;
  /** A process this call may own was left out, so the numbers are floors. */
  process_resources_partial?: boolean;
}

export interface ProcessObservationApi {
  process_seq: number;
  uid: number;
  pid: number;
  tgid: number;
  ppid: number | null;
  comm: string;
  executable: string | null;
  started_offset_ms: number;
  duration_ms: number | null;
  exit_code: number | null;
  observed_fork: boolean;
  observed_exec: boolean;
  observed_exit: boolean;
  tool_call_seqs: number[];
  /** Exit-time accounting for the Linux process group; null until it exited. */
  cpu_usec?: number | null;
  mem_peak_bytes?: number | null;
  io_read_bytes?: number | null;
  io_write_bytes?: number | null;
  io_rchar_bytes?: number | null;
  io_wchar_bytes?: number | null;
}

export interface AttemptResourcesApi {
  cpu_usec: number | null;
  mem_peak_bytes: number | null;
  mem_avg_bytes: number | null;
  io_read_bytes: number | null;
  io_write_bytes: number | null;
  net_rx_bytes: number | null;
  net_tx_bytes: number | null;
  sample_count: number;
  /** Fraction of the attempt window actually sampled, 0..1. */
  coverage: number;
  sampler_version: string | null;
  /** Union of the turn's tool windows, never their sum. */
  tool_time_ms?: number | null;
  tool_calls?: ToolCallResourcesApi[];
  tool_calls_truncated?: boolean;
  process_observer?: string | null;
  process_observation_complete?: boolean;
  process_events_lost?: number;
  processes_truncated?: boolean;
  processes?: ProcessObservationApi[];
  /** The whole-turn member execution window, or null when none was collected. */
  turn_summary?: TurnResourceSummaryApi | null;
}

/**
 * One metric's completeness, stated per metric rather than as a whole-turn
 * percentage. `available` means complete within its declared method and scope:
 * it never claims zero sampling error or that the number is the platform's
 * whole cost. `partial` is a measured lower bound for an additive quantity.
 * `unavailable` carries no number at all.
 */
export type ResourceMetricStatus = 'available' | 'partial' | 'unavailable';

/**
 * Every way a `member_execution_window` measurement can be incomplete.
 *
 * A bounded enum mirrored from the control plane, not free text: the admin UI
 * maps each code to one fixed sentence, so no UID, lease token, kernel field
 * name or exception string can reach a page through this field. Unknown codes
 * fall back to one generic sentence rather than being rendered.
 */
export type TurnResourceReasonCode =
  | 'missing_thread_baseline'
  | 'counter_regressed'
  | 'thread_identity_conflict'
  | 'observer_restarted'
  | 'ring_loss'
  | 'filesystem_attribution_unverified'
  | 'pss_unreadable'
  | 'address_space_share_unverified'
  | 'sampling_over_budget'
  | 'window_prefix_retained'
  | 'observer_unavailable'
  | 'not_a_kata_guest'
  | 'source_mismatch';

/** Reported before the sandbox has closed the window, and final once it has. */
export type TurnResourceState = 'collecting' | 'final';

/** Where a measurement actually reached, on the guest's monotonic clock. */
export interface TurnResourceWindowApi {
  /** Guest-monotonic open-to-close span. */
  duration_ms: number;
  /** How far data reached; below `duration_ms` this is a prefix, never filled in. */
  observed_through_ms: number;
  open_scan_ms: number;
  close_scan_ms: number;
}

/**
 * One member's own scheduler time inside the execution window.
 *
 * A thread that ran on two cores for ten seconds contributes about twenty
 * seconds, so `usec` may legitimately exceed the wall-clock duration. It is
 * never a utilisation percentage.
 */
export interface TurnCpuResourcesApi {
  status: ResourceMetricStatus;
  method: 'live_and_exit_task_counters';
  usec: number | null;
  /** Sandbox CPU quota in cores captured for this window; absent in older records. */
  capacity_cores?: number | null;
  reason_codes: TurnResourceReasonCode[];
}

/**
 * The sampled peak of the member's proportional resident memory.
 *
 * PSS shares mapped resident pages between users instead of billing each of
 * them in full, but it is not machine memory: it excludes the VM kernel,
 * unmapped page cache, host services and special page classes, and RSS is
 * never substituted for it. Sampling cannot see between ticks, so even a
 * complete plan stays labelled as sampled.
 */
export interface TurnMemoryResourcesApi {
  status: ResourceMetricStatus;
  method: 'sampled_member_pss';
  peak_bytes: number | null;
  sample_interval_ms: number;
  /** Cycles in which every one of the member's address spaces read cleanly. */
  valid_samples: number;
  /** Cycles the schedule intended for the window's duration. */
  expected_samples: number;
  max_gap_ms: number;
  reason_codes: TurnResourceReasonCode[];
}

/**
 * Storage I/O the kernel charged to the member's own tasks.
 *
 * This is task I/O accounting, not what the host disk physically did, so the
 * method is named beside the number wherever it is shown.
 */
export interface TurnStorageIoResourcesApi {
  status: ResourceMetricStatus;
  method: 'task_io_accounting';
  read_bytes: number | null;
  write_bytes: number | null;
  reason_codes: TurnResourceReasonCode[];
}

/**
 * One attempt's whole-turn member execution window.
 *
 * `state` is derived, not stored: a `collecting` snapshot for an attempt that
 * has already ended reads as `interrupted`, with `observed_through_ms` kept so
 * the UI can say how far it actually got instead of rounding it up to the end
 * of the task.
 */
export interface TurnResourceSummaryApi {
  window: TurnResourceWindowApi;
  measurement_id: string;
  state: TurnResourceState | 'interrupted';
  /** The capability string that produced the summary; never added across sources. */
  source: string;
  received_at: string | null;
  cpu: TurnCpuResourcesApi;
  memory: TurnMemoryResourcesApi;
  storage_io: TurnStorageIoResourcesApi;
  /**
   * How many attempts the run's numbers cover, out of how many it has, taken
   * from the run's compact projection. A run-level fact rather than an
   * attempt-level one, so an attempt response leaves both unset; when they are
   * present and differ, the summary must say that it is not the run's total.
   */
  attempts_measured?: number;
  attempts_total?: number;
}

/** One metric's status alone, for the Runs list's compact summary. */
export interface ResourceMetricBriefApi {
  status: ResourceMetricStatus;
  reason_codes: TurnResourceReasonCode[];
}

/**
 * The compact per-run resource projection the Runs list shows.
 *
 * Flattened scalars only, so listing a page of runs stays one bounded query
 * over exactly that page. The detail view carries the full
 * `TurnResourceSummaryApi`; this exists so the list does not have to.
 *
 * `source_mismatch` is the one reason code that can appear on a brief the
 * reducer did not take from a single stored summary: it means the run's
 * attempts were measured by different capabilities or schema versions, which
 * are never mixed into one total.
 */
export interface RunResourceSummaryApi {
  cpu_usec: number | null;
  memory_peak_bytes: number | null;
  state: TurnResourceState | 'interrupted';
  observed_through_ms: number | null;
  cpu: ResourceMetricBriefApi;
  memory: ResourceMetricBriefApi;
  storage_io: ResourceMetricBriefApi;
  /**
   * How many attempts the numbers cover, out of how many the run has. A total
   * over three of five executions is not the run's total.
   */
  attempts_measured: number;
  attempts_total: number;
}

export interface AgentAttemptSummaryApi {
  attempt_id: number;
  attempt_no: number;
  continuity_mode: 'hot' | 'warm_native' | 'cold_native' | 'replay' | null;
  context_status: 'new' | 'live' | 'restored' | 'lost' | null;
  duration_ms: number | null;
}

export interface AgentAttemptTelemetryApi extends AgentAttemptSummaryApi {
  /**
   * The attempt's measurements. The turn summary lives inside this object at
   * ``resources.turn_summary``, because that is where the server puts it: the
   * summary is one of the attempt's resources, not a sibling of them.
   */
  resources: AttemptResourcesApi | null;
}

export async function getAgentAttemptTelemetry(
  jobId: string,
  attemptId: number,
  signal?: AbortSignal,
): Promise<AgentAttemptTelemetryApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/attempts/${attemptId}/telemetry`,
    { signal },
  );
  return jsonOrThrow<AgentAttemptTelemetryApi>(resp);
}

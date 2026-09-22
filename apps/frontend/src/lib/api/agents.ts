import type { SelectedSkillApi } from './skills';
// Cloud agent sandbox API (issue #1041).
//
// Two things here are shaped by the backend's design rather than by
// convenience:
//
// 1. **The event stream is read with fetch + ReadableStream, not EventSource.**
//    EventSource cannot set an Authorization header, and these endpoints are
//    authenticated. This mirrors the streaming-chat client in `chat.ts`.
// 2. **Every frame carries a global event id, and reconnects resume from it.**
//    The gateway applies a finite cap to long-lived SSE, so a job that outlives
//    one connection is normal, not exceptional — resuming by cursor is how the
//    UI stays complete across that.

import { fetchWithAuth, jsonOrThrow } from './client';
import { announceAgentSessionsChanged } from './agentSessionEvents';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

/** Server-side job states. The UI maps these onto its own display states. */
export type AgentJobApiState =
  | 'queued'
  | 'running'
  | 'publishing'
  | 'succeeded'
  | 'failed'
  | 'cancelled';

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
export interface TurnResourceSummaryApi extends TurnResourceWindowApi {
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

export type AgentLedgerStatus = 'pending' | 'available' | 'partial' | 'unavailable' | 'stale';

export interface AgentLedgerTotalsApi {
  calls: number;
  tokens_in: number | null;
  tokens_out: number | null;
  cache_read: number | null;
  cache_write: number | null;
  reasoning: number | null;
  /** Decimal USD, preserving the Gateway's precision. */
  spent_usd: string | null;
}

export type AgentLedgerUnknownRowsApi = Record<
  Exclude<keyof AgentLedgerTotalsApi, 'calls'>,
  number
>;

export interface AgentAttemptUsageApi {
  attempt_id: number;
  attempt_no: number;
  status: AgentLedgerStatus;
  reason: string | null;
  totals: AgentLedgerTotalsApi | null;
  unknown_rows: AgentLedgerUnknownRowsApi | null;
  estimated_calls: number;
  queried_at: string | null;
  refreshing?: boolean;
  next_refresh_at?: string | null;
  windows: Array<{ since: string; until: string; basis: 'request_started_at' }>;
}

/** Cached job-scoped usage, including retries. The browser never queries the Gateway. */
export interface AgentJobUsageApi {
  ledger: {
    status: AgentLedgerStatus;
    source: 'gateway_api_logs';
    attribution: 'grant_time_window';
    finalized: false;
    totals: AgentLedgerTotalsApi | null;
    unknown_rows: AgentLedgerUnknownRowsApi | null;
    estimated_calls: number;
    queried_at: string | null;
    missing_attempts: number;
    refreshing?: boolean;
    next_refresh_at?: string | null;
    attempts: AgentAttemptUsageApi[];
  };
  runtime: {
    source: 'runtime';
    input_tokens: number | null;
    output_tokens: number | null;
    cache_read_tokens: number | null;
    /** Cache-inclusive input, null when the harness counters cannot establish it. */
    normalized_input_tokens?: number | null;
  };
  conversation?: {
    totals: AgentLedgerTotalsApi | null;
    unknown_rows: AgentLedgerUnknownRowsApi | null;
    estimated_calls: number;
    queried_at: string | null;
    turn_count: number;
    incomplete: boolean;
    refreshing: boolean;
    next_refresh_at?: string | null;
    active?: boolean;
  } | null;
}

export interface AgentJobApi {
  skills?: SelectedSkillApi[];
  /** How this turn came to exist. `goal` turns are the platform's own rounds. */
  turn_source?: 'human' | 'goal';
  goal_round?: number | null;
  /** `repair` is the one bounded chance a malformed report gets to restate. */
  goal_round_kind?: 'work' | 'repair' | null;
  /**
   * How the platform read this turn's report block. Only an automatic round
   * carries one: a person's turn is not part of the completion protocol.
   */
  goal_report_parse?: GoalReportParse | null;
  /** What a valid report said; null unless `goal_report_parse` is `valid`. */
  goal_report_outcome?: GoalReportOutcome | null;
  /** A failed parse's explanation, or the reason a valid report carried. */
  goal_report_detail?: string | null;
  goal_repair_round?: number | null;
  /** The conversation's live goal, on every turn of it. */
  goal?: GoalApi | null;
  title?: string | null;
  title_source?: 'fallback' | 'generated' | 'manual' | 'inherited' | null;
  id: string;
  /** Actual execution timestamps, matching duration_ms's selected attempt. */
  started_at?: string | null;
  finished_at?: string | null;
  repo: string;
  task_prompt: string;
  runtime: string;
  model: string;
  /** How hard the model was asked to think. Null is Auto — nobody chose. */
  reasoning_effort?: string | null;
  /** Requested branch/ref before the backend pinned it to base_sha. */
  base_ref?: string | null;
  base_sha: string | null;
  /** Server-authoritative branch used by the publisher. */
  output_branch?: string | null;
  state: AgentJobApiState;
  /** Current activity across the conversation, independent of the viewed turn. */
  conversation_state?: 'queued' | 'running' | 'idle' | 'failed' | null;
  cancel_requested: boolean;
  current_attempt_id: number | null;
  published_pr_url: string | null;
  published_commit_sha?: string | null;
  detail: string | null;
  metadata: Record<string, unknown> | null;
  /** Names only; optional so a pre-MCP deployment stays readable. */
  mcp_servers?: string[];
  created_at: string | null;
  updated_at: string | null;
  pinned_at?: string | null;
  archived_at?: string | null;
  // Read server-side from the billing ledger, never from the agent's own
  // report. Null means "no ledger configured", which is not the same as zero.
  spent_usd: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  model_calls: number | null;
  setup_egress_tier: string | null;
  agent_egress_tier: string | null;
  /** Attempt whose continuity and resource telemetry is attached below. */
  telemetry_attempt_id?: number | null;
  /** Which continuity rung this turn ran on. Null on turns that predate it. */
  continuity_mode?: 'hot' | 'warm_native' | 'cold_native' | 'replay' | null;
  /** Canonical user-facing context state for restartable chat sandboxes. */
  context_status?: 'new' | 'live' | 'restored' | 'lost' | null;
  /** Process state is independent from context correctness. */
  resident_state?: 'stopped' | 'starting' | 'live' | 'restarting' | 'lost' | null;
  sandbox_id?: string | null;
  workspace_shared?: boolean;
  native_state?: 'new' | 'ready' | 'fork_preparing' | 'lost' | null;
  fork_anchor_job_id?: string | null;
  native_fork_available?: boolean;
  workspace_publication_state?:
    | 'pending'
    | 'capturing'
    | 'captured'
    | 'publishing'
    | 'published'
    | 'failed'
    | null;
  workspace_epoch?: number | null;
  duration_ms?: number | null;
  /**
   * Resource evidence for this attempt. Null when no accounting row exists.
   * Current shared-member rows intentionally have no container totals;
   * historical sampled rows with partial coverage remain lower bounds.
   */
  resources?: AttemptResourcesApi | null;
  /** Lightweight retry summaries; resource detail is loaded only on selection. */
  attempt_telemetry?: AgentAttemptSummaryApi[];
  /** Conversation fields are optional so pre-thread deployments remain readable. */
  thread_id?: string | null;
  parent_job_id?: string | null;
  turn_no?: number;
  /** Set on turns created by a fork: the original turn this row copies. */
  forked_from_job_id?: string | null;
}

export interface AgentThreadMessageApi {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  job_id: string;
  created_at: string | null;
}

export interface AgentThreadApi {
  archived_at?: string | null;
  thread_id: string;
  title?: string;
  repo?: string;
  messages: AgentThreadMessageApi[];
  jobs: AgentJobApi[];
  created_at?: string | null;
  updated_at?: string | null;
}

/**
 * Aggregate numbers for a thread. A null field means the thread's runtime
 * does not report that measure — render it as a dash, never as a zero.
 */
export interface AgentThreadStatsApi {
  thread_id: string;
  turns: number;
  tool_calls: number;
  failed_tool_calls: number;
  wall_seconds: number;
  model_seconds: number;
  tool_seconds: number;
  ttft_seconds: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_hit_ratio: number | null;
  output_tokens_per_second: number | null;
  tokens_source: string;
  timing_source: string;
}

export interface AgentJobEventApi {
  id: number;
  attempt_id: number;
  seq: number;
  event_type: string;
  payload: Record<string, unknown> | null;
  created_at: string | null;
}

export type AgentJobFileStatus = 'added' | 'modified' | 'deleted';

export interface AgentJobFileEntryApi {
  name: string;
  path: string;
  kind: 'file' | 'directory' | 'symlink';
  status?: AgentJobFileStatus | null;
  size?: number | null;
  binary?: boolean;
  truncated?: boolean;
  omitted_reason?: string | null;
}

export interface AgentJobDirectoryApi {
  path: string;
  kind: 'directory';
  entries: AgentJobFileEntryApi[] | null;
  writable?: boolean;
  source?: 'workspace';
}

export interface AgentJobFileApi {
  path: string;
  kind: 'file';
  content: string | null;
  size: number | null;
  binary: boolean;
  truncated: boolean;
  status?: AgentJobFileStatus | null;
  omitted_reason?: string | null;
  writable?: boolean;
  source?: 'workspace';
}

export interface AgentJobSymlinkApi {
  path: string;
  kind: 'symlink';
  status?: AgentJobFileStatus | null;
  omitted_reason?: string | null;
  writable?: boolean;
  source?: 'workspace';
}

export type AgentJobFilesApi = AgentJobDirectoryApi | AgentJobFileApi | AgentJobSymlinkApi;

/** A completion condition the platform judges at the end of every turn. */
export interface GoalApi {
  /** Identity of the objective; replacing the objective mints a new one. */
  goal_id: string;
  objective: string;
  state: 'active' | 'paused' | 'complete';
  revision: number;
  /**
   * The bound the owner stated themselves, or `null` when they stated none —
   * which is the ordinary case. Never a large integer: "no limit" is the
   * absence of a value, so a reader can tell a chosen bound from no bound.
   */
  max_rounds: number | null;
  /** Rounds actually charged. A statistic, not a stop condition. */
  rounds_used: number;
  /** Why an active goal stopped continuing; null in every other state. */
  pause_reason: GoalPauseReason | null;
  /** The sentence behind the reason. Always set while paused. */
  pause_detail: string | null;
  /** The only basis a first-version goal completes on. */
  completion_basis: 'model_reported' | null;
  completed_by_job_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** Why a goal stopped continuing itself, as the goal record names it. */
export type GoalPauseReason =
  | 'user'
  | 'fork'
  | 'needs_input'
  | 'round_limit'
  | 'execution_failed'
  | 'cancelled'
  | 'report_invalid'
  | 'report_unavailable'
  | 'runtime_unsupported'
  | 'context_lost'
  | 'access_denied'
  | 'quota_unavailable'
  | 'scheduling_failed'
  | 'archived';

/** How the platform read one turn's report block. */
export type GoalReportParse = 'absent' | 'valid' | 'invalid' | 'unavailable';

/** What a valid report said about the objective. */
export type GoalReportOutcome = 'complete' | 'needs_input';

/** What a client sends to set one. */
export interface GoalInputApi {
  objective: string;
  /**
   * An explicit maximum number of automatic rounds. This UI never sends one:
   * the product asks the owner what to achieve, not how many rounds to spend,
   * and a goal without this field has no limit at all. The field exists for a
   * caller that wants to state a bound, and the server honors it exactly.
   */
  max_rounds?: number;
}

/** What the deployment advertises about goals: the objective bound, and only it. */
export interface GoalLimitsApi {
  max_objective_chars: number;
}

export interface AgentConfigApi {
  skill_limits?: import('@/lib/skillLimits').SkillLimits;
  goal_limits?: GoalLimitsApi;

  /** Upload bounds. Absent on a control plane that predates attachments. */
  attachment_limits?: import('@/lib/attachmentLimits').AttachmentLimits;
  repos: string[];
  runtimes: string[];
  /** Models an agent job can actually call — the create endpoint's own list. */
  models: string[];
  /** MCP servers this deployment offers. Never carries a URL or a credential. */
  mcp_servers?: AgentMcpServerApi[];
  /** Runtimes that can be given MCP servers; others refuse such a job. */
  mcp_runtimes?: string[];
  /**
   * Effort levels each runtime can express, in its own vocabulary. A runtime
   * absent here cannot express one at all.
   */
  runtime_reasoning_efforts?: Record<string, string[]>;
  /**
   * Effort levels each model accepts. Offer the intersection with the chosen
   * runtime's, and no control when it is empty — which is also what a backend
   * too old to answer these produces.
   */
  model_reasoning_efforts?: Record<string, string[]>;
  setup_egress_tier: string | null;
  agent_egress_tier: string | null;
  github_connected: boolean;
  github_install_url: string | null;
}

export interface AgentMcpServerApi {
  name: string;
  description: string;
  /** Tools exposed. Empty means every tool the server offers. */
  tools: string[];
  /** Applied when a job does not choose its servers explicitly. */
  default: boolean;
}

/** What this deployment will actually accept — the source for the pickers. */
export async function getAgentConfig(): Promise<AgentConfigApi> {
  return jsonOrThrow(await fetchWithAuth(API_BASE, '/v1/agent/config'));
}

export type AgentIntegrationProvider = 'github' | 'gitlab';

export interface AgentIntegrationAccountApi {
  id: string;
  label: string;
  web_url?: string | null;
}

export interface AgentIntegrationRepositoryApi {
  id: string;
  name: string;
  web_url?: string | null;
}

export interface AgentIntegrationProviderApi {
  provider: AgentIntegrationProvider;
  configured: boolean;
  connected: boolean;
  connect_url: string | null;
  manage_url?: string | null;
  capabilities: string[];
  accounts: AgentIntegrationAccountApi[];
  repositories: AgentIntegrationRepositoryApi[];
  error?: string | null;
}

export interface AgentIntegrationsApi {
  providers: AgentIntegrationProviderApi[];
}

export async function getAgentIntegrations(): Promise<AgentIntegrationsApi> {
  const resp = await fetchWithAuth(API_BASE, '/v1/agent/integrations');
  return jsonOrThrow(resp);
}

export interface GitHubConnectionApi {
  connections: Array<{ installation_id: number; account_login: string | null }>;
  repos: string[];
  install_url: string | null;
}

/**
 * Complete the GitHub connection with the code GitHub handed the browser.
 *
 * The platform validates the one-time state, exchanges the code for a token
 * that speaks as this user, and asks GitHub which installations they can reach.
 */
export async function connectGitHub(code: string, state: string): Promise<GitHubConnectionApi> {
  const resp = await fetchWithAuth(API_BASE, '/v1/agent/integrations/github/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, state }),
  });
  return jsonOrThrow(resp);
}

export async function connectGitLab(
  code: string,
  state: string,
): Promise<AgentIntegrationProviderApi> {
  const resp = await fetchWithAuth(API_BASE, '/v1/agent/integrations/gitlab/connect', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, state }),
  });
  return jsonOrThrow(resp);
}

export async function disconnectAgentIntegration(
  provider: AgentIntegrationProvider,
  connectionId: string,
): Promise<void> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/integrations/${provider}/connections/${encodeURIComponent(connectionId)}`,
    { method: 'DELETE' },
  );
  if (!resp.ok) await jsonOrThrow(resp);
}

/**
 * Model ids this gateway serves — the model picker's options.
 *
 * Sourced from the agent config, not /v1/models: that list answers for the
 * browsing user, and on staging it offered 15 models of which 13 failed the
 * job's first call. The config's list is the create endpoint's own predicate.
 */
export async function listAgentModels(): Promise<string[]> {
  const cfg = await getAgentConfig();
  return cfg.models ?? [];
}

export interface RepoBranchesApi {
  default: string | null;
  branches: string[];
}

/** Branches of one entitled repository, for the composer's branch picker. */
export async function listRepoBranches(repo: string): Promise<RepoBranchesApi> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/branches?repo=${encodeURIComponent(repo)}`);
  return jsonOrThrow(resp);
}

/**
 * One MCP connection a task may call, and the tools it may call on it.
 *
 * A capability the map does not name is denied, and the control plane derives
 * `enabled` from whether anything is left non-denied — so an empty map binds
 * the server switched off rather than fully open.
 */
export interface McpBindingInputApi {
  connection_id: number;
  alias?: string;
  enabled?: boolean;
  tools?: Record<string, 'deny' | 'confirm' | 'allow'>;
  resources?: Record<string, 'deny' | 'confirm' | 'allow'>;
  prompts?: Record<string, 'deny' | 'confirm' | 'allow'>;
}

export interface CreateAgentJobRequest {
  /** Omitted uses personal defaults; an empty list selects none. */
  skill_ids?: number[];
  /** Staged uploads to place under /uploads. Omitted attaches nothing. */
  attachment_ids?: number[];
  /** MCP connections this task may call. Omitted gives it none. */
  mcp_bindings?: McpBindingInputApi[];
  /** Omitted means no goal: nothing continues without the owner asking again. */
  goal?: GoalInputApi;
  repo: string;
  task_prompt: string;
  model: string;
  runtime?: string;
  /** Omitted is Auto: the runtime and model decide for themselves. */
  reasoning_effort?: string;
  /** A branch. The platform pins it to a commit at creation. */
  base_ref?: string;
  base_sha?: string;
}

export async function listAgentJobs(
  limit = 50,
  archived = false,
  repo?: string,
): Promise<AgentJobApi[]> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (archived) query.set('archived', 'true');
  if (repo) query.set('repo', repo);
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs?${query.toString()}`);
  const body = await jsonOrThrow<{ jobs: AgentJobApi[] }>(resp);
  return body.jobs;
}

export interface AgentConversationApi {
  job: AgentJobApi;
  title: string;
  archived: boolean;
  last_activity_at: string | null;
}

export async function listAgentConversations(
  options: {
    q?: string;
    repo?: string;
    includeArchived?: boolean;
    pinned?: boolean;
    cursor?: string;
    limit?: number;
  } = {},
): Promise<{ conversations: AgentConversationApi[]; next_cursor: string | null }> {
  const query = new URLSearchParams({ limit: String(options.limit ?? 50) });
  if (options.q) query.set('q', options.q);
  if (options.repo) query.set('repo', options.repo);
  if (options.includeArchived) query.set('include_archived', 'true');
  if (options.pinned !== undefined) query.set('pinned', String(options.pinned));
  if (options.cursor) query.set('cursor', options.cursor);
  return jsonOrThrow(await fetchWithAuth(API_BASE, `/v1/agent/conversations?${query}`));
}

export interface AgentProjectApi {
  repo: string;
  /** Conversations, not turns. */
  task_count: number;
  /** Non-terminal jobs, so a collapsed project can still show live work. */
  active_count: number;
  last_activity_at: string | null;
  pinned_count?: number;
  pinned_at?: string | null;
}

/**
 * Every repo the caller has tasks in, most recently active first.
 *
 * Separate from the job list because the sidebar's folder tree must include
 * projects with no activity inside the newest-first job window — listing only
 * the repos present in that page would make dormant projects disappear.
 */
export async function listAgentProjects(archived = false): Promise<AgentProjectApi[]> {
  const query = archived ? '?archived=true' : '';
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/projects${query}`);
  const body = await jsonOrThrow<{ projects: AgentProjectApi[] }>(resp);
  return body.projects;
}

export interface AgentThreadArchiveApi {
  thread_id: string;
  archived: boolean;
  archived_at: string | null;
}

export type AgentSessionState =
  | 'running'
  | 'queued'
  | 'idle'
  | 'needs_review'
  | 'done'
  | 'failed'
  | 'cancelled';

export interface AgentSessionApi {
  thread_id: string;
  title: string;
  archived_at: string | null;
  job: AgentJobApi;
  can_delete: boolean;
  delete_block_reason?:
    | 'session_active'
    | 'fork_in_progress'
    | 'publication_pending'
    | 'not_archived'
    | null;
}

export interface AgentSessionProjectApi {
  repo: string;
  count: number;
}

export interface AgentSessionListApi {
  sessions: AgentSessionApi[];
  total: number | null;
  next_cursor: string | null;
  /** Facets honor search and state, before selecting one repository; null when omitted. */
  projects: AgentSessionProjectApi[] | null;
}

export interface AgentSessionListOptions {
  archived?: boolean;
  includeSummary?: boolean;
  limit?: number;
  cursor?: string;
  search?: string;
  repo?: string;
  state?: AgentSessionState;
}

export async function listAgentSessions(
  options: AgentSessionListOptions = {},
  signal?: AbortSignal,
): Promise<AgentSessionListApi> {
  const query = new URLSearchParams({
    archived: String(options.archived ?? true),
    limit: String(options.limit ?? 50),
  });
  if (options.cursor) query.set('cursor', options.cursor);
  if (options.search) query.set('search', options.search);
  if (options.repo !== undefined) query.set('repo', options.repo);
  if (options.state) query.set('state', options.state);
  if (options.includeSummary !== undefined)
    query.set('include_summary', String(options.includeSummary));
  const response = await fetchWithAuth(API_BASE, `/v1/agent/sessions?${query}`, { signal });
  return jsonOrThrow<AgentSessionListApi>(response);
}

/** Matches the backend's explicit session-selection bound. */
export const AGENT_SESSION_SELECTION_LIMIT = 1000;

export async function setAgentSessionsArchived(
  threadIds: string[],
  archived: boolean,
): Promise<{ sessions: AgentThreadArchiveApi[] }> {
  const response = await fetchWithAuth(API_BASE, '/v1/agent/sessions/archive', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thread_ids: threadIds, archived }),
  });
  const result = await jsonOrThrow<{ sessions: AgentThreadArchiveApi[] }>(response);
  announceAgentSessionsChanged({ threadIds: result.sessions.map((s) => s.thread_id), archived });
  return result;
}

export async function deleteAgentSessions(threadIds: string[]): Promise<{ thread_ids: string[] }> {
  const response = await fetchWithAuth(API_BASE, '/v1/agent/sessions/delete', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ thread_ids: threadIds }),
  });
  const result = await jsonOrThrow<{ thread_ids: string[] }>(response);
  announceAgentSessionsChanged({ threadIds: result.thread_ids, deleted: true });
  return result;
}

/** Hide the entire conversation containing this job from active task history. */
export async function archiveAgentJob(jobId: string): Promise<AgentThreadArchiveApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/archive`,
    {
      method: 'POST',
    },
  );
  const result = await jsonOrThrow<AgentThreadArchiveApi>(resp);
  announceAgentSessionsChanged({ threadIds: [result.thread_id], archived: true });
  return result;
}

/** Return an archived conversation to active task history. */
export async function restoreAgentJob(jobId: string): Promise<AgentThreadArchiveApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/archive`,
    {
      method: 'DELETE',
    },
  );
  const result = await jsonOrThrow<AgentThreadArchiveApi>(resp);
  announceAgentSessionsChanged({ threadIds: [result.thread_id], archived: false });
  return result;
}

/** Rename the conversation without changing any turn's prompt. */
export async function renameAgentJob(
  jobId: string,
  title: string,
): Promise<{ thread_id: string; title: string }> {
  const response = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/title`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title }),
    },
  );
  const result = await jsonOrThrow<{ thread_id: string; title: string }>(response);
  announceAgentSessionsChanged({ threadIds: [result.thread_id], title: result.title });
  return result;
}

export interface AgentThreadPinApi {
  thread_id: string;
  pinned: boolean;
  pinned_at: string | null;
}

/** Keep the entire conversation containing this job at the top of task history. */
export async function pinAgentJob(jobId: string): Promise<AgentThreadPinApi> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/pin`, {
    method: 'POST',
  });
  const result = await jsonOrThrow<AgentThreadPinApi>(resp);
  announceAgentSessionsChanged({ threadIds: [result.thread_id], pinnedAt: result.pinned_at });
  return result;
}

/** Return a pinned conversation to normal activity ordering. */
export async function unpinAgentJob(jobId: string): Promise<AgentThreadPinApi> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/pin`, {
    method: 'DELETE',
  });
  const result = await jsonOrThrow<AgentThreadPinApi>(resp);
  announceAgentSessionsChanged({ threadIds: [result.thread_id], pinnedAt: result.pinned_at });
  return result;
}

export async function getAgentJob(jobId: string): Promise<AgentJobApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}?include_resources=false`,
  );
  const job = await jsonOrThrow<AgentJobApi>(resp);
  // Pin legacy telemetry to the fetched attempt before SSE advances current_attempt_id.
  return job.telemetry_attempt_id === undefined
    ? { ...job, telemetry_attempt_id: job.current_attempt_id }
    : job;
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

/** Read cached snapshots; a pre-ledger control plane returns null, never zero usage. */
export async function getAgentJobUsage(
  jobId: string,
  signal?: AbortSignal,
): Promise<AgentJobUsageApi | null> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/usage`, {
    signal,
  });
  if (resp.status === 404) return null;
  return jsonOrThrow<AgentJobUsageApi>(resp);
}

/** Queue a refresh in the control plane and return the current cached snapshots. */
export async function refreshAgentJobUsage(
  jobId: string,
  signal?: AbortSignal,
): Promise<AgentJobUsageApi | null> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/usage/refresh`,
    { method: 'POST', signal },
  );
  if (resp.status === 404) return null;
  return jsonOrThrow<AgentJobUsageApi>(resp);
}

/** Load the conversation containing a job. Old standalone jobs return null. */
export async function getAgentJobThread(jobId: string): Promise<AgentThreadApi | null> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/thread`);
  if (resp.status === 404) return null;
  return jsonOrThrow<AgentThreadApi>(resp);
}

/** Load a thread's aggregate stats. Old standalone jobs return null. */
export async function getAgentThreadStats(jobId: string): Promise<AgentThreadStatsApi | null> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/thread/stats`,
  );
  if (resp.status === 404) return null;
  return jsonOrThrow<AgentThreadStatsApi>(resp);
}

export async function createAgentJob(body: CreateAgentJobRequest): Promise<AgentJobApi> {
  return submitOnce('create', (requestId) =>
    fetchWithAuth(API_BASE, '/v1/agent/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': requestId },
      body: JSON.stringify(body),
    }),
  );
}

/** A goal write, with the revision the caller read it at. */
export interface GoalWriteApi {
  goal: GoalInputApi;
  revision?: number;
}

/** The conversation's goal after a read or a write, and its control revision. */
export interface GoalResponseApi {
  thread_id: string;
  goal: GoalApi | null;
  revision: number;
}

/** The conversation after removing its goal. */
export interface GoalClearedApi {
  thread_id: string;
  cleared: boolean;
  revision: number;
}

/** The conversation's goal, and the revision a compare-and-set write needs. */
export async function getAgentGoal(threadId: string): Promise<GoalResponseApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/threads/${encodeURIComponent(threadId)}/goal`,
  );
  return jsonOrThrow(resp);
}

/** Set, replace or edit the conversation's completion condition. */
export async function setAgentGoal(threadId: string, body: GoalWriteApi): Promise<GoalResponseApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/threads/${encodeURIComponent(threadId)}/goal`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  );
  return jsonOrThrow(resp);
}

/** Stop the goal continuing itself; the turn already running is left alone. */
export async function pauseAgentGoal(threadId: string): Promise<GoalResponseApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/threads/${encodeURIComponent(threadId)}/goal/pause`,
    { method: 'POST' },
  );
  return jsonOrThrow(resp);
}

/** Start a paused goal moving again, scheduling a round when nothing is pending. */
export async function resumeAgentGoal(threadId: string): Promise<GoalResponseApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/threads/${encodeURIComponent(threadId)}/goal/resume`,
    { method: 'POST' },
  );
  return jsonOrThrow(resp);
}

/** Remove the goal and every automatic round queued for it. */
export async function clearAgentGoal(threadId: string): Promise<GoalClearedApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/threads/${encodeURIComponent(threadId)}/goal`,
    { method: 'DELETE' },
  );
  return jsonOrThrow(resp);
}

export async function cancelAgentJob(
  jobId: string,
): Promise<{ id: string; state: string; cancel_requested: boolean }> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST',
  });
  return jsonOrThrow(resp);
}

export interface FollowUpAgentJobRequest {
  /** Staged uploads to deliver with this turn. */
  attachment_ids?: number[];
  /** Omitted inherits saved content; explicit ids snapshot the current library. */
  skill_ids?: number[];
  /** Set or replace the conversation's goal along with this message. */
  goal?: GoalInputApi;
  prompt: string;
  /** Omitted values inherit the parent run's harness and model. */
  runtime?: string;
  model?: string;
  /**
   * Omitted inherits the previous turn's effort. The literal `'auto'` clears
   * it back to unset — null cannot mean "clear", it is already what "inherit"
   * sends. No composer sends the sentinel (they always show a real level and
   * send it); it stays because it is the only way an API caller can undo an
   * inherited choice.
   */
  reasoning_effort?: string;
}

/** Queue another turn in the same thread, after its currently active run. */
export async function followUpAgentJob(
  jobId: string,
  body: FollowUpAgentJobRequest,
): Promise<AgentJobApi> {
  return submitOnce(`follow-up:${jobId}`, (requestId) =>
    fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/follow-ups`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': requestId },
      body: JSON.stringify(body),
    }),
  );
}

/**
 * A submission is one user action, however many times its request is sent.
 *
 * The control plane recognises a resent create or follow-up by its
 * `Idempotency-Key` and answers with the job the first send made instead of
 * queueing a second task. So the key must outlive a lost response: it is
 * minted per pending submission scope (the composer, or one thread's next
 * turn), kept in session storage across a reload, and dropped only once a
 * definitive answer arrives. A `409 idempotency_conflict` means the key was
 * reused for different content — an edited resubmit after a failure — and
 * that is a new action, so the key rotates and the request is sent once more.
 */
const pendingSubmissionIds = new Map<string, string>();

function submissionStorageKey(scope: string): string {
  return `agent-submission-request-${scope}`;
}

function pendingSubmissionId(scope: string): string {
  const memory = pendingSubmissionIds.get(scope);
  if (memory) return memory;
  try {
    const stored = globalThis.sessionStorage?.getItem(submissionStorageKey(scope));
    if (stored) {
      pendingSubmissionIds.set(scope, stored);
      return stored;
    }
  } catch {
    // Privacy settings may block browser storage; in-memory idempotency still
    // protects repeated clicks for the lifetime of this page.
  }
  const created =
    globalThis.crypto?.randomUUID?.() ??
    `submit-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  pendingSubmissionIds.set(scope, created);
  try {
    globalThis.sessionStorage?.setItem(submissionStorageKey(scope), created);
  } catch {
    // See the read path above.
  }
  return created;
}

function clearPendingSubmission(scope: string): void {
  pendingSubmissionIds.delete(scope);
  try {
    globalThis.sessionStorage?.removeItem(submissionStorageKey(scope));
  } catch {
    // A definitive response is already authoritative; cleanup is best effort.
  }
}

async function submitOnce(
  scope: string,
  send: (requestId: string) => Promise<Response>,
): Promise<AgentJobApi> {
  let resp = await send(pendingSubmissionId(scope));
  if (resp.status === 409 && (await forkFailureType(resp)) === 'idempotency_conflict') {
    // Same key, different content: the earlier send was a different action
    // and is not what the user is submitting now.
    clearPendingSubmission(scope);
    resp = await send(pendingSubmissionId(scope));
  }
  if (!resp.ok) {
    // Every other failure keeps the key: a lost or ambiguous response may
    // hide a committed job, and the retry is how it is recovered.
    return jsonOrThrow<AgentJobApi>(resp);
  }
  const job = await jsonOrThrow<AgentJobApi>(resp);
  clearPendingSubmission(scope);
  return job;
}

/**
 * Capability-gated shared chats native-fork the selected settled assistant
 * boundary into a second live CLI session in the same sandbox/workspace.
 * Chats without retained native state fail closed; transcript copying never
 * substitutes for provider context.
 *
 * The fork-only call starts/probes the child's resident CLI but sends no
 * prompt and performs no model call. Returns the copied anchor turn — the
 * page to navigate to.
 */
const pendingForkRequestIds = new Map<string, string>();

function forkRequestStorageKey(jobId: string): string {
  return `agent-native-fork-request-${jobId}`;
}

function pendingForkRequestId(jobId: string): string {
  const memory = pendingForkRequestIds.get(jobId);
  if (memory) return memory;
  try {
    const stored = globalThis.sessionStorage?.getItem(forkRequestStorageKey(jobId));
    if (stored) {
      pendingForkRequestIds.set(jobId, stored);
      return stored;
    }
  } catch {
    // Privacy settings may block browser storage; in-memory idempotency still
    // protects repeated clicks for the lifetime of this page.
  }
  const created =
    globalThis.crypto?.randomUUID?.() ??
    `fork-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  pendingForkRequestIds.set(jobId, created);
  try {
    globalThis.sessionStorage?.setItem(forkRequestStorageKey(jobId), created);
  } catch {
    // See the read path above.
  }
  return created;
}

function clearPendingForkRequest(jobId: string): void {
  pendingForkRequestIds.delete(jobId);
  try {
    globalThis.sessionStorage?.removeItem(forkRequestStorageKey(jobId));
  } catch {
    // A successful response is already authoritative; storage cleanup is best effort.
  }
}

async function forkFailureType(resp: Response): Promise<string | null> {
  if (resp.ok) return null;
  try {
    const body = (await resp.clone().json()) as {
      error?: { type?: unknown };
      detail?: { error?: { type?: unknown } };
    };
    const type = body.error?.type ?? body.detail?.error?.type;
    return typeof type === 'string' ? type : null;
  } catch {
    return null;
  }
}

export async function forkAgentJob(
  jobId: string,
  options: {
    requestScope?: string;
    /** A durable coordinator owns this ID and its receipt; this API never rotates it. */
    requestId?: string;
  } = {},
): Promise<AgentJobApi> {
  // Separate entry points can fork the same anchor for different purposes.
  // A side chat's recovery must not consume an Edit & rewind request's child.
  const scope = options.requestScope ? `${options.requestScope}:${jobId}` : jobId;
  const requestId = options.requestId ?? pendingForkRequestId(scope);
  try {
    const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/fork`, {
      method: 'POST',
      headers: { 'Idempotency-Key': requestId },
    });
    // Network/5xx/ordinary conflict retries must retain this key: the host may
    // already have materialized the child and the repeated request is how the
    // control plane recovers it. A durable FAILED saga is different: replaying
    // its key can only return the same failure forever, so allow the next user
    // click to start a new, independently fenced operation after reconciliation.
    if ((await forkFailureType(resp)) === 'native_fork_failed') {
      if (!options.requestId) clearPendingForkRequest(scope);
    }
    const result = await jsonOrThrow<AgentJobApi>(resp);
    if (!options.requestId) clearPendingForkRequest(scope);
    return result;
  } catch (cause) {
    // A lost response may hide a committed fork. Keep the same key so the
    // owner's retry returns that child instead of creating another one.
    throw cause;
  }
}

/** Page through the event log. Used to backfill before the stream attaches. */
export async function listAgentJobEvents(
  jobId: string,
  after = 0,
  limit = 500,
): Promise<{ events: AgentJobEventApi[]; next_cursor: number }> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/events?after=${after}&limit=${limit}`,
  );
  return jsonOrThrow(resp);
}

/** One stored event labelled with the conversation turn it belongs to. */
export interface AgentThreadEventApi extends AgentJobEventApi {
  job_id: string;
  turn_no: number;
}

/** Page through every turn's event log in append order. */
export async function listAgentThreadEvents(
  jobId: string,
  after = 0,
  limit = 1000,
): Promise<{ events: AgentThreadEventApi[]; next_cursor: number }> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/thread/events?after=${after}&limit=${limit}`,
  );
  return jsonOrThrow(resp);
}

/** Read a complete thread without imposing the server's page size on Chat. */
export async function listAllAgentThreadEvents(jobId: string): Promise<AgentThreadEventApi[]> {
  const events: AgentThreadEventApi[] = [];
  let cursor = 0;
  for (;;) {
    const page = await listAgentThreadEvents(jobId, cursor, 1000);
    events.push(...page.events);
    if (page.events.length < 1000 || page.next_cursor <= cursor) return events;
    cursor = page.next_cursor;
  }
}

/** Browse the retained container's live workspace. */
export async function getAgentJobFiles(jobId: string, path = ''): Promise<AgentJobFilesApi> {
  const query = path ? `?path=${encodeURIComponent(path)}` : '';
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/files${query}`,
  );
  return jsonOrThrow<AgentJobFilesApi>(resp);
}

/** Save one UTF-8 file in the live workspace. */
export async function writeAgentJobFile(
  jobId: string,
  path: string,
  content: string,
): Promise<AgentJobFileApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/files?path=${encodeURIComponent(path)}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content }),
    },
  );
  return jsonOrThrow<AgentJobFileApi>(resp);
}

export interface AgentTerminalApi {
  id: string;
  shell: string;
  state: string;
  cwd: string;
  rows: number;
  cols: number;
  last_seq: number;
}

/** List the PTY sessions that still belong to this workspace. */
export async function listAgentTerminals(jobId: string): Promise<AgentTerminalApi[]> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/terminals`,
  );
  const body = await jsonOrThrow<AgentTerminalApi[] | { terminals: AgentTerminalApi[] }>(resp);
  return Array.isArray(body) ? body : body.terminals;
}

/** Start an isolated shell that shares the job's retained workspace. */
export async function createAgentTerminal(
  jobId: string,
  size: { rows: number; cols: number } = { rows: 24, cols: 80 },
): Promise<AgentTerminalApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/terminals`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(size),
    },
  );
  return jsonOrThrow<AgentTerminalApi>(resp);
}

function terminalPath(jobId: string, terminalId: string): string {
  return `/v1/agent/jobs/${encodeURIComponent(jobId)}/terminals/${encodeURIComponent(terminalId)}`;
}

const MAX_TERMINAL_INPUT_BYTES = 64 * 1024;

function bytesToBase64(bytes: Uint8Array): string {
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

function utf8Chunks(value: string): Uint8Array[] {
  const encoded = new TextEncoder().encode(value);
  if (encoded.byteLength === 0) return [];
  const chunks: Uint8Array[] = [];
  let offset = 0;
  while (offset < encoded.byteLength) {
    let end = Math.min(offset + MAX_TERMINAL_INPUT_BYTES, encoded.byteLength);
    if (end < encoded.byteLength) {
      while (end > offset && (encoded[end] & 0xc0) === 0x80) end -= 1;
    }
    chunks.push(encoded.subarray(offset, end));
    offset = end;
  }
  return chunks;
}

/** Send keystrokes, including control sequences, to a running PTY. */
export async function writeAgentTerminalInput(
  jobId: string,
  terminalId: string,
  data: string,
): Promise<void> {
  for (const chunk of utf8Chunks(data)) {
    const resp = await fetchWithAuth(API_BASE, `${terminalPath(jobId, terminalId)}/input`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: bytesToBase64(chunk) }),
    });
    if (!resp.ok) await jsonOrThrow(resp);
  }
}

/** Tell the remote PTY its rendered character dimensions. */
export async function resizeAgentTerminal(
  jobId: string,
  terminalId: string,
  rows: number,
  cols: number,
): Promise<void> {
  const resp = await fetchWithAuth(API_BASE, `${terminalPath(jobId, terminalId)}/resize`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ rows, cols }),
  });
  if (!resp.ok) await jsonOrThrow(resp);
}

/** Kill a PTY and its process tree. */
export async function deleteAgentTerminal(jobId: string, terminalId: string): Promise<void> {
  const resp = await fetchWithAuth(API_BASE, terminalPath(jobId, terminalId), {
    method: 'DELETE',
  });
  if (!resp.ok) await jsonOrThrow(resp);
}

export interface AgentTerminalOutputEvent {
  seq: number;
  data: string;
}

export interface AgentTerminalExitEvent {
  seq: number;
  exit_code: number;
}

export interface AgentTerminalResetEvent {
  seq: number;
  reason: 'output_truncated';
}

export interface StreamAgentTerminalOptions {
  after?: number;
  signal?: AbortSignal;
  onOutput: (event: AgentTerminalOutputEvent) => void;
  onReset: (event: AgentTerminalResetEvent) => void;
  onExit: (event: AgentTerminalExitEvent) => void;
}

/**
 * Stream PTY output over authenticated SSE.
 *
 * `after` is the last applied sequence, so callers can resume a dropped
 * connection without replaying terminal bytes into xterm twice.
 */
export async function streamAgentTerminal(
  jobId: string,
  terminalId: string,
  options: StreamAgentTerminalOptions,
): Promise<void> {
  let cursor = Math.max(0, options.after ?? 0);
  let exited = false;
  let reconnectAttempts = 0;

  const dispatch = (frame: string): boolean => {
    let eventName = '';
    const dataLines: string[] = [];
    for (const line of frame.split('\n')) {
      if (line.startsWith('event:')) eventName = line.slice(6).trim();
      if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
    }
    if (!eventName || dataLines.length === 0) return false;
    try {
      const payload = JSON.parse(dataLines.join('\n')) as Record<string, unknown>;
      const seq = payload.seq;
      if (typeof seq !== 'number' || !Number.isSafeInteger(seq) || seq <= cursor) return false;
      if (eventName === 'output' && typeof payload.data === 'string') {
        cursor = seq;
        reconnectAttempts = 0;
        options.onOutput(payload as unknown as AgentTerminalOutputEvent);
        return true;
      } else if (eventName === 'reset' && payload.reason === 'output_truncated') {
        cursor = seq;
        reconnectAttempts = 0;
        options.onReset(payload as unknown as AgentTerminalResetEvent);
        return true;
      } else if (eventName === 'exit' && typeof payload.exit_code === 'number') {
        cursor = seq;
        exited = true;
        reconnectAttempts = 0;
        options.onExit(payload as unknown as AgentTerminalExitEvent);
        return true;
      }
    } catch {
      // One malformed frame must not discard the rest of a live shell stream.
    }
    return false;
  };

  const reconnectDelay = (delayMs: number) =>
    new Promise<void>((resolve) => {
      if (options.signal?.aborted) {
        resolve();
        return;
      }
      const finish = () => {
        clearTimeout(timer);
        options.signal?.removeEventListener('abort', finish);
        resolve();
      };
      const timer = setTimeout(finish, delayMs);
      options.signal?.addEventListener('abort', finish, { once: true });
    });

  const waitToReconnect = async (): Promise<boolean> => {
    if (options.signal?.aborted) return false;
    const delayMs = Math.min(100 * 2 ** reconnectAttempts, 5_000);
    reconnectAttempts = Math.min(reconnectAttempts + 1, 6);
    await reconnectDelay(delayMs);
    return !options.signal?.aborted;
  };

  while (!exited && !options.signal?.aborted) {
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    let reconnectable = true;
    try {
      const resp = await fetchWithAuth(
        API_BASE,
        `${terminalPath(jobId, terminalId)}/stream?after=${cursor}`,
        { headers: { Accept: 'text/event-stream' }, signal: options.signal },
      );
      if (!resp.ok) {
        reconnectable = false;
        await jsonOrThrow(resp);
        return;
      }
      if (!resp.body) {
        reconnectable = false;
        throw new Error('Terminal stream response has no body');
      }

      reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      while (!exited) {
        const { done, value } = await reader.read();
        if (done) {
          buffer += decoder.decode();
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const boundaryPattern = /\r?\n\r?\n/;
        let boundary = boundaryPattern.exec(buffer);
        while (boundary) {
          dispatch(buffer.slice(0, boundary.index));
          buffer = buffer.slice(boundary.index + boundary[0].length);
          boundary = boundaryPattern.exec(buffer);
        }
      }
      if (buffer.trim()) dispatch(buffer);
    } catch (cause) {
      if (options.signal?.aborted) return;
      if (!reconnectable) throw cause;
      if (cause instanceof Error && cause.name !== 'AbortError') {
        if (!(await waitToReconnect())) return;
        continue;
      }
      return;
    } finally {
      if (reader) {
        try {
          await reader.cancel();
        } catch {
          // Abort and network failures may already have errored the body.
        }
        reader.releaseLock();
      }
    }

    if (!exited && !options.signal?.aborted && !(await waitToReconnect())) return;
  }
}

export interface AgentGitWorkspaceApi {
  available: boolean;
  branch: string;
  changes: Array<{ code: string; path: string }>;
  patch: string;
  commits: Array<{
    sha: string;
    short_sha: string;
    subject: string;
    author: string;
    authored_at: string;
  }>;
}

/** Read Git status/diff/commits from the live workspace. */
export async function getAgentJobGit(jobId: string): Promise<AgentGitWorkspaceApi> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/git`);
  return jsonOrThrow<AgentGitWorkspaceApi>(resp);
}

export interface StreamAgentJobOptions {
  onEvent: (event: AgentJobEventApi) => void;
  onFinished?: (info: { state: string; published_pr_url: string | null }) => void;
  onError?: (error: unknown) => void;
  /** Resume point; frames with an id at or below this are not re-sent. */
  lastEventId?: number;
  signal?: AbortSignal;
}

interface ParsedFrame {
  event: string;
  data: string;
  id?: number;
}

function parseFrame(raw: string): ParsedFrame | null {
  let event = 'message';
  let id: number | undefined;
  const dataLines: string[] = [];
  for (const line of raw.split(/\r?\n/)) {
    // Comment frame (the server's keepalive) — carries no data by design.
    if (line.startsWith(':')) continue;
    if (line.startsWith('event:')) event = line.slice(6).trim();
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
    else if (line.startsWith('id:')) {
      const parsed = Number(line.slice(3).trim());
      if (Number.isFinite(parsed)) id = parsed;
    }
  }
  if (dataLines.length === 0) return null;
  return { event, data: dataLines.join('\n'), id };
}

/**
 * Stream a job's events, reconnecting from the last id until it finishes.
 *
 * Reconnection is expected behaviour rather than error handling: the gateway
 * caps long-lived streams, so a job running longer than that cap will drop a
 * connection through no fault of anyone's. The cursor makes that invisible.
 */
export async function streamAgentJob(jobId: string, opts: StreamAgentJobOptions): Promise<void> {
  let cursor = opts.lastEventId ?? 0;
  let finished = false;

  while (!finished) {
    if (opts.signal?.aborted) return;

    let resp: Response;
    try {
      resp = await fetchWithAuth(
        API_BASE,
        `/v1/agent/jobs/${encodeURIComponent(jobId)}/stream?after=${cursor}`,
        { headers: { Accept: 'text/event-stream' }, signal: opts.signal },
      );
    } catch (err) {
      if (opts.signal?.aborted) return;
      opts.onError?.(err);
      return;
    }

    if (!resp.ok || !resp.body) {
      await jsonOrThrow(resp).catch((err) => opts.onError?.(err));
      return;
    }

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        const boundary = /\r?\n\r?\n/;
        let match: RegExpExecArray | null;
        while ((match = boundary.exec(buffer))) {
          const raw = buffer.slice(0, match.index);
          buffer = buffer.slice(match.index + match[0].length);
          const frame = parseFrame(raw);
          if (!frame) continue;

          if (frame.event === 'job_finished') {
            finished = true;
            try {
              opts.onFinished?.(JSON.parse(frame.data));
            } catch {
              opts.onFinished?.({ state: 'unknown', published_pr_url: null });
            }
            return;
          }

          try {
            const parsed = JSON.parse(frame.data) as AgentJobEventApi;
            // Track the cursor from the frame id so a reconnect resumes
            // exactly here, with no gap and no duplicate.
            cursor = frame.id ?? parsed.id ?? cursor;
            opts.onEvent(parsed);
          } catch {
            // A frame we cannot parse is dropped rather than aborting the
            // stream: losing one row beats losing the rest of the job.
          }
        }
      }
    } catch (err) {
      if (opts.signal?.aborted) return;
      // A mid-stream drop is the expected shape when the server's stream cap
      // fires. Reconnect from the cursor instead of surfacing an error.
      opts.onError?.(err);
    } finally {
      reader.releaseLock();
    }
  }
}

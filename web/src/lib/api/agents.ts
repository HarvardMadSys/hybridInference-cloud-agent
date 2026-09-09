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
import { config } from '@/config/env';

const API_BASE = config.apiBase;

/** Server-side job states. The UI maps these onto its own display states. */
export type AgentJobApiState =
  | 'waiting'
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
}

export interface AgentAttemptSummaryApi {
  attempt_id: number;
  attempt_no: number;
  continuity_mode: 'hot' | 'warm_native' | 'cold_native' | 'replay' | null;
  context_status: 'new' | 'live' | 'restored' | 'lost' | null;
  duration_ms: number | null;
}

export interface AgentAttemptTelemetryApi extends AgentAttemptSummaryApi {
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
  };
}

export interface AgentJobApi {
  id: string;
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

export interface AgentConfigApi {
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

export interface CreateAgentJobRequest {
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

/** Hide the entire conversation containing this job from active task history. */
export async function archiveAgentJob(jobId: string): Promise<AgentThreadArchiveApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/archive`,
    {
      method: 'POST',
    },
  );
  return jsonOrThrow(resp);
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
  return jsonOrThrow(resp);
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
  return jsonOrThrow(resp);
}

/** Return a pinned conversation to normal activity ordering. */
export async function unpinAgentJob(jobId: string): Promise<AgentThreadPinApi> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/pin`, {
    method: 'DELETE',
  });
  return jsonOrThrow(resp);
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
  const resp = await fetchWithAuth(API_BASE, '/v1/agent/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  return jsonOrThrow<AgentJobApi>(resp);
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
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/follow-ups`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
  );
  return jsonOrThrow<AgentJobApi>(resp);
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

export async function forkAgentJob(jobId: string): Promise<AgentJobApi> {
  const requestId = pendingForkRequestId(jobId);
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
      clearPendingForkRequest(jobId);
    }
    const result = await jsonOrThrow<AgentJobApi>(resp);
    clearPendingForkRequest(jobId);
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

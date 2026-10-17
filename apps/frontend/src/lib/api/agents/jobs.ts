import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';
import type { SelectedSkillApi } from '../skills';
import type { GoalApi, GoalInputApi, GoalReportOutcome, GoalReportParse } from './goals';
import type { AgentAttemptSummaryApi, AttemptResourcesApi } from './resources';
import { responseErrorType, submitOnce } from './idempotency';

const API_BASE = config.apiBase;

/** Server-side job states. The UI maps these onto its own display states. */
export type AgentJobApiState =
  | 'queued'
  | 'running'
  | 'publishing'
  | 'succeeded'
  | 'failed'
  | 'cancelled';

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
  /** Null for a temporary workspace, which has no repository. */
  repo: string | null;
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
  /** Raised by one on every change to this turn's row (absent on older servers). */
  revision?: number;
  /** The same counter for the conversation's row: title, pin, archive. */
  thread_revision?: number;
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
  /** Whether a waiting message can be sent into this job's turn while it runs. */
  mid_turn_input_supported?: boolean;
  workspace_publication_state?:
    | 'pending'
    | 'capturing'
    | 'captured'
    | 'publishing'
    | 'published'
    | 'failed'
    | null;
  /** The newest publication found nothing to publish and opened no PR. */
  workspace_publication_unchanged?: boolean;
  /** The pull request the workspace was last published to. */
  workspace_pr_url?: string | null;
  /** Turns the owner has not published, or a last publication that failed. */
  workspace_unpublished_changes?: boolean;
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

export interface AgentPluginInputApi {
  /** Native name@marketplace (Claude/Codex), or an npm package (pi/OpenCode/Kilo). */
  plugin: string;
  /** GitHub owner/repo or a credential-free HTTPS Git URL for native plugins. */
  marketplace?: string;
}

export interface CreateAgentJobRequest {
  environment_id?: string | null;
  environment_revision?: number;
  global_revision?: number;
  /** Installed before the first agent turn in this conversation's sandbox. */
  plugins?: AgentPluginInputApi[];
  /** Omitted uses personal defaults; an empty list selects none. */
  skill_ids?: number[];
  /** Staged uploads to place under /uploads. Omitted attaches nothing. */
  attachment_ids?: number[];
  /** MCP connections this task may call. Omitted gives it none. */
  mcp_bindings?: McpBindingInputApi[];
  /** The owner's checked SSH servers this chat can reach. Omitted gives it none. */
  ssh_server_ids?: string[];
  /** Omitted means no goal: nothing continues without the owner asking again. */
  goal?: GoalInputApi;
  /** Null starts a temporary workspace: an empty sandbox whose work is never published. */
  repo: string | null;
  task_prompt: string;
  model: string;
  runtime?: string;
  /** Omitted is Auto: the runtime and model decide for themselves. */
  reasoning_effort?: string;
  /** A branch. The platform pins it to a commit at creation. */
  base_ref?: string;
  base_sha?: string;
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

/**
 * ``account`` scopes the pending key to the signed-in account. A key outlives
 * a lost response so a retry can recover what the first send made, and the
 * control plane matches it per account: another account signed in to this tab
 * must neither reuse it nor retire it.
 */
export async function createAgentJob(
  body: CreateAgentJobRequest,
  options: { account?: string } = {},
): Promise<AgentJobApi> {
  const request = { ...body, task_prompt: body.task_prompt.trim() };
  const scope = options.account ? `create:${options.account}` : 'create';
  return submitOnce(scope, (requestId) =>
    fetchWithAuth(API_BASE, '/v1/agent/jobs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': requestId },
      body: JSON.stringify(request),
    }),
  );
}

export async function cancelAgentJob(
  jobId: string,
): Promise<{ id: string; state: string; cancel_requested: boolean }> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST',
  });
  return jsonOrThrow(resp);
}

/** The workspace snapshot a publication request opened, or the one already on its way. */
export interface AgentThreadPublicationApi {
  thread_id: string;
  snapshot_id: number;
  state: 'pending' | 'capturing' | 'captured' | 'publishing';
  /** False when a publication of the workspace was already on its way. */
  requested: boolean;
}

/**
 * Push the conversation's workspace to its branch and open or update its
 * draft PR. The platform never publishes on its own; this is the owner asking.
 */
export async function publishAgentThread(threadId: string): Promise<AgentThreadPublicationApi> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/threads/${encodeURIComponent(threadId)}/publish`,
    { method: 'POST' },
  );
  return jsonOrThrow(resp);
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
    if ((await responseErrorType(resp)) === 'native_fork_failed') {
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

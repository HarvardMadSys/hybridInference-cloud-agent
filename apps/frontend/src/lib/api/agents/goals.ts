import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

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

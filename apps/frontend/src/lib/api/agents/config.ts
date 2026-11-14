import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';
import type { GoalLimitsApi } from './goals';
import type { SourceControlLoadStatus } from './integrations';

const API_BASE = config.apiBase;

/** One of the owner's enabled connections, as a composer offers it. */
export interface ModelConnectionOptionApi {
  id: string;
  name: string;
  /** Every listed model, in the owner's order. */
  models: string[];
  /**
   * The listed models a task can run now, each with the agents that can run
   * it: exactly what the server accepts. A model missing here is not
   * verified, or no agent can use it.
   */
  model_runtimes: Record<string, string[]>;
  /**
   * For each model in `model_runtimes`, per agent: the reasoning levels a task
   * may pick besides Auto, and whether the provider declared them. Absent on a
   * control plane that predates it, which offers Auto alone.
   */
  model_efforts?: Record<string, Record<string, ModelEffortOfferApi>>;
}

/** This owner's allowance on one provided model, for the current UTC day. */
export interface ProvidedModelAllowanceApi {
  /** Requests the owner may send today; `null` is unlimited. */
  daily_requests: number | null;
  /** Requests the deployment's relay has accepted from this owner today. */
  used_today: number;
}

/**
 * A connection the deployment provides to every admitted owner (`oconn_…`),
 * as a composer offers it. The same shape as an owner's own connection, plus
 * this owner's allowances. Only the models this owner may use are listed.
 */
export interface ProvidedModelSourceApi extends ModelConnectionOptionApi {
  /** Whether a new owner with no history starts here. */
  is_default: boolean;
  /** The model a new owner starts on, when this owner may use it. */
  default_model: string | null;
  /** Per listed model: this owner's allowance and today's use. */
  allowances: Record<string, ProvidedModelAllowanceApi>;
  /** When today's counts start over: the next midnight UTC, as ISO time. */
  resets_at: string;
}

/**
 * What the model's provider declared for the API one agent's turn calls.
 * `supported`: `levels` are the declared ones that agent can send, weakest
 * first. `unsupported`: none (declared, or none the agent can send).
 * `unknown`: the provider did not say. Auto is always available besides.
 */
export interface ModelEffortOfferApi {
  status: 'supported' | 'unsupported' | 'unknown';
  levels: string[];
  /**
   * `not_read`, `listing_unavailable`, `not_listed`, `not_declared`,
   * `conflicting_declarations`, `agent_cannot_express`.
   */
  reason?: string;
  source?: string;
}

export interface AgentConfigApi {
  environment_limits?: Record<string, unknown>;
  skill_limits?: import('@/lib/skillLimits').SkillLimits;
  goal_limits?: GoalLimitsApi;

  /** Upload bounds. Absent on a control plane that predates attachments. */
  attachment_limits?: import('@/lib/attachmentLimits').AttachmentLimits;
  repos: string[];
  runtimes: string[];
  /** Runtimes that can be given MCP servers; others refuse such a job. */
  mcp_runtimes?: string[];
  /** Runtimes that install task-selected plugins when a sandbox is initialized. */
  plugin_runtimes?: string[];
  /**
   * Effort levels each runtime can express, in its own vocabulary. A runtime
   * absent here cannot express one at all.
   */
  runtime_reasoning_efforts?: Record<string, string[]>;
  /**
   * What a new task should start on: the model of this owner's newest turn,
   * while its connection still lists it. Null leaves the model control empty
   * until the owner chooses, rather than picking the first model in the list.
   */
  default_model?: string | null;
  /** The source `default_model` ran on: the owner's connection or a provided one. */
  default_model_connection_id?: string | null;
  /** This owner's enabled model connections. Absent before the feature. */
  model_connections?: ModelConnectionOptionApi[];
  /**
   * Connections this deployment provides, each with this owner's allowance. A
   * model chosen from one is submitted with the source's id as
   * `model_connection_id`. Absent on a control plane that predates them.
   */
  provided_sources?: ProvidedModelSourceApi[];
  /** Whether this deployment lets owners add and run on their own connections. */
  model_connections_enabled?: boolean;
  /**
   * Neither an enabled connection of the owner's nor a provided one can run
   * anything yet; setup is gated separately.
   */
  model_connection_required?: boolean;
  /**
   * Whether this user has done what the deployment asks before choosing a
   * repository: connected GitHub where users can, or been given a repository by
   * the operator's allowlist where they cannot. `repos` may still be empty — a
   * connection without repository access, not a missing connection.
   */
  github_connected: boolean;
  github_install_url: string | null;
  /**
   * Whether this deployment's GitHub App is working right now. `unavailable`
   * means configured but not loaded, and the service is retrying on its own.
   * Absent on a control plane that predates it, which then reads as before.
   */
  github_load_status?: SourceControlLoadStatus;
}

/**
 * What this deployment will actually accept — the source for the pickers.
 *
 * `fresh` asks GitHub again for the user's repositories instead of reusing the
 * service's minute-old answer: for a repository just given to the App.
 */
export async function getAgentConfig(options: { fresh?: boolean } = {}): Promise<AgentConfigApi> {
  const query = options.fresh ? '?fresh=true' : '';
  return jsonOrThrow(await fetchWithAuth(API_BASE, `/v1/agent/config${query}`));
}

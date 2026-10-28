import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';
import type { GoalLimitsApi } from './goals';
import type { SourceControlLoadStatus } from './integrations';

const API_BASE = config.apiBase;

/** One of the owner's enabled connections, as a composer offers it. */
export interface ModelConnectionOptionApi {
  id: string;
  name: string;
  models: string[];
  /** Every agent that can run on this connection, natively or translated. */
  runtimes: string[];
  /**
   * Those of `runtimes` that reach it through the compatibility helper (Codex
   * or Claude Code on a Chat Completions connection). Absent before it existed.
   */
  translated_runtimes?: string[];
}

export interface AgentConfigApi {
  environment_limits?: Record<string, unknown>;
  skill_limits?: import('@/lib/skillLimits').SkillLimits;
  goal_limits?: GoalLimitsApi;

  /** Upload bounds. Absent on a control plane that predates attachments. */
  attachment_limits?: import('@/lib/attachmentLimits').AttachmentLimits;
  repos: string[];
  runtimes: string[];
  /** Models an agent job can actually call — the create endpoint's own list. */
  models: string[];
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
   * Effort levels each model accepts. Offer the intersection with the chosen
   * runtime's, and no control when it is empty — which is also what a backend
   * too old to answer these produces.
   */
  model_reasoning_efforts?: Record<string, string[]>;
  /**
   * What a new task should start on, by priority: this owner's pinned model,
   * the one their newest turn used, then the deployment default an
   * administrator saved. Always one of `models`, so a retired model is never
   * pre-selected. Null means none of the three resolves (or the catalog was
   * unreadable); the composer then leaves its model control empty and waits
   * for the owner to choose, rather than picking the first model in the list.
   */
  default_model?: string | null;
  /**
   * The source of `default_model`: the owner's connection it last ran on, or
   * null for the Gateway. When set, `default_model` is one of that
   * connection's models and need not be in `models`.
   */
  default_model_connection_id?: string | null;
  /** This owner's enabled model connections. Absent before the feature. */
  model_connections?: ModelConnectionOptionApi[];
  /** Whether this deployment lets owners add and run on their own connections. */
  model_connections_enabled?: boolean;
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

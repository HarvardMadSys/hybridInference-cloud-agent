import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';
import type { GoalLimitsApi } from './goals';
import type { SourceControlLoadStatus } from './integrations';

const API_BASE = config.apiBase;

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
  /** MCP servers this deployment offers. Never carries a URL or a credential. */
  mcp_servers?: AgentMcpServerApi[];
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

export interface AgentMcpServerApi {
  name: string;
  description: string;
  /** Tools exposed. Empty means every tool the server offers. */
  tools: string[];
  /** Applied when a job does not choose its servers explicitly. */
  default: boolean;
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

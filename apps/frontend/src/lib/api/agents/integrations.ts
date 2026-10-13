import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

export interface CreateGitHubRepositoryRequest {
  name: string;
  description?: string;
  private: boolean;
}

export interface CreatedGitHubRepository {
  repo: string;
  web_url: string;
  default_branch: string;
  /** Whether the connected GitHub App can already use the repository. */
  accessible: boolean;
}

/**
 * Create a personal GitHub repository initialized with a README.
 *
 * A failed response can follow a successful creation on GitHub, so this
 * operation is never retried automatically. The caller must check GitHub
 * before repeating an ambiguous request.
 */
export async function createGitHubRepository(
  request: CreateGitHubRepositoryRequest,
): Promise<CreatedGitHubRepository> {
  const resp = await fetchWithAuth(API_BASE, '/v1/agent/repositories', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  });
  return jsonOrThrow(resp);
}

export type AgentIntegrationProvider = 'github' | 'gitlab';

/** The deployment's side of a provider, not the user's connection to it. */
export type SourceControlLoadStatus = 'unconfigured' | 'unavailable' | 'ready';

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
  /**
   * `unavailable`: configured, not loaded, retrying. The user's own
   * connection (`connected`, `accounts`) is still reported as it is.
   */
  load_status?: SourceControlLoadStatus;
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

/**
 * Forget this user's GitHub connection — the stored token, the account, every
 * installation — and ask GitHub to withdraw the App's authorization. Answers
 * with the page's whole state.
 */
export async function disconnectGitHubAccount(): Promise<AgentIntegrationsApi> {
  const resp = await fetchWithAuth(API_BASE, '/v1/agent/integrations/github', {
    method: 'DELETE',
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

export interface RepoBranchesApi {
  default: string | null;
  branches: string[];
}

/** Branches of one entitled repository, for the composer's branch picker. */
export async function listRepoBranches(repo: string): Promise<RepoBranchesApi> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/branches?repo=${encodeURIComponent(repo)}`);
  return jsonOrThrow(resp);
}

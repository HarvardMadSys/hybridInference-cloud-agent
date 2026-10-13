import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';
import { announceAgentSessionsChanged } from '../agentSessionEvents';
import type { AgentJobApi } from './jobs';

const API_BASE = config.apiBase;

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
  delete_block_reason?: 'fork_in_progress' | 'publication_pending' | null;
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

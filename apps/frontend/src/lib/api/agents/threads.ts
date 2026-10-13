import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';
import type { PendingMessageApi, QueuePausedReason } from '../pendingMessages';
import type { AgentJobApi } from './jobs';

const API_BASE = config.apiBase;

export interface AgentThreadMessageApi {
  id: number;
  role: 'user' | 'assistant';
  content: string;
  job_id: string;
  created_at: string | null;
  /**
   * A message the owner sent into this job's turn while it ran, not the prompt
   * that started it. It reads among the turn's replies, where it was sent.
   */
  mid_turn?: boolean;
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
  /** Messages written while a turn ran, in the order they will be sent. */
  pending_messages?: PendingMessageApi[];
  /** Why the queue is not moving by itself, until the owner continues it. */
  queue_paused_reason?: QueuePausedReason | null;
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
  signal?: AbortSignal,
): Promise<{ conversations: AgentConversationApi[]; next_cursor: string | null }> {
  const query = new URLSearchParams({ limit: String(options.limit ?? 50) });
  if (options.q) query.set('q', options.q);
  if (options.repo) query.set('repo', options.repo);
  if (options.includeArchived) query.set('include_archived', 'true');
  if (options.pinned !== undefined) query.set('pinned', String(options.pinned));
  if (options.cursor) query.set('cursor', options.cursor);
  return jsonOrThrow(await fetchWithAuth(API_BASE, `/v1/agent/conversations?${query}`, { signal }));
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

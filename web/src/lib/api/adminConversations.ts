import { config } from '@/config/env';

import type { AdminRun, AdminRunFilters } from './adminRuns';
import { fetchWithAuth, jsonOrThrow } from './client';

export interface AdminConversation {
  thread_id: string;
  title: string | null;
  user_id: string;
  repo: string;
  created_at: string;
  last_activity_at: string;
  archived_at: string | null;
  deleted_at: string | null;
  state: string;
  turn_count: number;
  latest_job_id: string;
  latest_turn_no: number;
  latest_turn_state: string;
  latest_runtime: string;
  latest_model: string;
}

export interface AdminConversationMatch extends AdminConversation {
  matching_turn_count: number;
  last_matching_turn_at: string;
}

export interface AdminConversationsPage {
  conversations: AdminConversationMatch[];
  since: string;
  until: string;
  next_cursor: string | null;
}

export interface AdminConversationDetail {
  conversation: AdminConversation;
  turns: (AdminRun & { matches_filters: boolean; fork_source_job_id: string | null })[];
  next_cursor: string | null;
}

export function conversationQuery(filters: AdminRunFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== '') params.set(key, String(value));
  }
  return params.size ? `?${params}` : '';
}

export function conversationHref(threadId: string, filters: AdminRunFilters = {}): string {
  const context = { ...filters };
  delete context.cursor;
  delete context.limit;
  return `/admin/conversations/${encodeURIComponent(threadId)}${conversationQuery(context)}`;
}

async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  return jsonOrThrow<T>(await fetchWithAuth(config.apiBase, path, { signal, cache: 'no-store' }));
}

export function getAdminConversations(
  filters: AdminRunFilters,
  signal?: AbortSignal,
): Promise<AdminConversationsPage> {
  return read(`/admin/agent/conversations${conversationQuery(filters)}`, signal);
}

export function getAdminConversation(
  threadId: string,
  filters: AdminRunFilters = {},
  signal?: AbortSignal,
): Promise<AdminConversationDetail> {
  return read(
    `/admin/agent/conversations/${encodeURIComponent(threadId)}${conversationQuery(filters)}`,
    signal,
  );
}

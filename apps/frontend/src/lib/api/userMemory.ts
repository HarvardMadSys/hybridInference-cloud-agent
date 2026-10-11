import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

const USER_MEMORY = '/v1/agent/user-memory';

export interface UserPortrait {
  summary: string;
  projects: string[];
  focus_areas: string[];
  preferred_runtime: string | null;
  preferred_model: string | null;
}

export interface UserMemoryEntry {
  id: string;
  kind: 'preference' | 'task';
  content: string;
  source_job_id: string;
  created_at: string | null;
}

export interface UserMemorySuggestion {
  id: string;
  title: string;
  reason: string;
  action: string;
}

export interface UserMemoryView {
  enabled: boolean;
  revision: number;
  updated_at: string | null;
  avatar: { data_url: string; style: 'illustrated' };
  portrait: UserPortrait;
  memories: UserMemoryEntry[];
  suggestions: UserMemorySuggestion[];
}

/** Reading refreshes the current user's profile from their recent activity. */
export async function getUserMemory(signal?: AbortSignal): Promise<UserMemoryView> {
  const response = await fetchWithAuth(config.apiBase, USER_MEMORY, {
    signal,
    cache: 'no-store',
  });
  return jsonOrThrow<UserMemoryView>(response);
}

export async function updateUserMemory(
  enabled: boolean,
  signal?: AbortSignal,
): Promise<UserMemoryView> {
  const response = await fetchWithAuth(config.apiBase, USER_MEMORY, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
    signal,
  });
  return jsonOrThrow<UserMemoryView>(response);
}

export async function clearUserMemory(signal?: AbortSignal): Promise<UserMemoryView> {
  const response = await fetchWithAuth(config.apiBase, USER_MEMORY, {
    method: 'DELETE',
    signal,
  });
  return jsonOrThrow<UserMemoryView>(response);
}

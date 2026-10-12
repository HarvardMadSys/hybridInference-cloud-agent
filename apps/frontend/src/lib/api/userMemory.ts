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
  source_repo: string | null;
  user_edited: boolean;
  scope?: 'repo' | 'user';
  edited?: boolean;
  created_at: string | null;
}

export interface UserMemoryEdit {
  id: string;
  content: string;
  scope: 'repo' | 'user';
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

/** Reading refreshes the current user's profile and cross-project preferences. */
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

export async function editUserMemoryEntry(
  id: string,
  content: string,
  expectedRevision: number,
  signal?: AbortSignal,
): Promise<UserMemoryView> {
  const response = await fetchWithAuth(
    config.apiBase,
    `${USER_MEMORY}/entries/${encodeURIComponent(id)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, expected_revision: expectedRevision }),
      signal,
    },
  );
  return jsonOrThrow<UserMemoryView>(response);
}

export async function deleteUserMemoryEntry(
  id: string,
  expectedRevision: number,
  signal?: AbortSignal,
): Promise<UserMemoryView> {
  const response = await fetchWithAuth(
    config.apiBase,
    `${USER_MEMORY}/entries/${encodeURIComponent(id)}`,
    {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ expected_revision: expectedRevision }),
      signal,
    },
  );
  return jsonOrThrow<UserMemoryView>(response);
}

export async function saveUserMemories(
  revision: number,
  memories: UserMemoryEdit[],
  signal?: AbortSignal,
): Promise<UserMemoryView> {
  const response = await fetchWithAuth(config.apiBase, USER_MEMORY, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision, memories }),
    signal,
  });
  return jsonOrThrow<UserMemoryView>(response);
}

export async function consolidateUserMemory(
  revision: number,
  model?: string,
  signal?: AbortSignal,
): Promise<UserMemoryView> {
  const response = await fetchWithAuth(config.apiBase, `${USER_MEMORY}/consolidate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision, model }),
    signal,
  });
  return jsonOrThrow<UserMemoryView>(response);
}

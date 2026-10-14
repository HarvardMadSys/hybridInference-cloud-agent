import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

const MEMORIES = '/v1/agent/memories';

/** One lasting rule agents follow. `repo` is null for every project. */
export interface AgentMemory {
  id: string;
  repo: string | null;
  content: string;
  description: string;
  status: 'active' | 'archived';
  /** `agent`: an agent saved it during a task. `manual`: the owner wrote or edited it. */
  origin: 'agent' | 'manual';
  source_job_ids: string[];
  version: number;
  created_at: string;
  updated_at: string;
  last_used_at: string | null;
  archived_at: string | null;
}

/** The memories of one scope: the user (`repo` null) or one repository. */
export interface AgentMemoryScope {
  repo: string | null;
  enabled: boolean;
  memories: AgentMemory[];
  archived: AgentMemory[];
}

function json(method: string, body: unknown, signal?: AbortSignal): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  };
}

/** Succeed on an empty answer; otherwise raise the API's error. */
async function emptyOrThrow(response: Response): Promise<void> {
  if (!response.ok) await jsonOrThrow<never>(response);
}

function scopeQuery(repo: string | null): string {
  return repo ? `?${new URLSearchParams({ repo })}` : '';
}

export async function getMemories(
  repo: string | null,
  signal?: AbortSignal,
): Promise<AgentMemoryScope> {
  const response = await fetchWithAuth(config.apiBase, `${MEMORIES}${scopeQuery(repo)}`, {
    signal,
    cache: 'no-store',
  });
  return jsonOrThrow<AgentMemoryScope>(response);
}

export async function addMemory(
  repo: string | null,
  content: string,
  signal?: AbortSignal,
): Promise<AgentMemory> {
  const response = await fetchWithAuth(
    config.apiBase,
    MEMORIES,
    json('POST', { repo, content }, signal),
  );
  return jsonOrThrow<AgentMemory>(response);
}

export async function updateMemory(
  memory: AgentMemory,
  content: string,
  repo: string | null,
  signal?: AbortSignal,
): Promise<AgentMemory> {
  const response = await fetchWithAuth(
    config.apiBase,
    `${MEMORIES}/${encodeURIComponent(memory.id)}`,
    json('PATCH', { content, repo, expected_version: memory.version }, signal),
  );
  return jsonOrThrow<AgentMemory>(response);
}

export async function deleteMemory(memory: AgentMemory, signal?: AbortSignal): Promise<void> {
  const response = await fetchWithAuth(
    config.apiBase,
    `${MEMORIES}/${encodeURIComponent(memory.id)}`,
    json('DELETE', { expected_version: memory.version }, signal),
  );
  await emptyOrThrow(response);
}

export async function restoreMemory(
  memory: AgentMemory,
  signal?: AbortSignal,
): Promise<AgentMemory> {
  const response = await fetchWithAuth(
    config.apiBase,
    `${MEMORIES}/${encodeURIComponent(memory.id)}/restore`,
    json('POST', { expected_version: memory.version }, signal),
  );
  return jsonOrThrow<AgentMemory>(response);
}

export async function clearMemories(repo: string | null, signal?: AbortSignal): Promise<void> {
  const response = await fetchWithAuth(config.apiBase, `${MEMORIES}${scopeQuery(repo)}`, {
    method: 'DELETE',
    signal,
  });
  await emptyOrThrow(response);
}

export async function setMemoryEnabled(
  repo: string | null,
  enabled: boolean,
  signal?: AbortSignal,
): Promise<AgentMemoryScope> {
  const response = await fetchWithAuth(
    config.apiBase,
    `${MEMORIES}/settings`,
    json('PUT', { repo, enabled }, signal),
  );
  return jsonOrThrow<AgentMemoryScope>(response);
}

/** Repositories the owner worked in or keeps memories for. */
export async function getMemoryProjects(signal?: AbortSignal): Promise<string[]> {
  const response = await fetchWithAuth(config.apiBase, `${MEMORIES}/projects`, {
    signal,
    cache: 'no-store',
  });
  return (await jsonOrThrow<{ projects: string[] }>(response)).projects;
}

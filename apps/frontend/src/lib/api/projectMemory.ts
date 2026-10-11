import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';
import type {
  UserMemoryEdit,
  UserMemoryEntry,
  UserMemorySuggestion,
  UserPortrait,
} from './userMemory';

const PROJECT_MEMORY = '/v1/agent/project-memory';

export interface ProjectMemoryView {
  enabled: boolean;
  revision: number;
  updated_at: string | null;
  repo: string;
  portrait: UserPortrait;
  memories: UserMemoryEntry[];
  suggestions: UserMemorySuggestion[];
}

function projectPath(repo: string) {
  return `${PROJECT_MEMORY}?repo=${encodeURIComponent(repo)}`;
}

function projectEntryPath(repo: string, id: string) {
  return `${PROJECT_MEMORY}/entries/${encodeURIComponent(id)}?repo=${encodeURIComponent(repo)}`;
}

export async function getMemoryProjects(signal?: AbortSignal): Promise<string[]> {
  const response = await fetchWithAuth(config.apiBase, `${PROJECT_MEMORY}/projects`, {
    signal,
    cache: 'no-store',
  });
  return jsonOrThrow<string[]>(response);
}

export async function getProjectMemory(
  repo: string,
  signal?: AbortSignal,
): Promise<ProjectMemoryView> {
  const response = await fetchWithAuth(config.apiBase, projectPath(repo), {
    signal,
    cache: 'no-store',
  });
  return jsonOrThrow<ProjectMemoryView>(response);
}

export async function updateProjectMemory(
  repo: string,
  enabled: boolean,
  signal?: AbortSignal,
): Promise<ProjectMemoryView> {
  const response = await fetchWithAuth(config.apiBase, projectPath(repo), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
    signal,
  });
  return jsonOrThrow<ProjectMemoryView>(response);
}

export async function clearProjectMemory(
  repo: string,
  signal?: AbortSignal,
): Promise<ProjectMemoryView> {
  const response = await fetchWithAuth(config.apiBase, projectPath(repo), {
    method: 'DELETE',
    signal,
  });
  return jsonOrThrow<ProjectMemoryView>(response);
}

export async function editProjectMemoryEntry(
  repo: string,
  id: string,
  content: string,
  expectedRevision: number,
  signal?: AbortSignal,
): Promise<ProjectMemoryView> {
  const response = await fetchWithAuth(config.apiBase, projectEntryPath(repo, id), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content, expected_revision: expectedRevision }),
    signal,
  });
  return jsonOrThrow<ProjectMemoryView>(response);
}

export async function deleteProjectMemoryEntry(
  repo: string,
  id: string,
  expectedRevision: number,
  signal?: AbortSignal,
): Promise<ProjectMemoryView> {
  const response = await fetchWithAuth(config.apiBase, projectEntryPath(repo, id), {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_revision: expectedRevision }),
    signal,
  });
  return jsonOrThrow<ProjectMemoryView>(response);
}

export async function saveProjectMemories(
  repo: string,
  revision: number,
  memories: UserMemoryEdit[],
  signal?: AbortSignal,
): Promise<ProjectMemoryView> {
  const response = await fetchWithAuth(config.apiBase, projectPath(repo), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ revision, memories }),
    signal,
  });
  return jsonOrThrow<ProjectMemoryView>(response);
}

export async function consolidateProjectMemory(
  repo: string,
  revision: number,
  model?: string,
  signal?: AbortSignal,
): Promise<ProjectMemoryView> {
  const response = await fetchWithAuth(
    config.apiBase,
    `${PROJECT_MEMORY}/consolidate?repo=${encodeURIComponent(repo)}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ revision, model }),
      signal,
    },
  );
  return jsonOrThrow<ProjectMemoryView>(response);
}

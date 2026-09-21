import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

export interface SelectedSkillApi {
  id: number;
  name: string;
  description: string;
  revision: number;
}

export interface SkillSummaryApi extends SelectedSkillApi {
  content_revision?: number;
  instruction_bytes?: number;
  source: 'created' | 'uploaded' | 'github';
  source_url: string | null;
  source_revision: string | null;
  default_enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface SkillApi extends SkillSummaryApi {
  instructions: string;
}

export interface SkillContent {
  name: string;
  description: string;
  instructions: string;
}

export interface SkillImportPreviewApi extends SkillContent {
  source: 'uploaded' | 'github';
  source_url: string | null;
  source_revision: string | null;
  content_digest: string | null;
}

const PATH = '/v1/agent/skills';

async function request<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const json = body === undefined ? undefined : JSON.stringify(body);
  const response = await fetchWithAuth(config.apiBase, path, {
    method,
    ...(json === undefined ? {} : { headers: { 'Content-Type': 'application/json' }, body: json }),
  });
  return jsonOrThrow<T>(response);
}

export async function listSkills(): Promise<SkillSummaryApi[]> {
  return (await request<{ skills: SkillSummaryApi[] }>(PATH)).skills;
}

export function getSkill(id: number): Promise<SkillApi> {
  return request<SkillApi>(`${PATH}/${id}`);
}

export function createSkill(
  body: SkillContent & { source?: 'created' | 'uploaded'; default_enabled: boolean },
): Promise<SkillApi> {
  return request<SkillApi>(PATH, 'POST', body);
}

export function updateSkill(
  id: number,
  body: Partial<SkillContent> & { revision: number; default_enabled?: boolean },
): Promise<SkillApi> {
  return request<SkillApi>(`${PATH}/${id}`, 'PATCH', body);
}

export async function deleteSkill(id: number, revision: number): Promise<void> {
  const response = await fetchWithAuth(config.apiBase, `${PATH}/${id}?revision=${revision}`, {
    method: 'DELETE',
  });
  if (!response.ok) await jsonOrThrow(response);
}

export function previewSkillImport(
  body: { url: string } | { content: string },
): Promise<SkillImportPreviewApi> {
  return request<SkillImportPreviewApi>(`${PATH}/import/preview`, 'POST', body);
}

export function importGitHubSkill(body: {
  url: string;
  content_digest: string;
  name: string;
  default_enabled: boolean;
}): Promise<SkillApi> {
  return request<SkillApi>(`${PATH}/import`, 'POST', body);
}

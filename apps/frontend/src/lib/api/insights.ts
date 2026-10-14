import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

export interface WorkSuggestion {
  id: string;
  title: string;
  reason: string;
  action: string;
}

/** Derived from the owner's latest requests on every read; not memory. */
export interface WorkInsights {
  focus_areas: string[];
  suggestions: WorkSuggestion[];
  avatar: { data_url: string; style: 'illustrated' };
}

export async function getInsights(signal?: AbortSignal): Promise<WorkInsights> {
  const response = await fetchWithAuth(config.apiBase, '/v1/agent/insights', {
    signal,
    cache: 'no-store',
  });
  return jsonOrThrow<WorkInsights>(response);
}

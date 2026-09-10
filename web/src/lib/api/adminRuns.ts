import { config } from '@/config/env';

import type { AgentAttemptTelemetryApi, AgentJobApiState, AgentJobUsageApi } from './agents';
import { fetchWithAuth, jsonOrThrow } from './client';

const RUNS = '/admin/agent/runs';

/** Metadata only. User-authored content is retrieved through the audited endpoint. */
export interface AdminRun {
  id: string;
  title: string | null;
  thread_id: string | null;
  turn_no: number;
  user_id: string;
  repo: string;
  runtime: string;
  model: string;
  reasoning_effort: string | null;
  execution_profile: string;
  state: AgentJobApiState;
  created_at: string;
  updated_at: string;
  hosts: string[];
  tool_calls: number;
}

export interface AdminRunFilters {
  since?: string;
  until?: string;
  user_id?: string;
  state?: AgentJobApiState;
  runtime?: string;
  model?: string;
  host?: string;
  repo?: string;
  cursor?: string;
  limit?: number;
}

export interface AdminRunsPage {
  runs: AdminRun[];
  next_cursor: string | null;
  since: string;
  until: string;
}

export interface AdminAttempt {
  attempt_id: number;
  attempt_no: number;
  host: string | null;
  status: string;
  started_at: string;
  finished_at: string | null;
}

export interface AdminRunDetail {
  run: AdminRun;
  attempts: AdminAttempt[];
  usage: AgentJobUsageApi;
  capabilities: { llm_requests: false };
}

export interface AdminActivityEvent {
  id: number;
  attempt_id: number;
  attempt_no: number;
  seq: number;
  event_type: string;
  created_at: string;
  tool_use_id: string | null;
  tool_name: string | null;
  is_error: boolean | null;
  has_content: boolean;
}

export interface AdminActivityPage {
  events: AdminActivityEvent[];
  next_cursor: string | null;
}

export interface AdminEventContent {
  id: number;
  attempt_id: number;
  seq: number;
  event_type: string;
  created_at: string;
  payload: Record<string, unknown>;
  truncated: boolean;
}

function query(values: Record<string, string | number | undefined>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== '') params.set(key, String(value));
  }
  return params.size ? `?${params}` : '';
}

function runPath(jobId: string): string {
  return `${RUNS}/${encodeURIComponent(jobId)}`;
}

async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  return jsonOrThrow<T>(await fetchWithAuth(config.apiBase, path, { signal, cache: 'no-store' }));
}

export function getAdminRuns(
  filters: AdminRunFilters,
  signal?: AbortSignal,
): Promise<AdminRunsPage> {
  return read(`${RUNS}${query({ ...filters })}`, signal);
}

export function getAdminRun(jobId: string, signal?: AbortSignal): Promise<AdminRunDetail> {
  return read(runPath(jobId), signal);
}

export function getAdminRunActivity(
  jobId: string,
  filters: { cursor?: string; attempt_id?: number; limit?: number } = {},
  signal?: AbortSignal,
): Promise<AdminActivityPage> {
  return read(`${runPath(jobId)}/activity${query(filters)}`, signal);
}

export function getAdminAttemptResources(
  jobId: string,
  attemptId: number,
  signal?: AbortSignal,
): Promise<AgentAttemptTelemetryApi> {
  return read(`${runPath(jobId)}/attempts/${attemptId}/resources`, signal);
}

/** Explicit content access. The server records the actor and event before returning it. */
export async function revealAdminEventContent(
  jobId: string,
  eventId: number,
  signal?: AbortSignal,
): Promise<AdminEventContent> {
  return jsonOrThrow<AdminEventContent>(
    await fetchWithAuth(config.apiBase, `${runPath(jobId)}/events/${eventId}/content`, {
      method: 'POST',
      signal,
      cache: 'no-store',
    }),
  );
}

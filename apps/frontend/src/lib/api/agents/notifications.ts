import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from '../client';

const API_BASE = config.apiBase;

export interface AgentNotificationJobApi {
  id: string;
  thread_id: string;
  title: string;
  repo: string | null;
  state: 'queued' | 'waiting' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  current_attempt_id: number | null;
  turn_no: number;
  revision: number;
  cancel_requested: boolean;
  finished_at: string | null;
  updated_at: string | null;
}

export interface AgentNotificationThreadApi {
  id: string;
  title: string;
  revision: number;
}

export interface AgentNotificationStateApi {
  mode: 'snapshot' | 'delta';
  jobs: AgentNotificationJobApi[];
  threads: AgentNotificationThreadApi[];
  deleted_job_ids: string[];
  deleted_thread_ids: string[];
  observed_at: string;
  next_cursor: string | null;
  change_cursor: string | null;
  reset_required: boolean;
}

/** Bootstrap active turns once, then read only durable owner changes. */
export async function getAgentNotificationState(
  cursor?: string,
  limit = 200,
): Promise<AgentNotificationStateApi> {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor) query.set('cursor', cursor);
  return jsonOrThrow(await fetchWithAuth(API_BASE, `/v1/agent/notifications/state?${query}`));
}

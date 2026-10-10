import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from './client';

export interface AdminSandboxResourceUsage {
  source: 'docker-stats';
  scope: 'container';
  sample_id: string;
  sampled_at: string;
  received_at: string;
  age_seconds: number;
  stale: boolean;
  cpu_cores: number | null;
  cpu_window_seconds: number | null;
  memory_bytes: number | null;
  memory_limit_bytes: number | null;
  pids: number | null;
  io_read_bytes: number | null;
  io_write_bytes: number | null;
  net_rx_bytes: number | null;
  net_tx_bytes: number | null;
  io_read_bytes_per_second: number | null;
  io_write_bytes_per_second: number | null;
  net_rx_bytes_per_second: number | null;
  net_tx_bytes_per_second: number | null;
}

export interface AdminSandbox {
  id: string;
  origin_thread_id: string;
  title: string | null;
  repo: string;
  user_id: string;
  host: string;
  container_id: string | null;
  container_name: string;
  runtime_id: string;
  runtime_version: string | null;
  execution_profile: string;
  state: 'running';
  memory_limit: string;
  cpu_limit: string;
  member_count: number;
  active_attempt_count: number;
  resource_usage?: AdminSandboxResourceUsage | null;
  resident_slot_capacity?: number;
  active_operation_count?: number;
  snapshot_state?: string;
  terminal_count?: number | null;
  terminal_count_observed_at?: string | null;
  created_at: string;
  last_activity_at: string;
  idle_deadline: string | null;
  updated_at: string;
  host_last_heartbeat_at: string | null;
}

export interface AdminSandboxesPage {
  sandboxes: AdminSandbox[];
  next_cursor: string | null;
  observed_at: string;
  total_count: number;
}

export function getAdminSandboxes(
  filters: { cursor?: string; limit?: number } = {},
  signal?: AbortSignal,
): Promise<AdminSandboxesPage> {
  const params = new URLSearchParams();
  if (filters.cursor) params.set('cursor', filters.cursor);
  if (filters.limit !== undefined) params.set('limit', String(filters.limit));
  const query = params.size ? `?${params}` : '';
  return fetchWithAuth(config.apiBase, `/admin/agent/sandboxes${query}`, {
    signal,
    cache: 'no-store',
  }).then(jsonOrThrow<AdminSandboxesPage>);
}

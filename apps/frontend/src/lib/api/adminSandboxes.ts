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
  state: AdminSandboxState;
  generation?: number;
  stop_requested_at?: string | null;
  archived_at?: string | null;
  deletion_state?: AdminSandboxDeletionState;
  deletion_discard?: boolean;
  deletion_requested_at?: string | null;
  deletion_request_id?: string | null;
  deletion_archive_key?: string | null;
  deletion_sha256?: string | null;
  deletion_size_bytes?: number | null;
  deleted_at?: string | null;
  deletion_error?: string | null;
  memory_limit: string;
  cpu_limit: string;
  /** Retained writable-layer quota; zero or absent on older responses means no quota. */
  disk_limit_mib?: number;
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

export type AdminSandboxState =
  | 'provisioning'
  | 'starting'
  | 'running'
  | 'stopping'
  | 'stopped'
  | 'stopped_dirty'
  | 'lost';

export type AdminSandboxDeletionState = 'none' | 'pending' | 'archived' | 'deleted' | 'failed';

export interface AdminSandboxMember {
  thread_id: string;
  title: string | null;
  user_id: string;
  resident_slot_id: number | null;
  resident_state: string | null;
  context_status: string | null;
  native_state: string | null;
  active_attempt_id: number | null;
  last_settled_job_id: string | null;
  created_at: string;
  updated_at: string;
}

/** The newest restorable checkpoint a host reported. Restore uses it later. */
export interface AdminSandboxCheckpoint {
  id: string;
  generation: number;
  host: string;
  size_bytes: number;
  new_layer_bytes: number;
  state: 'reported' | 'verified' | 'superseded' | 'deleted';
  captured_at: string;
  recorded_at: string;
  durations_ms?: Record<string, number>;
}

export interface AdminSandboxDetail {
  sandbox: AdminSandbox;
  members: AdminSandboxMember[];
  observed_at: string;
  terminal_activity_error?: string | null;
  latest_checkpoint?: AdminSandboxCheckpoint | null;
}

export interface AdminSandboxStopRequest {
  generation: number;
  container_id: string;
  last_activity_at: string;
}

export interface AdminSandboxTrajectoryEvent {
  id: number;
  generation: number;
  event_type: string;
  from_state: string | null;
  to_state: string | null;
  source: 'system' | 'admin' | 'host' | 'migration';
  actor_id: string | null;
  created_at: string;
  details: Record<string, unknown>;
}

export interface AdminSandboxTrajectory {
  sandbox: AdminSandbox;
  members: AdminSandboxMember[];
  member_history_count: number;
  member_history_truncated: boolean;
  events: AdminSandboxTrajectoryEvent[];
  observed_at: string;
  next_cursor: string | null;
  latest_checkpoint?: AdminSandboxCheckpoint | null;
}

export interface AdminSandboxInventoryRequest {
  generation: number;
  container_id: string | null;
  last_activity_at: string;
}

export interface AdminSandboxesPage {
  sandboxes: AdminSandbox[];
  next_cursor: string | null;
  observed_at: string;
  total_count: number;
}

export function getAdminSandboxes(
  filters: {
    cursor?: string;
    limit?: number;
    state?: AdminSandboxState | 'all';
    include_archived?: boolean;
  } = {},
  signal?: AbortSignal,
): Promise<AdminSandboxesPage> {
  const params = new URLSearchParams();
  if (filters.cursor) params.set('cursor', filters.cursor);
  if (filters.limit !== undefined) params.set('limit', String(filters.limit));
  if (filters.state) params.set('state', filters.state);
  if (filters.include_archived !== undefined)
    params.set('include_archived', String(filters.include_archived));
  const query = params.size ? `?${params}` : '';
  return fetchWithAuth(config.apiBase, `/admin/agent/sandboxes${query}`, {
    signal,
    cache: 'no-store',
  }).then(jsonOrThrow<AdminSandboxesPage>);
}

export function getAdminSandbox(
  sandboxId: string,
  signal?: AbortSignal,
): Promise<AdminSandboxDetail> {
  return fetchWithAuth(config.apiBase, `/admin/agent/sandboxes/${encodeURIComponent(sandboxId)}`, {
    signal,
    cache: 'no-store',
  }).then(jsonOrThrow<AdminSandboxDetail>);
}

export function getAdminSandboxTrajectory(
  sandboxId: string,
  filters: { cursor?: string; limit?: number } = {},
  signal?: AbortSignal,
): Promise<AdminSandboxTrajectory> {
  const params = new URLSearchParams();
  if (filters.limit !== undefined) params.set('limit', String(filters.limit));
  if (filters.cursor) params.set('cursor', filters.cursor);
  const query = params.size ? `?${params}` : '';
  return fetchWithAuth(
    config.apiBase,
    `/admin/agent/sandboxes/${encodeURIComponent(sandboxId)}/trajectory${query}`,
    { signal, cache: 'no-store' },
  ).then(jsonOrThrow<AdminSandboxTrajectory>);
}

export function stopAdminSandbox(
  sandboxId: string,
  reviewed: AdminSandboxStopRequest,
  signal?: AbortSignal,
): Promise<AdminSandboxDetail> {
  return fetchWithAuth(
    config.apiBase,
    `/admin/agent/sandboxes/${encodeURIComponent(sandboxId)}/stop`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(reviewed),
      signal,
      cache: 'no-store',
    },
  ).then(jsonOrThrow<AdminSandboxDetail>);
}

export function deleteAdminSandbox(
  sandboxId: string,
  reviewed: AdminSandboxStopRequest,
  signal?: AbortSignal,
  force = false,
  discard = false,
): Promise<AdminSandboxDetail> {
  const params = new URLSearchParams();
  if (force) params.set('force', 'true');
  if (discard) params.set('discard', 'true');
  const query = params.size ? `?${params}` : '';
  return postSandboxAction(sandboxId, 'delete', reviewed, signal, query);
}

export function archiveAdminSandbox(
  sandboxId: string,
  reviewed: AdminSandboxInventoryRequest,
  signal?: AbortSignal,
  stale = false,
): Promise<AdminSandboxDetail> {
  return postSandboxAction(sandboxId, 'archive', reviewed, signal, stale ? '?stale=true' : '');
}

export function restoreAdminSandbox(
  sandboxId: string,
  reviewed: AdminSandboxInventoryRequest,
  signal?: AbortSignal,
): Promise<AdminSandboxDetail> {
  return postSandboxAction(sandboxId, 'restore', reviewed, signal);
}

function postSandboxAction(
  sandboxId: string,
  operation: 'archive' | 'restore' | 'delete',
  reviewed: AdminSandboxInventoryRequest,
  signal?: AbortSignal,
  query = '',
): Promise<AdminSandboxDetail> {
  return fetchWithAuth(
    config.apiBase,
    `/admin/agent/sandboxes/${encodeURIComponent(sandboxId)}/${operation}${query}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(reviewed),
      signal,
      cache: 'no-store',
    },
  ).then(jsonOrThrow<AdminSandboxDetail>);
}

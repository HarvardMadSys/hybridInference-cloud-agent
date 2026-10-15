import { config } from '@/config/env';

import type { AdminUsageAggregate, AdminUsageBreakdown, AdminUsageDay } from './adminUsage';
import { fetchWithAuth, jsonOrThrow } from './client';

const USAGE = '/v1/agent/usage';

export type ContainerState =
  | 'starting'
  | 'running'
  | 'stopping'
  | 'stopped'
  | 'stored'
  | 'deleting'
  | 'unavailable';

export interface OwnerContainerChat {
  thread_id: string;
  /** The chat's latest turn, which a chat link opens. */
  job_id: string | null;
  title: string | null;
  archived: boolean;
}

/** The latest Docker sample of a running container. */
export interface OwnerContainerReading {
  cpu_cores: number | null;
  memory_bytes: number | null;
  sampled_at: string;
  stale: boolean;
}

export interface OwnerContainer {
  id: string;
  state: ContainerState;
  repo: string | null;
  chats: OwnerContainerChat[];
  chat_count: number;
  cpu_limit_cores: number | null;
  memory_limit_bytes: number | null;
  /** Null when the container has no disk limit. */
  disk_limit_bytes: number | null;
  reading: OwnerContainerReading | null;
  created_at: string;
  last_activity_at: string;
  /** When an idle running container stops by itself. */
  idle_deadline: string | null;
}

export interface OwnerContainerList {
  observed_at: string;
  counts: Partial<Record<ContainerState, number>>;
  total_count: number;
  /** Sums over running containers with a fresh sample; the rest are counted. */
  current: { cpu_cores: number; memory_bytes: number; measured: number; unmeasured: number };
  containers: OwnerContainer[];
  next_cursor: string | null;
}

export interface OwnerResourceTotals {
  running_seconds: number;
  /** Running time covered by samples; CPU and memory describe only this. */
  measured_seconds: number;
  cpu_core_seconds: number;
  memory_byte_seconds: number;
}

export interface OwnerResourceDay extends OwnerResourceTotals {
  date: string;
  since: string;
  until: string;
}

export interface OwnerResourceHistory {
  since: string;
  until: string;
  day_timezone: 'UTC';
  generated_at: string;
  totals: OwnerResourceTotals;
  days: OwnerResourceDay[];
}

/** The admin Usage page's accounting, selected for the owner's own runs. */
export interface OwnerTokenUsage {
  since: string;
  until: string;
  day_timezone: 'UTC';
  time_basis: 'job_created_at';
  generated_at: string;
  summary: AdminUsageAggregate;
  days: AdminUsageDay[];
  by_model: AdminUsageBreakdown;
}

export async function getOwnerContainers(
  options: { cursor?: string | null; limit?: number; signal?: AbortSignal } = {},
): Promise<OwnerContainerList> {
  const params = new URLSearchParams();
  if (options.cursor) params.set('cursor', options.cursor);
  if (options.limit) params.set('limit', String(options.limit));
  const query = params.toString();
  const response = await fetchWithAuth(
    config.apiBase,
    `${USAGE}/containers${query ? `?${query}` : ''}`,
    { signal: options.signal, cache: 'no-store' },
  );
  return jsonOrThrow<OwnerContainerList>(response);
}

export async function getOwnerResources(
  days: number,
  signal?: AbortSignal,
): Promise<OwnerResourceHistory> {
  const response = await fetchWithAuth(config.apiBase, `${USAGE}/resources?days=${days}`, {
    signal,
    cache: 'no-store',
  });
  return jsonOrThrow<OwnerResourceHistory>(response);
}

export async function getOwnerTokens(days: number, signal?: AbortSignal): Promise<OwnerTokenUsage> {
  const response = await fetchWithAuth(config.apiBase, `${USAGE}/tokens?days=${days}`, {
    signal,
    cache: 'no-store',
  });
  return jsonOrThrow<OwnerTokenUsage>(response);
}

import { config } from '@/config/env';

import type { AgentLedgerStatus, AgentLedgerTotalsApi } from './agents';
import { fetchWithAuth, jsonOrThrow } from './client';

const USAGE = '/admin/agent/usage';

/** Known metric sums. An unreported metric stays null and is never a zero. */
export type AdminUsageTotals = AgentLedgerTotalsApi;

export type AdminUsageUnknownRows = Record<Exclude<keyof AdminUsageTotals, 'calls'>, number>;

export interface AdminUsageAggregate {
  run_count: number;
  attempt_count: number;
  totals: AdminUsageTotals | null;
  unknown_rows: AdminUsageUnknownRows | null;
  estimated_calls: number;
  oldest_queried_at: string | null;
  runs_with_snapshot: number;
  missing_attempts: number;
  status_counts: Record<AgentLedgerStatus, number>;
  active_runs: number;
  refreshing_runs: number;
}

export interface AdminUsageGroup {
  /** The complete stored identity; null is only an unknown configured model. */
  key: string | null;
  usage: AdminUsageAggregate;
}

export interface AdminUsageBreakdown {
  /** Groups before the first twenty were taken. */
  group_count: number;
  groups: AdminUsageGroup[];
  other: AdminUsageGroup | null;
}

export interface AdminUsageDay {
  date: string;
  /** The intersection with the selection, not the interval of the usage. */
  since: string;
  until: string;
  usage: AdminUsageAggregate;
}

export interface AdminUsageSelection {
  since: string;
  until: string;
  time_basis: 'job_created_at';
  day_timezone: 'UTC';
  model_basis: 'job_configured_model';
  user_id: string | null;
  model: string | null;
  runtime: string | null;
  repo: string | null;
}

export interface AdminUsage {
  schema_version: 1;
  source: 'gateway_api_logs';
  attribution: 'grant_time_window';
  finalized: false;
  selection: AdminUsageSelection;
  generated_at: string;
  summary: AdminUsageAggregate;
  days: AdminUsageDay[];
  by_user: AdminUsageBreakdown;
  by_model: AdminUsageBreakdown;
}

export interface AdminUsageFilters {
  since?: string;
  until?: string;
  user_id?: string;
  model?: string;
  runtime?: string;
  repo?: string;
}

export async function getAdminUsage(
  filters: AdminUsageFilters,
  signal?: AbortSignal,
): Promise<AdminUsage> {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== '') params.set(key, value);
  }
  const query = params.size ? `?${params}` : '';
  return jsonOrThrow<AdminUsage>(
    await fetchWithAuth(config.apiBase, `${USAGE}${query}`, { signal, cache: 'no-store' }),
  );
}

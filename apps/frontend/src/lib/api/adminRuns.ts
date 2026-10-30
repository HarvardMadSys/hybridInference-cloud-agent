import { config } from '@/config/env';

import type {
  AgentAttemptTelemetryApi,
  AgentJobApiState,
  AgentJobUsageApi,
  RunResourceSummaryApi,
} from './agents';
import { fetchWithAuth, jsonOrThrow } from './client';

// The admin Runs surface reads the same resource shapes the owner-facing
// telemetry endpoint returns, so they are defined once beside
// `AttemptResourcesApi` and re-exported here for the admin page and its
// presentation module.
export type {
  ResourceMetricBriefApi,
  ResourceMetricStatus,
  RunResourceSummaryApi,
  TurnCpuResourcesApi,
  TurnMemoryResourcesApi,
  TurnResourceReasonCode,
  TurnResourceState,
  TurnResourceSummaryApi,
  TurnResourceWindowApi,
  TurnStorageIoResourcesApi,
} from './agents';

const RUNS = '/admin/agent/runs';

/** Metadata only: never the turn's prompt, result or raw error. */
export interface AdminRun {
  id: string;
  title: string | null;
  thread_id: string | null;
  turn_no: number;
  user_id: string;
  /** Null for a temporary workspace. */
  repo: string | null;
  runtime: string;
  model: string;
  reasoning_effort: string | null;
  execution_profile: string;
  state: AgentJobApiState;
  created_at: string;
  updated_at: string;
  hosts: string[];
  /** Display names; older control planes only return runner IDs in hosts. */
  physical_hosts?: string[];
  tool_calls: number;
  /**
   * Compact whole-turn member execution window, or null when no attempt of
   * this run has one. Optional because older control planes do not send it;
   * null and absent both mean "not collected", never zero.
   */
  resource_summary?: RunResourceSummaryApi | null;
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

/** Estimates from recorded activity, aggregated across this run's executions. */
export interface AdminSessionStatistics {
  model_seconds: number | null;
  tool_seconds: number | null;
  /** Turn start to first recorded output event, startup included; not TTFT. */
  first_output_seconds: number | null;
  output_tokens_per_second: number | null;
  timing_source: 'message_events';
}

export interface AdminRunDetail {
  run: AdminRun;
  attempts: AdminAttempt[];
  usage: AgentJobUsageApi;
  /** Older control planes do not include session statistics. */
  session_statistics?: AdminSessionStatistics | null;
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

export function getAdminAttemptResources(
  jobId: string,
  attemptId: number,
  signal?: AbortSignal,
): Promise<AgentAttemptTelemetryApi> {
  return read(`${runPath(jobId)}/attempts/${attemptId}/resources`, signal);
}


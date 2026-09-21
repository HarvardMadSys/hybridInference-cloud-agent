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
  ttft_seconds: number | null;
  output_tokens_per_second: number | null;
  timing_source: 'message_events';
}

export interface AdminRunDetail {
  run: AdminRun;
  attempts: AdminAttempt[];
  usage: AgentJobUsageApi;
  /** Older control planes do not include session statistics. */
  session_statistics?: AdminSessionStatistics | null;
  capabilities: { llm_requests: boolean };
}

/** One stored gateway attribution reference for a run's execution. */
export interface AdminLlmRequestSource {
  source_id: string | null;
  attempt_id: number;
  attempt_no: number;
  window_since: string | null;
  window_until: string | null;
  window_state: 'open' | 'closed' | 'unknown';
  availability: 'queryable' | 'pending' | 'unavailable';
  reason: string | null;
}

export interface AdminLlmRequestSourcesPage {
  job_id: string;
  observed_at: string;
  sources: AdminLlmRequestSource[];
  next_cursor: string | null;
  reason: string | null;
}

/** One logged gateway request. Safe metadata only: never a prompt or response. */
export interface AdminLlmRequest {
  request_id: string;
  request_started_at: string;
  logged_at: string;
  model: string | null;
  served_model_id: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cache_read: number | null;
  cache_write: number | null;
  reasoning: number | null;
  spent_usd: string | null;
  ttft_ms: number | null;
  latency_ms: number | null;
  status_code: number | null;
  usage_estimated: boolean;
}

export interface AdminLlmRequestPage {
  job_id: string;
  source_id: string;
  attempt_id: number;
  attempt_no: number;
  window: { since: string; until: string; basis: 'request_started_at' };
  source: 'gateway_api_logs';
  fetched_at: string;
  gateway_as_of: string;
  pagination_consistency: 'live_keyset';
  finalized: false;
  requests: AdminLlmRequest[];
  next_cursor: string | null;
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

/** Which stored references this execution can be read through; no gateway call. */
export function getAdminLlmRequestSources(
  jobId: string,
  filters: { attempt_id?: number; cursor?: string; limit?: number } = {},
  signal?: AbortSignal,
): Promise<AdminLlmRequestSourcesPage> {
  return read(`${runPath(jobId)}/llm-request-sources${query(filters)}`, signal);
}

/** One page of gateway request rows for one stored reference. */
export function getAdminLlmRequests(
  jobId: string,
  filters: { source_id: string | number; cursor?: string; limit?: number },
  signal?: AbortSignal,
): Promise<AdminLlmRequestPage> {
  return read(`${runPath(jobId)}/llm-requests${query(filters)}`, signal);
}

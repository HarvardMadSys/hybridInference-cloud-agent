import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

export type AgentLedgerStatus = 'pending' | 'available' | 'partial' | 'unavailable' | 'stale';

export interface AgentLedgerTotalsApi {
  calls: number;
  tokens_in: number | null;
  tokens_out: number | null;
  cache_read: number | null;
  cache_write: number | null;
  reasoning: number | null;
  /** Decimal USD, preserving the Gateway's precision. */
  spent_usd: string | null;
}

export type AgentLedgerUnknownRowsApi = Record<
  Exclude<keyof AgentLedgerTotalsApi, 'calls'>,
  number
>;

export interface AgentAttemptUsageApi {
  attempt_id: number;
  attempt_no: number;
  status: AgentLedgerStatus;
  reason: string | null;
  totals: AgentLedgerTotalsApi | null;
  unknown_rows: AgentLedgerUnknownRowsApi | null;
  estimated_calls: number;
  queried_at: string | null;
  refreshing?: boolean;
  next_refresh_at?: string | null;
  windows: Array<{ since: string; until: string; basis: 'request_started_at' }>;
}

/** Cached job-scoped usage, including retries. The browser never queries the Gateway. */
export interface AgentJobUsageApi {
  ledger: {
    status: AgentLedgerStatus;
    source: 'gateway_api_logs';
    attribution: 'grant_time_window';
    finalized: false;
    totals: AgentLedgerTotalsApi | null;
    unknown_rows: AgentLedgerUnknownRowsApi | null;
    estimated_calls: number;
    queried_at: string | null;
    missing_attempts: number;
    refreshing?: boolean;
    next_refresh_at?: string | null;
    attempts: AgentAttemptUsageApi[];
  };
  runtime: {
    source: 'runtime';
    input_tokens: number | null;
    output_tokens: number | null;
    cache_read_tokens: number | null;
    /** Cache-inclusive input, null when the harness counters cannot establish it. */
    normalized_input_tokens?: number | null;
  };
  /**
   * The turn ran on its owner's own model connection: that provider bills it.
   */
  external_provider?: boolean;
  /**
   * Orders two reads of this job's usage, asked for or pushed: the one whose
   * version compares greater saw everything the other did. Opaque.
   */
  version?: string | null;
  conversation?: {
    totals: AgentLedgerTotalsApi | null;
    unknown_rows: AgentLedgerUnknownRowsApi | null;
    estimated_calls: number;
    queried_at: string | null;
    turn_count: number;
    incomplete: boolean;
    refreshing: boolean;
    next_refresh_at?: string | null;
    active?: boolean;
    /** Some turn ran on the owner's own connection. */
    external_provider?: boolean;
  } | null;
}

/** Read cached snapshots; a pre-ledger control plane returns null, never zero usage. */
export async function getAgentJobUsage(
  jobId: string,
  signal?: AbortSignal,
): Promise<AgentJobUsageApi | null> {
  const resp = await fetchWithAuth(API_BASE, `/v1/agent/jobs/${encodeURIComponent(jobId)}/usage`, {
    signal,
  });
  if (resp.status === 404) return null;
  return jsonOrThrow<AgentJobUsageApi>(resp);
}

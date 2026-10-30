import { fetchWithAuth, jsonOrThrow } from '../client';
import { config } from '@/config/env';

const API_BASE = config.apiBase;

/** How much of a turn's usage its agent has reported. */
export type AgentUsageStatus = 'pending' | 'available' | 'partial' | 'unavailable';

/**
 * Known metric sums of what agents reported. A metric nothing reported stays
 * null, never zero. Agents report no cost and count no model calls, so
 * `spent_usd` and `calls` are null; `reasoning` is part of `tokens_out`.
 */
export interface AgentUsageTotalsApi {
  calls: number | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cache_read: number | null;
  cache_write: number | null;
  reasoning: number | null;
  spent_usd: string | null;
}

export type AgentUsageUnknownRowsApi = Record<Exclude<keyof AgentUsageTotalsApi, 'calls'>, number>;

/**
 * What one job's agent reported about its own model calls, across its
 * retries. A counter is null when no call reported it: unknown, never zero.
 */
export interface AgentJobUsageApi {
  runtime: {
    source: 'runtime';
    input_tokens: number | null;
    output_tokens: number | null;
    cache_read_tokens: number | null;
    cache_write_tokens?: number | null;
    /** Of `output_tokens`, those reported as reasoning (Codex only). */
    reasoning_tokens?: number | null;
    /** Cache-inclusive input, null when the agent's counters cannot establish it. */
    normalized_input_tokens?: number | null;
  };
  /** The turn ran on its owner's own model connection: that provider bills it. */
  external_provider?: boolean;
  /**
   * Orders two reads of this job's usage, asked for or pushed: the one whose
   * version compares greater saw everything the other did. Opaque.
   */
  version?: string | null;
}

/** Read what a job's agent reported; a job that is not the caller's reads as null. */
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

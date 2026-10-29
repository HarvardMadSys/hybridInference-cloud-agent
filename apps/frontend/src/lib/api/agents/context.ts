import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from '../client';

const API_BASE = config.apiBase;

// One conversation's Context view, as GET /v1/agent/jobs/{id}/thread/context
// returns it. Every number keeps its kind: a request's prompt size is what the
// CLI relayed from the provider for that request, the latest one is the latest
// *observed* context (never the live window), a window and its share exist only
// when the CLI reported one, and the composition is an estimate of stored text.

export type ContextSupport = 'reported' | 'partly_reported' | 'not_reported';

export interface RuntimeContextCapabilityApi {
  runtime: string;
  turns: number;
  request_usage: ContextSupport;
  subagent_split: 'reported' | 'not_applicable' | 'not_reported';
  compaction_events: ContextSupport;
  compaction_sizes: ContextSupport;
  context_window: ContextSupport;
  window_reported_turns: number;
  live_occupancy: 'not_collected';
}

export interface ContextPointApi {
  index: number;
  job_id: string;
  turn_no: number;
  attempt_id: number;
  seq: number;
  event_id: number | null;
  at: string | null;
  prompt_tokens: number;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  uncached_input_tokens: number | null;
  output_tokens: number | null;
  context_window: number | null;
  model: string | null;
  delta_tokens: number | null;
  after_compaction: boolean;
}

export interface LatestContextApi {
  point: ContextPointApi;
  window_share: number | null;
  compacted_after: boolean;
  turn_active: boolean;
}

export interface ContextTurnApi {
  job_id: string;
  turn_no: number;
  created_at: string | null;
  runtime: string | null;
  model: string | null;
  state: string | null;
  copied: boolean;
  attempts: number;
  requests: number;
  subagent_requests: number;
  first_prompt_tokens: number | null;
  last_prompt_tokens: number | null;
  peak_prompt_tokens: number | null;
  change_tokens: number | null;
  compactions: number;
  context_window: number | null;
  first_index: number | null;
}

export interface ContextCompactionApi {
  job_id: string;
  turn_no: number;
  attempt_id: number;
  seq: number;
  event_id: number | null;
  at: string | null;
  source: string;
  trigger: string | null;
  pre_tokens: number | null;
  post_tokens: number | null;
  completed: boolean | null;
  observed_before: number | null;
  observed_after: number | null;
}

export interface ContextChangeApi {
  direction: 'growth' | 'drop';
  delta_tokens: number;
  from_index: number;
  to_index: number;
  compaction_between: boolean;
}

export type ContextCategory = 'user' | 'assistant' | 'reasoning' | 'tool_calls' | 'tool_results';

export interface ContextCompositionApi {
  basis: 'since_last_compaction' | 'whole_conversation';
  chars_per_token: number;
  categories: Partial<Record<ContextCategory, number>>;
  estimated_tokens: number;
  truncated: boolean;
  unattributed_tokens: number | null;
}

export interface ContextCoverageApi {
  turns: number;
  attempts: number;
  attempts_observed: number;
  main_requests: number;
  subagent_requests: number;
  compactions: number;
  compactions_unknown_outcome: number;
  points_omitted: number;
  unparsed_raw: number;
}

export interface ThreadContextApi {
  thread_id: string;
  generated_at: string;
  capabilities: RuntimeContextCapabilityApi[];
  latest: LatestContextApi | null;
  peak: ContextPointApi | null;
  turns: ContextTurnApi[];
  requests: ContextPointApi[];
  compactions: ContextCompactionApi[];
  largest_changes: ContextChangeApi[];
  composition: ContextCompositionApi | null;
  coverage: ContextCoverageApi;
}

/** The Context view of the caller's conversation holding ``jobId``; null when absent. */
export async function getAgentThreadContext(
  jobId: string,
  signal?: AbortSignal,
): Promise<ThreadContextApi | null> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/thread/context`,
    { signal, cache: 'no-store' },
  );
  if (resp.status === 404) return null;
  return jsonOrThrow<ThreadContextApi>(resp);
}

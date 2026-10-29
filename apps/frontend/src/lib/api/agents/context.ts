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
  /** The CLI's own account of a request, read without holding the conversation. */
  native_composition: ContextSupport | 'names_only';
  snapshot_turns: number;
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

export type ContextEventKind = 'injection' | 'compaction' | 'prune' | 'model_switch' | 'mode';
export const CONTEXT_EVENT_KINDS: readonly ContextEventKind[] = [
  'injection',
  'compaction',
  'prune',
  'model_switch',
  'mode',
];

/**
 * One context event, with only the sizes its record carries: a compaction's
 * sizes as the CLI reported them (`tokens_basis` `cli`), an injection's as an
 * estimate from its characters (`estimate`), nothing otherwise.
 */
export interface ContextEventApi {
  kind: ContextEventKind;
  source: string;
  label: string | null;
  names: string[];
  items: number | null;
  job_id: string;
  turn_no: number;
  attempt_id: number | null;
  seq: number | null;
  event_id: number | null;
  at: string | null;
  chars: number | null;
  tokens: number | null;
  tokens_basis: 'cli' | 'estimate' | null;
  before_tokens: number | null;
  after_tokens: number | null;
  from_value: string | null;
  to_value: string | null;
  /** From an attempt a retry replaced: listed, not counted. */
  superseded: boolean;
  /** From a Fork's copy of its source's turn. */
  copied: boolean;
}

export interface EventCapabilityApi {
  runtime: string;
  kind: ContextEventKind;
  support: ContextSupport;
  reason: string | null;
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
  coverage: ContextCoverageApi;
  events: ContextEventApi[];
  event_counts: Partial<Record<ContextEventKind, number>>;
  events_omitted: number;
  event_capabilities: EventCapabilityApi[];
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

// One request's composition, as GET .../thread/context/requests/{event_id}
// returns it: the provider's reported input, the CLI's own snapshot when one
// was taken, and stored content reconstructed in transcript order.

export interface NativeSnapshotCategoryApi {
  key: string;
  tokens: number | null;
  chars?: number;
  entries?: number;
  /** False: the CLI does not expose this category; its size is unknown. */
  exposed: boolean;
  labels?: string[];
}

export interface NativeSnapshotEntryApi {
  category: string;
  label: string;
  source?: string;
  tokens: number | null;
  chars?: number;
  token_basis?: string;
  preview?: string;
  deferred?: boolean;
  pruned?: boolean;
  synthetic?: boolean;
  is_error?: boolean;
}

export interface NativeSnapshotApi {
  v: number;
  source: string;
  status: string;
  detail?: string;
  /** `names_only`: named, never sized; otherwise an estimate from text. */
  token_basis?: string;
  /** `turn_start`: read as the turn started, for its first request. */
  observed?: string;
  total_tokens?: number | null;
  context_window?: number | null;
  categories?: NativeSnapshotCategoryApi[];
  entries?: NativeSnapshotEntryApi[];
  entries_total?: number;
  entries_omitted?: number;
  excluded?: Record<string, number>;
  mcp_servers?: { name: string; status: string | null }[];
}

export interface CompositionEntryApi {
  category: string;
  label: string;
  event_id: number | null;
  job_id: string;
  turn_no: number;
  chars: number;
  tokens: number;
  truncated: boolean;
  preview: string;
}

export interface CompositionCategoryApi {
  key: string;
  entries: number;
  chars: number;
  tokens: number;
  truncated_entries: number;
}

export interface ReconstructedCompositionApi {
  basis: 'stored_events_transcript_order';
  token_basis: string;
  categories: CompositionCategoryApi[];
  entries: CompositionEntryApi[];
  entries_total: number;
  entries_omitted: number;
  estimated_tokens: number;
  boundary: {
    job_id: string;
    turn_no: number;
    seq: number;
    event_id: number | null;
    source: string;
    may_preserve_recent: boolean;
  } | null;
  excluded: Record<string, { entries: number; chars: number }>;
  copied_turns: number;
  events_capped: boolean;
}

export interface RequestCompositionApi {
  thread_id: string;
  request: ContextPointApi;
  runtime: string | null;
  native: NativeSnapshotApi | null;
  /** The read came as the turn started: requests and a compaction since then are not in it. */
  native_relation: {
    event_id: number | null;
    requests_before: number;
    compacted_since: boolean;
  } | null;
  native_status: string;
  reconstructed: ReconstructedCompositionApi;
}

export interface EventContentApi {
  event_id: number;
  event_type: string;
  phase: string | null;
  offset: number;
  text: string;
  total_chars: number;
  truncated: boolean;
  next_offset: number | null;
}

/** What one request carried; null when it is not the caller's. */
export async function getRequestComposition(
  jobId: string,
  eventId: number,
  signal?: AbortSignal,
): Promise<RequestCompositionApi | null> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/thread/context/requests/${eventId}`,
    { signal, cache: 'no-store' },
  );
  if (resp.status === 404) return null;
  return jsonOrThrow<RequestCompositionApi>(resp);
}

/** A window of one stored event's text; null when it is not the caller's. */
export async function getEventContent(
  jobId: string,
  eventId: number,
  offset = 0,
  signal?: AbortSignal,
): Promise<EventContentApi | null> {
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/thread/context/content/${eventId}?offset=${offset}`,
    { signal, cache: 'no-store' },
  );
  if (resp.status === 404) return null;
  return jsonOrThrow<EventContentApi>(resp);
}

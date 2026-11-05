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

/** The presets every half of the Usage page reads, in UTC days. */
export type UsageRangePreset = 'today' | '7d' | '30d' | 'all';
export const USAGE_RANGE_PRESETS: readonly UsageRangePreset[] = ['today', '7d', '30d', 'all'];
export type UsageBucket = 'day' | 'week' | 'month';

/** The interval a read covers, as the server resolved it. */
export interface UsageRange {
  preset: UsageRangePreset | null;
  days: number | null;
  since: string;
  until: string;
  day_timezone: 'UTC';
  /** Each entry of a history is one of these: a day, an ISO week or a month. */
  bucket: UsageBucket;
  /** For `all`: the earliest retained record; null when there is none. */
  first_record_at: string | null;
}

export interface OwnerResourceHistory {
  since: string;
  until: string;
  day_timezone: 'UTC';
  generated_at: string;
  totals: OwnerResourceTotals;
  /** One entry per bucket of `period` (a day, week or month). */
  days: OwnerResourceDay[];
  period: UsageRange | null;
}

/** The admin Usage page's accounting, selected for the owner's own runs. */
export interface OwnerTokenUsage {
  since: string;
  until: string;
  day_timezone: 'UTC';
  time_basis: 'job_created_at';
  generated_at: string;
  summary: AdminUsageAggregate;
  /** One entry per bucket of `period`. */
  days: AdminUsageDay[];
  by_model: AdminUsageBreakdown;
  period: UsageRange | null;
  /** Bounded batches the server summed this in; more than one for a long history. */
  batches: number;
}

export type ContextSort = 'recent' | 'peak' | 'compactions' | 'requests';
export const CONTEXT_SORTS: readonly ContextSort[] = ['recent', 'peak', 'compactions', 'requests'];

/** Recorded-content categories, in the order a chart stacks them. */
export type RecordedCategory =
  | 'turn_prompts'
  | 'platform_context'
  | 'user_messages'
  | 'assistant'
  | 'reasoning'
  | 'tool_calls'
  | 'tool_results'
  | 'compaction_summary'
  | 'injected';

/**
 * Recorded content beside the provider's reported request sizes. The first is
 * an estimate of stored text, each item counted once; the second is exact as
 * the CLI relayed it, summed over every request, so retained text counts once
 * per request that carried it. Neither is derived from the other.
 */
export interface ContextComposition {
  recorded: {
    basis: string;
    categories: { key: RecordedCategory; tokens: number }[];
    total_tokens: number;
    turns: number;
    turns_profiled: number;
    turns_truncated: number;
  };
  provider: {
    requests: number;
    prompt_tokens: number;
    cache_read_tokens: number;
    cache_write_tokens: number;
    output_tokens: number;
    output_unreported: number;
    subagent_requests: number;
    subagent_prompt_tokens: number;
    subagent_output_tokens: number;
  };
}

/** One UTC day of the range with matching runs. */
export interface ContextActivityDay {
  day: string;
  turns: number;
  conversations: number;
  main_requests: number;
  compactions: number;
  peak_prompt_tokens: number | null;
  /**
   * The provider-reported input of every request that day, summed. Absent
   * from a control plane that predates it: never read as zero.
   */
  prompt_tokens?: number;
}

/** One project of the matching runs; `repo` null is the temporary workspaces. */
export interface ContextProject {
  repo: string | null;
  conversations: number;
  turns: number;
  main_requests: number;
  compactions: number;
  peak_prompt_tokens: number | null;
  last_turn_at: string;
}

/** What narrows the list: all of it is applied by the server, over the whole range. */
export interface ContextInsightsFilters {
  q: string;
  repo: string | null;
  temporary: boolean;
  day: string | null;
}

/** One conversation's context profile over its runs in the range. */
export interface ConversationContext {
  thread_id: string;
  title: string | null;
  repo: string | null;
  archived: boolean;
  latest_job_id: string;
  turns: number;
  first_turn_at: string;
  last_turn_at: string;
  runtimes: string[];
  models: string[];
  attempts: number;
  attempts_profiled: number;
  main_requests: number;
  subagent_requests: number;
  peak_prompt_tokens: number | null;
  latest_prompt_tokens: number | null;
  latest_observed_at: string | null;
  context_window: number | null;
  compactions: number;
  /** Server-summed Gateway usage of these runs; null when none was recorded. */
  usage: AdminUsageAggregate | null;
  composition: ContextComposition | null;
}

export interface ContextInsightsPage {
  period: UsageRange;
  sort: ContextSort;
  time_basis: 'job_created_at';
  generated_at: string;
  summary: {
    conversations: number;
    conversations_observed: number;
    turns: number;
    main_requests: number;
    subagent_requests: number;
    compactions: number;
    peak_prompt_tokens: number | null;
    peak_thread_id: string | null;
    deleted_conversation_runs: number;
  };
  profiling: {
    pending_attempts: number;
    pending_capped: boolean;
    refreshed: number;
    complete: boolean;
  };
  conversations: ConversationContext[];
  next_cursor: string | null;
  filters: ContextInsightsFilters;
  /** First page only: every UTC day of the range with matching runs, day filter aside. */
  activity: ContextActivityDay[] | null;
  /** First page only: every project of the matching runs, project filter aside. */
  projects: ContextProject[] | null;
  /** First page only: every matching conversation's composition, summed. */
  composition: ContextComposition | null;
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
  range: UsageRangePreset,
  signal?: AbortSignal,
): Promise<OwnerResourceHistory> {
  const response = await fetchWithAuth(config.apiBase, `${USAGE}/resources?range=${range}`, {
    signal,
    cache: 'no-store',
  });
  return jsonOrThrow<OwnerResourceHistory>(response);
}

export async function getOwnerTokens(
  range: UsageRangePreset,
  signal?: AbortSignal,
): Promise<OwnerTokenUsage> {
  const response = await fetchWithAuth(config.apiBase, `${USAGE}/tokens?range=${range}`, {
    signal,
    cache: 'no-store',
  });
  return jsonOrThrow<OwnerTokenUsage>(response);
}

export async function getContextInsights(options: {
  range: UsageRangePreset;
  sort: ContextSort;
  cursor?: string | null;
  limit?: number;
  filters?: Partial<ContextInsightsFilters>;
  signal?: AbortSignal;
}): Promise<ContextInsightsPage> {
  const params = new URLSearchParams({ range: options.range, sort: options.sort });
  const filters = options.filters ?? {};
  if (filters.q?.trim()) params.set('q', filters.q.trim());
  if (filters.repo) params.set('repo', filters.repo);
  else if (filters.temporary) params.set('temporary', 'true');
  if (filters.day) params.set('day', filters.day);
  if (options.cursor) params.set('cursor', options.cursor);
  if (options.limit) params.set('limit', String(options.limit));
  const response = await fetchWithAuth(config.apiBase, `${USAGE}/context?${params}`, {
    signal: options.signal,
    cache: 'no-store',
  });
  return jsonOrThrow<ContextInsightsPage>(response);
}

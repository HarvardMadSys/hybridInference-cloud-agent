import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from '../client';

const API_BASE = config.apiBase;

// One conversation's Command Executions, as
// GET /v1/agent/jobs/{id}/thread/context/commands returns it. The numbers keep
// their kind: a duration exists only where the platform recorded one (or
// measured the call-to-result interval, which says so), a missing exit code is
// unknown rather than zero, and a capped read reports the conversation's true
// total instead of presenting its subset as the whole.

export type CommandStatus = 'running' | 'completed' | 'failed' | 'unknown';
export type CommandFilter = 'all' | 'failed' | 'slow' | 'background';
export type CommandSort = 'latest' | 'slowest';
export type CommandDurationBasis = 'recorded' | 'events';
export type CommandParseBasis = 'executable' | 'shell';
export type CommandDurationBasisTotal = 'recorded' | 'mixed' | 'events' | 'none';

export interface CommandExecutionApi {
  id: string;
  job_id: string;
  turn_no: number;
  attempt_id: number;
  attempt_no: number;
  runtime: string;
  tool: string;
  command: string;
  command_chars: number;
  command_truncated: boolean;
  cwd: string | null;
  status: CommandStatus;
  exit_code: number | null;
  tool_error: boolean;
  background: boolean;
  superseded: boolean;
  duration_ms: number | null;
  duration_basis: CommandDurationBasis | null;
  at: string | null;
  result_at: string | null;
  event_id: number | null;
  result_event_id: number | null;
  output_preview: string;
  output_chars: number;
  output_truncated: boolean;
  program: string;
  parse_basis: CommandParseBasis;
}

export interface CommandProgramApi {
  name: string;
  count: number;
  share: number;
  fallback: boolean;
}

export interface CommandSummaryApi {
  total: number;
  settled: number;
  failed: number;
  failure_rate: number | null;
  running: number;
  unknown: number;
  cumulative_ms: number;
  durations_known: number;
  durations_missing: number;
  duration_basis: CommandDurationBasisTotal;
  background: number;
}

export interface CommandCountsApi {
  all: number;
  failed: number;
  slow: number;
  background: number;
}

export interface CommandCoverageApi {
  executions_total: number;
  executions_covered: number;
  truncated: boolean;
  turns: number;
  attempts: number;
  copied_jobs_excluded: number;
  unsupported_runtimes: string[];
}

export interface CommandAttentionApi {
  slowest: CommandExecutionApi | null;
  failing: CommandProgramApi | null;
  repeats: { command: string; count: number } | null;
  background: number;
}

export interface CommandPageApi {
  total: number;
  returned: number;
  limit: number;
  next_cursor: string | null;
}

export interface ThreadCommandsApi {
  thread_id: string;
  generated_at: string;
  /** The cutoff the `slow` filter uses, published so it is discoverable. */
  slow_cutoff_ms: number;
  summary: CommandSummaryApi;
  coverage: CommandCoverageApi;
  counts: CommandCountsApi;
  attention: CommandAttentionApi;
  programs: CommandProgramApi[];
  executions: CommandExecutionApi[];
  page: CommandPageApi;
  filter: CommandFilter;
  sort: CommandSort;
  q: string;
  program: string | null;
}

export interface CommandQuery {
  filter?: CommandFilter;
  sort?: CommandSort;
  q?: string;
  program?: string | null;
  cursor?: string | null;
  limit?: number;
}

/** A page of the conversation's command executions; null when it is not the caller's. */
export async function getThreadContextCommands(
  jobId: string,
  query: CommandQuery = {},
  signal?: AbortSignal,
): Promise<ThreadCommandsApi | null> {
  const params = new URLSearchParams();
  if (query.filter && query.filter !== 'all') params.set('filter', query.filter);
  if (query.sort && query.sort !== 'latest') params.set('sort', query.sort);
  if (query.q) params.set('q', query.q);
  if (query.program) params.set('program', query.program);
  if (query.cursor) params.set('cursor', query.cursor);
  if (query.limit) params.set('limit', String(query.limit));
  const suffix = params.size > 0 ? `?${params.toString()}` : '';
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/thread/context/commands${suffix}`,
    { signal, cache: 'no-store' },
  );
  if (resp.status === 404) return null;
  return jsonOrThrow<ThreadCommandsApi>(resp);
}

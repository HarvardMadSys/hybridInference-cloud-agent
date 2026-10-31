import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from '../client';

const API_BASE = config.apiBase;

// A conversation's File Activity, as GET /v1/agent/jobs/{id}/thread/files
// returns it. Every number keeps its kind: counts are successfully recorded
// operations, added/removed are cumulative recorded churn against the
// recorded patches and writes (never the workspace's current Git diff), a
// delta the record did not carry is null instead of zero, and the coverage
// says which bounded window the totals describe.

export type FileActivitySort = 'active' | 'lines' | 'latest' | 'path' | 'name';
export type FileActivityFilter = 'all' | 'read' | 'written' | 'searched' | 'images';
export type FileOperationKind = 'read' | 'write' | 'edit' | 'search' | 'image';
export type FileOperationStatus = 'succeeded' | 'failed' | 'pending' | 'not_recorded';
export type FilePathBasis = 'absolute' | 'cwd_relative' | 'relative' | 'unknown';
export type FileKind = 'code' | 'data' | 'markup' | 'image' | 'shell' | 'text' | 'other';

export const FILE_ACTIVITY_SORTS: readonly FileActivitySort[] = [
  'active',
  'lines',
  'latest',
  'path',
  'name',
];

export const FILE_ACTIVITY_FILTERS: readonly FileActivityFilter[] = [
  'all',
  'read',
  'written',
  'searched',
  'images',
];

export interface FileActivityRowApi {
  path: string;
  name: string;
  type_label: string;
  file_kind: FileKind;
  reads: number;
  writes: number;
  searches: number;
  images: number;
  failed: number;
  pending: number;
  not_recorded: number;
  /** Cumulative recorded churn; not the current Git diff. */
  added: number;
  removed: number;
  /** Write/edit operations whose recorded line count was unavailable. */
  added_unknown_ops: number;
  removed_unknown_ops: number;
  operations: number;
  events: number;
  subagent_operations: number;
  attempts: number;
  first_at: string | null;
  last_at: string | null;
}

export interface FileActivityTotalsApi {
  files: number;
  operations: number;
  reads: number;
  writes: number;
  searches: number;
  images: number;
  added: number;
  removed: number;
  added_unknown_files: number;
  removed_unknown_files: number;
  failed: number;
  pending: number;
  not_recorded: number;
  /** File-shaped calls with no usable path: counted, never named. */
  unknown_path_operations: number;
  /** Searches whose scope named no file: counted, attached to none. */
  search_unknown_scope: number;
  subagent_operations: number;
}

export interface FileActivityCoverageApi {
  turns: number;
  attempts: number;
  events_scanned: number;
  /** The scan stopped at its bound: older operations are not in this view. */
  events_capped: boolean;
  window_first_at: string | null;
  window_last_at: string | null;
}

export interface FileActivitySemanticsApi {
  line_changes: 'cumulative_recorded';
  net_git_diff: 'not_read';
  search_scope: 'explicit_path_only';
  image_evidence: 'explicit_only';
  counts: 'successful_operations';
}

export interface FileActivityApi {
  thread_id: string;
  generated_at: string;
  sort: FileActivitySort;
  filter: FileActivityFilter;
  q: string;
  totals: FileActivityTotalsApi;
  filter_counts: Partial<Record<FileActivityFilter, number>>;
  coverage: FileActivityCoverageApi;
  semantics: FileActivitySemanticsApi;
  files: FileActivityRowApi[];
  has_more: boolean;
  next_cursor: string | null;
}

export interface FileOperationApi {
  call_id: string | null;
  event_id: number;
  result_event_id: number | null;
  attempt_id: number;
  attempt_no: number;
  job_id: string;
  turn_no: number;
  seq: number;
  at: string | null;
  tool: string;
  action: string;
  kind: FileOperationKind;
  status: FileOperationStatus;
  path: string | null;
  name: string | null;
  path_basis: FilePathBasis;
  added: number | null;
  removed: number | null;
  patch_index: number | null;
  patch_files: number | null;
  renamed_to: string | null;
  subagent: boolean;
  superseded: boolean;
  clipped: boolean;
}

export interface FileOperationsPageApi {
  thread_id: string;
  path: string;
  total: number;
  operations: FileOperationApi[];
  has_earlier: boolean;
  next_cursor: string | null;
}

export interface FileActivityQuery {
  sort?: FileActivitySort;
  filter?: FileActivityFilter;
  q?: string;
  cursor?: string | null;
  limit?: number;
}

/** One page of the conversation's File Activity; null when it is not the caller's. */
export async function getAgentFileActivity(
  jobId: string,
  query: FileActivityQuery = {},
  signal?: AbortSignal,
): Promise<FileActivityApi | null> {
  const params = new URLSearchParams();
  params.set('sort', query.sort ?? 'active');
  params.set('filter', query.filter ?? 'all');
  if (query.q) params.set('q', query.q);
  if (query.cursor) params.set('cursor', query.cursor);
  if (query.limit) params.set('limit', String(query.limit));
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/thread/files?${params.toString()}`,
    { signal, cache: 'no-store' },
  );
  if (resp.status === 404) return null;
  return jsonOrThrow<FileActivityApi>(resp);
}

/** One page of one file's recorded operations, newest first. */
export async function getAgentFileOperations(
  jobId: string,
  path: string,
  query: { cursor?: string | null; limit?: number } = {},
  signal?: AbortSignal,
): Promise<FileOperationsPageApi | null> {
  const params = new URLSearchParams({ path });
  if (query.cursor) params.set('cursor', query.cursor);
  if (query.limit) params.set('limit', String(query.limit));
  const resp = await fetchWithAuth(
    API_BASE,
    `/v1/agent/jobs/${encodeURIComponent(jobId)}/thread/files/operations?${params.toString()}`,
    { signal, cache: 'no-store' },
  );
  if (resp.status === 404) return null;
  return jsonOrThrow<FileOperationsPageApi>(resp);
}

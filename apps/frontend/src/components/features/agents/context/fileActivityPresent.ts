// How the File Activity section reads its data: which filters and sorts it
// offers, how a row's numbers and lines are worded, and where a row's paths
// are split for a narrow column. Kept apart from the component so the ordering
// and the unknown-versus-zero distinction are tested directly.

import type {
  FileActivityFilter,
  FileActivityRowApi,
  FileActivitySort,
  FileOperationApi,
  FileOperationKind,
  FileOperationStatus,
  FilePathBasis,
} from '@/lib/api/agents/context';
import type { TranslationKey } from '@/lib/i18n/translate';

export const SORT_LABEL: Record<FileActivitySort, TranslationKey> = {
  active: 'files.sort.active',
  lines: 'files.sort.lines',
  latest: 'files.sort.latest',
  path: 'files.sort.path',
  name: 'files.sort.name',
};

export const FILTER_LABEL: Record<FileActivityFilter, TranslationKey> = {
  all: 'files.filter.all',
  read: 'files.filter.read',
  written: 'files.filter.written',
  searched: 'files.filter.searched',
  images: 'files.filter.images',
};

export const KIND_LABEL: Record<FileOperationKind, TranslationKey> = {
  read: 'files.op.read',
  write: 'files.op.write',
  edit: 'files.op.edit',
  search: 'files.op.search',
  image: 'files.op.image',
};

export const STATUS_LABEL: Record<FileOperationStatus, TranslationKey> = {
  succeeded: 'files.status.succeeded',
  failed: 'files.status.failed',
  pending: 'files.status.pending',
  not_recorded: 'files.status.not_recorded',
};

export const BASIS_LABEL: Record<FilePathBasis, TranslationKey> = {
  absolute: 'files.basis.absolute',
  cwd_relative: 'files.basis.cwdRelative',
  relative: 'files.basis.relative',
  unknown: 'files.basis.unknown',
};

/** A path split so a narrow column can wrap on its separators, not mid-token. */
export interface PathParts {
  directory: string;
  name: string;
}

export function splitPath(path: string): PathParts {
  const cut = path.lastIndexOf('/');
  if (cut < 0) return { directory: '', name: path };
  // Keep the leading slash with the directory so the name stands alone.
  return { directory: path.slice(0, cut + 1), name: path.slice(cut + 1) };
}

/** The count each filter shows, from the server's own per-filter totals. */
export function filterCount(
  counts: Partial<Record<FileActivityFilter, number>>,
  filter: FileActivityFilter,
): number {
  return counts[filter] ?? 0;
}

/** A file's own filter counts, in the chips' order. */
export interface RowCounts {
  reads: number;
  writes: number;
  searches: number;
  images: number;
}

export function rowCounts(row: FileActivityRowApi): RowCounts {
  return { reads: row.reads, writes: row.writes, searches: row.searches, images: row.images };
}

/** Whether a file has failed, pending or unanswered operations to disclose. */
export function rowHasUnsettled(row: FileActivityRowApi): boolean {
  return row.failed + row.pending + row.not_recorded > 0;
}

/**
 * The line churn to show, in the form the design asks for: both sides known,
 * one side known, or neither. A dash is never a zero.
 */
export interface LineChurn {
  added: string;
  removed: string;
  /** Which side has no recorded number; null when both do. */
  unknown: 'added' | 'removed' | 'both' | null;
}

export function lineChurn(row: FileActivityRowApi): LineChurn {
  const added = row.added_unknown_ops > 0 ? '—' : `+${row.added.toLocaleString('en-US')}`;
  const removed = row.removed_unknown_ops > 0 ? '—' : `−${row.removed.toLocaleString('en-US')}`;
  const addedUnknown = row.added_unknown_ops > 0;
  const removedUnknown = row.removed_unknown_ops > 0;
  return {
    added,
    removed,
    unknown:
      addedUnknown && removedUnknown
        ? 'both'
        : addedUnknown
          ? 'added'
          : removedUnknown
            ? 'removed'
            : null,
  };
}

/** One operation's own line churn, keeping an unknown side a dash. */
export function operationChurn(operation: FileOperationApi): LineChurn | null {
  if (operation.kind !== 'write' && operation.kind !== 'edit') return null;
  if (operation.added === null && operation.removed === null) {
    return { added: '—', removed: '—', unknown: 'both' };
  }
  return {
    added: operation.added === null ? '—' : `+${operation.added.toLocaleString('en-US')}`,
    removed: operation.removed === null ? '—' : `−${operation.removed.toLocaleString('en-US')}`,
    unknown: operation.added === null ? 'added' : operation.removed === null ? 'removed' : null,
  };
}

/** File icons are a label, never activity evidence; they come from the kind. */
export const KIND_MARK: Record<string, string> = {
  code: 'bg-sky-500/15 text-sky-600 dark:text-sky-300',
  data: 'bg-amber-500/15 text-amber-600 dark:text-amber-300',
  markup: 'bg-violet-500/15 text-violet-600 dark:text-violet-300',
  image: 'bg-pink-500/15 text-pink-600 dark:text-pink-300',
  shell: 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-300',
  text: 'bg-slate-500/15 text-slate-600 dark:text-slate-300',
  other: 'bg-slate-500/10 text-muted',
};

/** A stable key for a row across re-reads: the normalized path is the identity. */
export function rowKey(row: Pick<FileActivityRowApi, 'path'>): string {
  return row.path;
}

/** A stable key for one recorded operation. */
export function operationKey(operation: FileOperationApi): string {
  const section = operation.patch_index === null ? '' : `:${operation.patch_index}`;
  const call = operation.call_id ?? `event-${operation.event_id}`;
  return `${operation.attempt_id}:${call}${section}`;
}

// File Activity's presentation rules, tested directly: a path that wraps on its
// separators, a filter count that is the server's own, and the churn formatter
// that must show an unknown side as a dash rather than a zero.

import { describe, expect, it } from 'vitest';

import type { FileActivityRowApi, FileOperationApi } from '@/lib/api/agents/context';
import { FILE_ACTIVITY_FILTERS, FILE_ACTIVITY_SORTS } from '@/lib/api/agents/fileActivity';
import {
  FILTER_LABEL,
  KIND_LABEL,
  SORT_LABEL,
  filterCount,
  lineChurn,
  operationChurn,
  operationKey,
  rowCounts,
  rowHasUnsettled,
  splitPath,
} from '@/components/features/agents/context/fileActivityPresent';

function row(overrides: Partial<FileActivityRowApi> = {}): FileActivityRowApi {
  return {
    path: '/workspace/src/app.ts',
    name: 'app.ts',
    type_label: 'TS',
    file_kind: 'code',
    reads: 2,
    writes: 1,
    searches: 0,
    images: 0,
    failed: 0,
    pending: 0,
    not_recorded: 0,
    added: 10,
    removed: 4,
    added_unknown_ops: 0,
    removed_unknown_ops: 0,
    operations: 3,
    events: 5,
    subagent_operations: 0,
    attempts: 1,
    first_at: '2026-10-31T08:00:00Z',
    last_at: '2026-10-31T09:00:00Z',
    ...overrides,
  };
}

function operation(overrides: Partial<FileOperationApi> = {}): FileOperationApi {
  return {
    call_id: 'call-1',
    event_id: 4,
    result_event_id: 5,
    attempt_id: 2,
    attempt_no: 1,
    job_id: 'job-1',
    turn_no: 3,
    seq: 4,
    at: '2026-10-31T08:00:00Z',
    tool: 'Edit',
    action: 'edit',
    kind: 'edit',
    status: 'succeeded',
    path: '/workspace/src/app.ts',
    name: 'app.ts',
    path_basis: 'cwd_relative',
    added: 2,
    removed: 1,
    patch_index: null,
    patch_files: null,
    renamed_to: null,
    subagent: false,
    superseded: false,
    clipped: false,
    ...overrides,
  };
}

describe('splitPath', () => {
  it('keeps the directory and its trailing slash with the directory', () => {
    expect(splitPath('/workspace/src/app.ts')).toEqual({
      directory: '/workspace/src/',
      name: 'app.ts',
    });
  });

  it('handles a path with no directory', () => {
    expect(splitPath('app.ts')).toEqual({ directory: '', name: 'app.ts' });
  });

  it('keeps a Unicode name whole', () => {
    expect(splitPath('/w/深い/ファイル.py')).toEqual({
      directory: '/w/深い/',
      name: 'ファイル.py',
    });
  });

  it('keeps a long path intact rather than cutting it', () => {
    const long = `/w/${'segment/'.repeat(80)}file.ts`;
    expect(splitPath(long).name).toBe('file.ts');
    expect(splitPath(long).directory.endsWith('segment/')).toBe(true);
  });
});

describe('filterCount', () => {
  it('reads the server count for the filter and never invents one', () => {
    expect(filterCount({ all: 5, read: 3 }, 'read')).toBe(3);
    expect(filterCount({ all: 5 }, 'images')).toBe(0);
  });
});

describe('lineChurn', () => {
  it('shows both sides when both were recorded', () => {
    expect(lineChurn(row())).toEqual({ added: '+10', removed: '−4', unknown: null });
  });

  it('shows a dash for an unknown side instead of zero', () => {
    expect(lineChurn(row({ removed_unknown_ops: 2 }))).toEqual({
      added: '+10',
      removed: '—',
      unknown: 'removed',
    });
  });

  it('reports both sides unknown', () => {
    expect(lineChurn(row({ added_unknown_ops: 1, removed_unknown_ops: 1 })).unknown).toBe('both');
  });

  it('shows a real zero as zero, because the record proved it', () => {
    expect(lineChurn(row({ added: 0, removed: 0 }))).toEqual({
      added: '+0',
      removed: '−0',
      unknown: null,
    });
  });
});

describe('operationChurn', () => {
  it('is absent for a read or a search', () => {
    expect(operationChurn(operation({ kind: 'read' }))).toBeNull();
    expect(operationChurn(operation({ kind: 'search' }))).toBeNull();
  });

  it('keeps an unknown side a dash', () => {
    expect(operationChurn(operation({ removed: null }))).toEqual({
      added: '+2',
      removed: '—',
      unknown: 'removed',
    });
  });

  it('reports both unknown for a write with no computable delta', () => {
    expect(operationChurn(operation({ added: null, removed: null }))).toEqual({
      added: '—',
      removed: '—',
      unknown: 'both',
    });
  });
});

describe('rowCounts and unsettled', () => {
  it('keeps the four counts in their own fields', () => {
    expect(rowCounts(row({ images: 2 }))).toEqual({
      reads: 2,
      writes: 1,
      searches: 0,
      images: 2,
    });
  });

  it('flags failed and unanswered operations', () => {
    expect(rowHasUnsettled(row())).toBe(false);
    expect(rowHasUnsettled(row({ failed: 1 }))).toBe(true);
    expect(rowHasUnsettled(row({ not_recorded: 2 }))).toBe(true);
  });
});

describe('labels', () => {
  it('names every sort and filter the API can return', () => {
    for (const sort of FILE_ACTIVITY_SORTS) expect(SORT_LABEL[sort]).toMatch(/^files\.sort\./);
    for (const filter of FILE_ACTIVITY_FILTERS) {
      expect(FILTER_LABEL[filter]).toMatch(/^files\.filter\./);
    }
    for (const kind of ['read', 'write', 'edit', 'search', 'image'] as const) {
      expect(KIND_LABEL[kind]).toMatch(/^files\.op\./);
    }
  });
});

describe('operationKey', () => {
  it('identifies a call and its patch section', () => {
    expect(operationKey(operation())).toBe('2:call-1');
    expect(operationKey(operation({ patch_index: 2 }))).toBe('2:call-1:2');
  });

  it('falls back to the event id when no call id was recorded', () => {
    expect(operationKey(operation({ call_id: null, event_id: 9 }))).toBe('2:event-9');
  });

  it('keeps the same two calls on one file apart', () => {
    const first = operation({ call_id: 'a' });
    const second = operation({ call_id: 'b' });
    expect(operationKey(first)).not.toBe(operationKey(second));
  });
});

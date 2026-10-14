import { describe, expect, it } from 'vitest';

import type { AgentRecordsChange } from './agentPush';
import { changesSidebarRows } from './sidebarRecordChanges';

function change(update: Partial<AgentRecordsChange>): AgentRecordsChange {
  return {
    jobs: [],
    threads: [],
    usage: [],
    usageValues: {},
    queues: [],
    mcp: [],
    publications: [],
    resync: false,
    ...update,
  };
}

const thread = {
  id: 'thread-a',
  revision: 1,
  title: 'Task',
  title_source: 'manual' as const,
  pinned_at: null,
  archived_at: null,
  deleted_at: null,
  updated_at: '2026-10-14T10:00:00Z',
};
const job = {
  id: 'a',
  thread_id: 'thread-a',
  revision: 1,
  state: 'running' as const,
  published_pr_url: null,
};

describe('changesSidebarRows', () => {
  it('counts a record seen for the first time, then ignores activity alone', () => {
    const seen = new Map<string, string>();
    expect(changesSidebarRows(change({ threads: [thread], jobs: [job] }), seen)).toBe(true);
    const bumped = { ...thread, revision: 2, updated_at: '2026-10-14T10:00:05Z' };
    expect(
      changesSidebarRows(change({ threads: [bumped], jobs: [{ ...job, revision: 2 }] }), seen),
    ).toBe(false);
  });

  it.each([
    ['a rename', { threads: [{ ...thread, title: 'Renamed' }] }],
    ['a pin', { threads: [{ ...thread, pinned_at: '2026-10-14T10:01:00Z' }] }],
    ['an archive', { threads: [{ ...thread, archived_at: '2026-10-14T10:01:00Z' }] }],
    ['a deletion', { threads: [{ id: 'thread-a', deleted: true }] }],
    ['a turn ending', { jobs: [{ ...job, state: 'succeeded' as const }] }],
    ['a publication', { jobs: [{ ...job, published_pr_url: 'https://example.test/pr/1' }] }],
    ['a new turn', { jobs: [{ ...job, id: 'b' }] }],
    ['a stream gap', { resync: true }],
  ])('counts %s', (_label, update) => {
    const seen = new Map<string, string>();
    changesSidebarRows(change({ threads: [thread], jobs: [job] }), seen);
    expect(changesSidebarRows(change(update), seen)).toBe(true);
  });

  it('remembers every record in a batch, not only the first that changed', () => {
    const seen = new Map<string, string>();
    const other = { ...thread, id: 'thread-b' };
    expect(changesSidebarRows(change({ threads: [thread, other] }), seen)).toBe(true);
    expect(changesSidebarRows(change({ threads: [other] }), seen)).toBe(false);
  });
});

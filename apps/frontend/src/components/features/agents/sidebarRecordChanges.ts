import type { AgentRecordsChange } from './agentPush';

/**
 * How often a conversation's activity alone re-reads the sidebar's lists.
 *
 * Each reply a running turn stores bumps its conversation, and every open tab
 * is pushed that bump. Activity only reorders rows, so it is read at most this
 * often rather than once per reply.
 */
export const SIDEBAR_ACTIVITY_REFRESH_MS = 15_000;

/**
 * Whether pushed records can add, remove or relabel a sidebar row, rather than
 * only bump a conversation's activity.
 *
 * A record seen for the first time counts as a change, as does a stream gap.
 * `seen` holds what each record last showed, and is updated for every record.
 * A gap also forgets it: pushes were missed, and the lists re-read for it may
 * show newer values than `seen`, so a later push that returns a record to its
 * older value is a change the lists have not seen.
 */
export function changesSidebarRows(change: AgentRecordsChange, seen: Map<string, string>): boolean {
  if (change.resync) seen.clear();
  let changed = change.resync;
  for (const job of change.jobs) {
    const shown = [
      job.deleted,
      job.thread_id,
      job.state,
      job.conversation_state,
      job.published_pr_url,
    ];
    changed = remember(seen, `job:${job.id}`, shown) || changed;
  }
  for (const thread of change.threads) {
    const shown = [
      thread.deleted,
      thread.title,
      thread.title_source,
      thread.pinned_at,
      thread.archived_at,
      thread.deleted_at,
    ];
    changed = remember(seen, `thread:${thread.id}`, shown) || changed;
  }
  return changed;
}

function remember(seen: Map<string, string>, key: string, shown: unknown[]): boolean {
  const value = JSON.stringify(shown);
  if (seen.get(key) === value) return false;
  seen.set(key, value);
  return true;
}

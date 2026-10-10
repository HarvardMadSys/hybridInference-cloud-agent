import { useCallback, useEffect, useRef, useState } from 'react';
import { listAgentConversations, type AgentConversationApi } from '@/lib/api/agents';
import { onAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { onAgentRecordsChanged } from './agentPush';
import { toDisplayJob } from './adapt';
import type { ConversationRow } from './conversations';

export interface SidebarConversation extends ConversationRow {
  archived: boolean;
  lastActivityAt: string | null;
}
export function conversationRow(row: AgentConversationApi): SidebarConversation {
  return {
    key: row.job.thread_id ?? row.job.id,
    job: { ...toDisplayJob(row.job), title: row.title },
    jobIds: [row.job.id],
    archived: row.archived,
    lastActivityAt: row.last_activity_at,
  };
}
const EMPTY_ROWS: SidebarConversation[] = [];

/** Refresh loaded rows in batches; polling must not discard earlier pages. */
export function useConversationList({
  q = '',
  repo,
  includeArchived = false,
  pinned,
  enabled = true,
  userId,
  refresh = 0,
}: {
  q?: string;
  repo?: string;
  includeArchived?: boolean;
  pinned?: boolean;
  enabled?: boolean;
  userId?: string;
  refresh?: number;
} = {}) {
  const identity = JSON.stringify([q, repo, includeArchived, pinned, userId]);
  const [data, setData] = useState<{
    identity: string;
    rows: SidebarConversation[];
    cursor: string | null;
    loadedRefresh: number | null;
  }>({ identity, rows: [], cursor: null, loadedRefresh: null });
  const dataRef = useRef(data);
  dataRef.current = data;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const busy = useRef(false);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => {
    generation.current += 1;
    setTick((v) => v + 1);
  }, []);
  useEffect(
    () =>
      onAgentSessionsChanged((change) => {
        setData((current) => ({
          ...current,
          rows: current.rows.flatMap((row) => {
            if (!change.threadIds.includes(row.key)) return [row];
            if (change.deleted || (change.archived && !includeArchived)) return [];
            return [
              {
                ...row,
                archived: change.archived ?? row.archived,
                job: {
                  ...row.job,
                  title: change.title ?? row.job.title,
                  pinnedAt:
                    change.pinnedAt !== undefined
                      ? (change.pinnedAt ?? undefined)
                      : row.job.pinnedAt,
                },
              },
            ];
          }),
        }));
        reload();
      }),
    [includeArchived, reload],
  );
  // A pushed title or pin shows at once; the list owner's refresh then reads
  // the page again for order and activity.
  useEffect(
    () =>
      onAgentRecordsChanged((change) => {
        const threads = new Map(change.threads.map((thread) => [thread.id, thread]));
        if (threads.size === 0) return;
        setData((current) => ({
          ...current,
          rows: current.rows.map((row) => {
            const thread = threads.get(row.key);
            if (!thread || thread.deleted) return row;
            return {
              ...row,
              job: {
                ...row.job,
                title: thread.title ?? row.job.title,
                pinnedAt:
                  thread.pinned_at !== undefined
                    ? (thread.pinned_at ?? undefined)
                    : row.job.pinnedAt,
              },
            };
          }),
        }));
      }),
    [],
  );
  const fetchPage = useCallback(
    async (next?: string, version = generation.current) => {
      busy.current = true;
      setLoading(true);
      setError(null);
      try {
        const previous = dataRef.current;
        const targetCount = !next && previous.identity === identity ? previous.rows.length : 0;
        const merged = new Map(
          (next && previous.identity === identity ? previous.rows : []).map((row) => [
            row.key,
            row,
          ]),
        );
        let cursor = next;
        let following: string | null = null;
        do {
          // Each request recomputes conversation activity. Use the API's larger
          // batch size when refreshing history instead of repeating 50-row scans.
          const limit = next ? 50 : Math.min(200, Math.max(50, targetCount - merged.size));
          const result = await listAgentConversations({
            q,
            repo,
            includeArchived,
            pinned,
            cursor,
            limit,
          });
          if (generation.current !== version) return;
          result.conversations.map(conversationRow).forEach((row) => merged.set(row.key, row));
          following = result.next_cursor;
          cursor = following ?? undefined;
        } while (!next && following && merged.size < targetCount);
        setData({
          identity,
          rows: [...merged.values()],
          cursor: following,
          loadedRefresh: refresh,
        });
      } catch (cause) {
        if (generation.current === version)
          setError(cause instanceof Error ? cause.message : 'Could not load conversations');
      } finally {
        if (generation.current === version) {
          busy.current = false;
          setLoading(false);
        }
      }
    },
    [q, repo, includeArchived, pinned, identity, refresh],
  );
  useEffect(() => {
    const version = ++generation.current;
    setError(null);
    busy.current = false;
    setLoading(false);
    if (enabled) void fetchPage(undefined, version);
    return () => {
      generation.current += 1;
    };
  }, [fetchPage, enabled, refresh, tick]);
  const rows = data.identity === identity ? data.rows : EMPTY_ROWS;
  const cursor = data.identity === identity ? data.cursor : null;
  const loadedRefresh = data.identity === identity ? data.loadedRefresh : null;
  const loadMore = useCallback(() => {
    if (cursor && !busy.current) void fetchPage(cursor);
  }, [cursor, fetchPage]);
  return { rows, cursor, loadedRefresh, loading, error, loadMore, reload };
}

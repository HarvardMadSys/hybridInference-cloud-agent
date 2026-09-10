import { useCallback, useEffect, useRef, useState } from 'react';
import { listAgentConversations, type AgentConversationApi } from '@/lib/api/agents';
import { onAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
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
  enabled = true,
  userId,
  refresh = 0,
}: {
  q?: string;
  repo?: string;
  includeArchived?: boolean;
  enabled?: boolean;
  userId?: string;
  refresh?: number;
} = {}) {
  const identity = JSON.stringify([q, repo, includeArchived, userId]);
  const [data, setData] = useState<{
    identity: string;
    rows: SidebarConversation[];
    cursor: string | null;
  }>({ identity, rows: [], cursor: null });
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
            return [{ ...row, archived: change.archived ?? row.archived }];
          }),
        }));
        reload();
      }),
    [includeArchived, reload],
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
          const result = await listAgentConversations({ q, repo, includeArchived, cursor, limit });
          if (generation.current !== version) return;
          result.conversations.map(conversationRow).forEach((row) => merged.set(row.key, row));
          following = result.next_cursor;
          cursor = following ?? undefined;
        } while (!next && following && merged.size < targetCount);
        setData({ identity, rows: [...merged.values()], cursor: following });
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
    [q, repo, includeArchived, identity],
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
  const loadMore = useCallback(() => {
    if (cursor && !busy.current) void fetchPage(cursor);
  }, [cursor, fetchPage]);
  return { rows, cursor, loading, error, loadMore, reload };
}

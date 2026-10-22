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
const REORDER_PAGE = 50;

/**
 * The loaded list after one read of its first page, when only activity moved;
 * `null` when that read cannot say.
 *
 * Pinned rows come first, ordered by when they were pinned. Unpinned rows follow
 * by activity, which only moves forward, so an unpinned conversation that
 * changed since the list was read now sorts above every unpinned one that did
 * not. When the page's last row is an unpinned one that did not, every changed
 * row is on the page, and the loaded rows below that one are still exactly the
 * rows that follow it. A page that ends among pinned rows cannot say what
 * moved below it.
 */
export function reorderedRows(
  loaded: SidebarConversation[],
  page: SidebarConversation[],
  nextCursor: string | null,
): SidebarConversation[] | null {
  if (nextCursor === null) return page;
  const last = page[page.length - 1];
  const at = last && !last.job.pinnedAt ? loaded.findIndex((row) => row.key === last.key) : -1;
  if (at < 0) return null;
  const before = loaded[at];
  if (before.lastActivityAt !== last.lastActivityAt || before.job.pinnedAt !== last.job.pinnedAt)
    return null;
  const shown = new Set(page.map((row) => row.key));
  return [...page, ...loaded.slice(at + 1).filter((row) => !shown.has(row.key))];
}

type ConversationQuery = { q: string; repo?: string; includeArchived: boolean; pinned?: boolean };
type ReadRows = { rows: SidebarConversation[]; cursor: string | null };

/** The list after one read of its first page; `null` when it has to be read in full. */
async function readReordered(
  query: ConversationQuery,
  loaded: ReadRows,
  signal: AbortSignal,
): Promise<ReadRows | null> {
  const result = await listAgentConversations(
    { ...query, cursor: undefined, limit: REORDER_PAGE },
    signal,
  );
  const page = result.conversations.map(conversationRow);
  const rows = reorderedRows(loaded.rows, page, result.next_cursor);
  return rows && { rows, cursor: result.next_cursor === null ? null : loaded.cursor };
}

/**
 * Every loaded row read again in batches, or the page after `next` merged
 * below them; `null` once `current` says the read no longer matters.
 */
async function readRows(
  query: ConversationQuery,
  next: string | undefined,
  loaded: SidebarConversation[],
  signal: AbortSignal,
  current: () => boolean,
): Promise<ReadRows | null> {
  const targetCount = next ? 0 : loaded.length;
  const merged = new Map((next ? loaded : []).map((row) => [row.key, row]));
  let cursor = next;
  let following: string | null = null;
  do {
    // Each request recomputes conversation activity. Use the API's larger
    // batch size when refreshing history instead of repeating 50-row scans.
    const limit = next ? 50 : Math.min(200, Math.max(50, targetCount - merged.size));
    const result = await listAgentConversations({ ...query, cursor, limit }, signal);
    if (!current()) return null;
    result.conversations.map(conversationRow).forEach((row) => merged.set(row.key, row));
    following = result.next_cursor;
    cursor = following ?? undefined;
  } while (!next && following && merged.size < targetCount);
  return { rows: [...merged.values()], cursor: following };
}

/**
 * Refresh loaded rows in batches; polling must not discard earlier pages.
 *
 * `refresh` reads every loaded row again. `reorder` says only activity moved,
 * which can only lift rows to the top: it reads the first page and keeps the
 * rows below it when that page shows nothing else moved. The pinned list, and
 * a list whose last refresh failed, are read in full instead.
 */
export function useConversationList({
  q = '',
  repo,
  includeArchived = false,
  pinned,
  enabled = true,
  userId,
  refresh = 0,
  reorder = 0,
}: {
  q?: string;
  repo?: string;
  includeArchived?: boolean;
  pinned?: boolean;
  enabled?: boolean;
  userId?: string;
  refresh?: number;
  reorder?: number;
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
  const [loadMode, setLoadMode] = useState<'foreground' | 'background' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const activeRequest = useRef<AbortController | null>(null);
  // The read under way: one of the whole list, or of the page after its cursor.
  const busy = useRef<'all' | 'more' | null>(null);
  const [tick, setTick] = useState(0);
  const reload = useCallback(() => {
    generation.current += 1;
    activeRequest.current?.abort();
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
    async (next?: string, version = generation.current, mode: 'full' | 'reorder' = 'full') => {
      activeRequest.current?.abort();
      const controller = new AbortController();
      activeRequest.current = controller;
      busy.current = next ? 'more' : 'all';
      const previous = dataRef.current;
      // Refresh even an empty loaded list without inserting a status row and
      // shifting the sidebar. First loads and explicit pagination show progress.
      setLoadMode(
        !next && previous.identity === identity && previous.loadedRefresh !== null
          ? 'background'
          : 'foreground',
      );
      setError(null);
      const query = { q, repo, includeArchived, pinned };
      const current = () => !controller.signal.aborted && generation.current === version;
      try {
        if (mode === 'reorder') {
          const reordered = await readReordered(query, previous, controller.signal);
          if (!current()) return;
          if (reordered) {
            setData({ identity, ...reordered, loadedRefresh: previous.loadedRefresh });
            return;
          }
        }
        const loaded = previous.identity === identity ? previous.rows : [];
        const read = await readRows(query, next, loaded, controller.signal, current);
        if (read) setData({ identity, ...read, loadedRefresh: refresh });
      } catch (cause) {
        if (current())
          setError(cause instanceof Error ? cause.message : 'Could not load conversations');
      } finally {
        if (activeRequest.current === controller) activeRequest.current = null;
        if (generation.current === version) {
          busy.current = null;
          setLoadMode(null);
        }
      }
    },
    [q, repo, includeArchived, pinned, identity, refresh],
  );
  useEffect(() => {
    const version = ++generation.current;
    setError(null);
    busy.current = null;
    setLoadMode(null);
    if (enabled) void fetchPage(undefined, version);
    return () => {
      generation.current += 1;
      activeRequest.current?.abort();
      activeRequest.current = null;
    };
  }, [fetchPage, enabled, refresh, tick]);
  const appliedReorder = useRef(reorder);
  useEffect(() => {
    if (reorder === appliedReorder.current) return;
    appliedReorder.current = reorder;
    const loaded = dataRef.current;
    // A read of the whole list under way, or none finished yet, already covers
    // the move. A page being loaded does not, so this read replaces it.
    if (
      !enabled ||
      busy.current === 'all' ||
      loaded.identity !== identity ||
      loaded.loadedRefresh === null
    )
      return;
    // Rows below the first page can be kept only from a read of this `refresh`.
    // Pinned rows keep their place as activity moves, so a page of the pinned
    // list never shows what moved below it.
    const mode = loaded.loadedRefresh === refresh && !pinned ? 'reorder' : 'full';
    void fetchPage(undefined, ++generation.current, mode);
  }, [reorder, enabled, identity, refresh, pinned, fetchPage]);
  const rows = data.identity === identity ? data.rows : EMPTY_ROWS;
  const cursor = data.identity === identity ? data.cursor : null;
  const loadedRefresh = data.identity === identity ? data.loadedRefresh : null;
  const loadMore = useCallback(() => {
    if (enabled && cursor && !busy.current) void fetchPage(cursor);
  }, [cursor, enabled, fetchPage]);
  return {
    rows,
    cursor,
    loadedRefresh,
    loading: loadMode !== null,
    showLoading: loadMode === 'foreground',
    error,
    loadMore,
    reload,
  };
}

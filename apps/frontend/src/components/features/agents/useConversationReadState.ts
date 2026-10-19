import { useCallback, useEffect, useRef, useState } from 'react';
import {
  listAgentReadReceipts,
  saveAgentReadReceipts,
  type AgentReadReceiptApi,
} from '@/lib/api/agents/readReceipts';
import type { SidebarConversation } from './useConversationList';
import { getConversationState } from './conversationState';

const READ_RECEIPT_BATCH_SIZE = 200;
const COMPLETED_STATES = new Set(['done', 'needs_review', 'failed', 'cancelled']);
interface Receipt {
  jobId: string;
  attemptId: number | null;
  turnNo: number | null;
}
type Receipts = Record<string, Receipt>;
interface ReadCache {
  receipts: Receipts;
  acknowledged: Receipts;
}
interface ReadState {
  userId: string | undefined;
  receipts: Receipts;
}
interface ReadEventDetail extends ReadState {
  key: string;
}
export const CONVERSATION_READ_EVENT = 'agent:conversation-read';
export const conversationReadStorageKey = (userId: string) =>
  `agents.sidebar.read.${encodeURIComponent(userId)}`;
const numberOrNull = (value: unknown): number | null =>
  typeof value === 'number' && Number.isSafeInteger(value) && value > 0 ? value : null;

/** Attempt identity is consistent between list and detail, unlike execution telemetry. */
function completionReceipt(row: SidebarConversation): Receipt | null {
  const state = getConversationState(row.job);
  if (state === 'running' || state === 'queued') return null;
  if (!COMPLETED_STATES.has(row.job.state)) return null;
  return {
    jobId: row.job.id,
    attemptId: numberOrNull(row.job.currentAttemptId),
    turnNo: numberOrNull(row.job.turnNo),
  };
}
function covers(read: Receipt | undefined, completion: Receipt): boolean {
  if (!read) return false;
  if (read.turnNo !== null && completion.turnNo !== null && read.turnNo > completion.turnNo)
    return true;
  if (read.jobId !== completion.jobId) return false;
  // A cancellation before an attempt starts has no new execution result.
  return (read.attemptId ?? 0) >= (completion.attemptId ?? 0);
}
/** A stale tab or a late network response can only advance progress. */
function mergeReceipts(local: Receipts, incoming: Receipts): Receipts {
  const merged = { ...local };
  for (const [key, receipt] of Object.entries(incoming)) {
    if (!covers(merged[key], receipt)) merged[key] = receipt;
  }
  return merged;
}
function fromApi(receipts: AgentReadReceiptApi[]): Receipts {
  return Object.fromEntries(
    receipts.map((receipt) => [
      receipt.thread_id,
      { jobId: receipt.job_id, attemptId: receipt.attempt_id, turnNo: receipt.turn_no },
    ]),
  );
}
function parseReceipts(data: unknown): Receipts {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return {};
  const receipts: Receipts = {};
  for (const [key, value] of Object.entries(data)) {
    if (
      !value ||
      typeof value !== 'object' ||
      !('jobId' in value) ||
      typeof value.jobId !== 'string' ||
      !value.jobId ||
      value.jobId.length > 128 ||
      value.jobId.includes('\0')
    )
      continue;
    receipts[key] = {
      jobId: value.jobId,
      attemptId: numberOrNull('attemptId' in value ? value.attemptId : null),
      turnNo: numberOrNull('turnNo' in value ? value.turnNo : null),
    };
  }
  return receipts;
}
function parseCache(raw: string | null): ReadCache {
  try {
    const data = JSON.parse(raw ?? 'null');
    if (data?.version === 2)
      return {
        receipts: parseReceipts(data.receipts),
        acknowledged: parseReceipts(data.acknowledged),
      };
  } catch {
    // Ignore malformed browser caches.
  }
  return { receipts: {}, acknowledged: {} };
}
function storedCache(userId: string | undefined): ReadCache {
  try {
    return parseCache(userId ? localStorage.getItem(conversationReadStorageKey(userId)) : null);
  } catch {
    return parseCache(null);
  }
}
/** Keep progress and server acknowledgments together so other tabs cannot echo cloud reads. */
function persistCache(userId: string | undefined, incoming: ReadCache): ReadCache {
  const stored = storedCache(userId);
  const merged = {
    receipts: mergeReceipts(stored.receipts, incoming.receipts),
    acknowledged: mergeReceipts(stored.acknowledged, incoming.acknowledged),
  };
  const advances = (before: Receipts, after: Receipts) =>
    Object.entries(after).some(([key, receipt]) => !covers(before[key], receipt));
  if (
    userId &&
    (advances(stored.receipts, merged.receipts) ||
      advances(stored.acknowledged, merged.acknowledged))
  ) {
    try {
      localStorage.setItem(
        conversationReadStorageKey(userId),
        JSON.stringify({ version: 2, ...merged }),
      );
    } catch {
      // Reads and acknowledgments remain usable in memory when storage is blocked.
    }
  }
  return merged;
}
function recordCompletion(
  row: SidebarConversation,
  userId: string | undefined,
  local: Receipts,
): Receipts {
  const completion = completionReceipt(row);
  if (!completion) return local;
  const receipts = mergeReceipts(local, storedCache(userId).receipts);
  if (!covers(receipts[row.key], completion)) receipts[row.key] = completion;
  const saved = persistCache(userId, { receipts, acknowledged: {} });
  window.dispatchEvent(
    new CustomEvent<ReadEventDetail>(CONVERSATION_READ_EVENT, {
      detail: { userId, receipts: saved.receipts, key: row.key },
    }),
  );
  return saved.receipts;
}

/** Detail views can mark a completion read even while its sidebar row is not rendered. */
export function markConversationRead(row: SidebarConversation, userId?: string): void {
  if (completionReceipt(row)) recordCompletion(row, userId, {});
}

/** Remember only completions the user opened; listing history does not mark it read. */
export function useConversationReadState(userId?: string): {
  ready: boolean;
  isReady: (row: SidebarConversation) => boolean;
  isUnread: (row: SidebarConversation) => boolean;
  markRead: (row: SidebarConversation) => void;
} {
  const [state, setState] = useState<ReadState>({ userId, receipts: {} });
  const [readyOwner, setReadyOwner] = useState<string>();
  const [confirmed, setConfirmed] = useState<{ userId?: string; keys: Set<string> }>({
    keys: new Set(),
  });
  const current = useRef(state);
  const owner = useRef(userId);
  owner.current = userId;

  useEffect(() => {
    const cached = storedCache(userId);
    const receipts = mergeReceipts(
      cached.receipts,
      // A child may mark an active completion before this mount effect runs.
      current.current.userId === userId ? current.current.receipts : {},
    );
    current.current = { userId, receipts };
    setState(current.current);
    setReadyOwner(undefined);
    setConfirmed({ userId, keys: new Set() });
    let disposed = false;
    let busy = false;
    let request: AbortController | undefined;
    let acknowledged = cached.acknowledged;
    let queuedRefresh = false;
    let queuedUpload = false;
    let syncCursor: string | undefined;
    const confirmedKeys = new Set<string>();
    const active = () => !disposed && owner.current === userId;
    const apply = (incoming: Receipts, persist = false) => {
      let merged = mergeReceipts(current.current.receipts, incoming);
      if (persist) {
        const saved = persistCache(userId, { receipts: merged, acknowledged });
        merged = saved.receipts;
        acknowledged = saved.acknowledged;
      }
      current.current = { userId, receipts: merged };
      setState(current.current);
    };
    const applyCloud = (receipts: AgentReadReceiptApi[]) => {
      const cloud = fromApi(receipts);
      acknowledged = mergeReceipts(acknowledged, cloud);
      for (const key of Object.keys(cloud)) confirmedKeys.add(key);
      setConfirmed({ userId, keys: new Set(confirmedKeys) });
      apply(cloud, true);
    };
    const sync = async (refresh: boolean) => {
      if (!userId || !active()) return;
      if (busy) {
        queuedRefresh ||= refresh;
        queuedUpload ||= !refresh;
        return;
      }
      busy = true;
      request = new AbortController();
      const controller = request;
      const withTimeout = async <T>(read: () => Promise<T>): Promise<T> => {
        const timeout = window.setTimeout(() => controller.abort(), 15_000);
        try {
          return await read();
        } finally {
          window.clearTimeout(timeout);
        }
      };
      try {
        if (refresh) {
          let cursor = syncCursor;
          do {
            const page = await withTimeout(() => listAgentReadReceipts(cursor, controller.signal));
            if (!active() || page.user_id !== userId) return;
            applyCloud(page.receipts);
            // Advance only after applying this page, and never from a POST response:
            // other devices may have written revisions before our own upload.
            syncCursor = page.sync_cursor ?? syncCursor;
            cursor = page.next_cursor ?? undefined;
          } while (cursor);
          setReadyOwner(userId);
        }
        // Import the old version-2 browser cache and retry offline reads in
        // bounded batches. Reads arriving during an upload join the next batch.
        while (active()) {
          const pending = Object.entries(current.current.receipts)
            .filter(([key, receipt]) => !covers(acknowledged[key], receipt))
            .slice(0, READ_RECEIPT_BATCH_SIZE);
          if (!pending.length) break;
          const response = await withTimeout(() =>
            saveAgentReadReceipts(
              pending.map(([, receipt]) => ({
                job_id: receipt.jobId,
                attempt_id: receipt.attemptId,
              })),
              controller.signal,
            ),
          );
          if (!active() || response.user_id !== userId) return;
          // The server also acknowledges obsolete/deleted local records by
          // ignoring them; they must not stall the rest of the migration.
          acknowledged = mergeReceipts(acknowledged, Object.fromEntries(pending));
          applyCloud(response.receipts);
        }
      } catch {
        // Keep the local receipt; reconnect, focus and periodic refresh retry it.
      } finally {
        busy = false;
        if (active()) {
          setReadyOwner(userId);
          if (queuedRefresh || queuedUpload) {
            const refreshAgain = queuedRefresh;
            queuedRefresh = queuedUpload = false;
            void sync(refreshAgain);
          }
        }
      }
    };
    const onRead = (event: Event) => {
      const detail = (event as CustomEvent<ReadEventDetail>).detail;
      if (owner.current !== userId || detail?.userId !== userId || !detail.receipts) return;
      const receipt = detail.receipts[detail.key];
      if (!receipt || covers(current.current.receipts[detail.key], receipt)) return;
      apply(detail.receipts);
      void sync(false);
    };
    const onStorage = (event: StorageEvent) => {
      if (
        !userId ||
        owner.current !== userId ||
        (event.key !== conversationReadStorageKey(userId) && event.key !== null)
      )
        return;
      try {
        if (event.storageArea && event.storageArea !== localStorage) return;
      } catch {
        return;
      }
      const cache = parseCache(event.newValue);
      acknowledged = mergeReceipts(acknowledged, cache.acknowledged);
      apply(cache.receipts);
      void sync(false);
    };
    const refresh = () => {
      if (document.visibilityState !== 'hidden') void sync(true);
    };
    window.addEventListener(CONVERSATION_READ_EVENT, onRead);
    window.addEventListener('storage', onStorage);
    window.addEventListener('focus', refresh);
    const reconnect = () => void sync(true);
    window.addEventListener('online', reconnect);
    document.addEventListener('visibilitychange', refresh);
    const timer = window.setInterval(refresh, 30_000);
    void sync(true);
    return () => {
      disposed = true;
      request?.abort();
      window.clearInterval(timer);
      window.removeEventListener(CONVERSATION_READ_EVENT, onRead);
      window.removeEventListener('storage', onStorage);
      window.removeEventListener('focus', refresh);
      window.removeEventListener('online', reconnect);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [userId]);

  const isUnread = useCallback(
    (row: SidebarConversation): boolean => {
      const completion = completionReceipt(row);
      return (
        completion !== null &&
        (state.userId !== userId || !covers(state.receipts[row.key], completion))
      );
    },
    [state, userId],
  );

  const markRead = useCallback(
    (row: SidebarConversation): void => {
      if (owner.current !== userId) return;
      const completion = completionReceipt(row);
      if (!completion) return;
      const local = current.current.userId === userId ? current.current.receipts : {};
      if (covers(local[row.key], completion)) return;
      const receipts = recordCompletion(row, userId, local);
      current.current = { userId, receipts };
      setState(current.current);
    },
    [userId],
  );

  const ready = !userId || readyOwner === userId;
  const isReady = (row: SidebarConversation) =>
    ready || (confirmed.userId === userId && confirmed.keys.has(row.key));
  return { isUnread, markRead, ready, isReady };
}

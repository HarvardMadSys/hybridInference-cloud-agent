import { useCallback, useEffect, useRef, useState } from 'react';
import type { SidebarConversation } from './useConversationList';
import { getConversationState } from './conversationState';

const COMPLETED_STATES = new Set(['done', 'needs_review', 'failed', 'cancelled']);
interface Receipt {
  jobId: string;
  attemptId: number | null;
  turnNo: number | null;
}
type Receipts = Record<string, Receipt>;
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
  typeof value === 'number' && Number.isFinite(value) ? value : null;

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
  if (read.attemptId === completion.attemptId) return true;
  return (
    read.attemptId !== null &&
    completion.attemptId !== null &&
    read.attemptId > completion.attemptId
  );
}
function parseReceipts(raw: string | null): Receipts {
  try {
    const data = JSON.parse(raw ?? 'null');
    if (
      data?.version !== 2 ||
      !data.receipts ||
      typeof data.receipts !== 'object' ||
      Array.isArray(data.receipts)
    )
      return {};
    const receipts: Receipts = {};
    for (const [key, value] of Object.entries(data.receipts)) {
      if (
        !value ||
        typeof value !== 'object' ||
        !('jobId' in value) ||
        typeof value.jobId !== 'string'
      )
        continue;
      receipts[key] = {
        jobId: value.jobId,
        attemptId: numberOrNull('attemptId' in value ? value.attemptId : null),
        turnNo: numberOrNull('turnNo' in value ? value.turnNo : null),
      };
    }
    return receipts;
  } catch {
    return {};
  }
}
function storedReceipts(userId: string | undefined): Receipts {
  if (!userId) return {};
  try {
    return parseReceipts(localStorage.getItem(conversationReadStorageKey(userId)));
  } catch {
    return {};
  }
}
function recordCompletion(
  row: SidebarConversation,
  userId: string | undefined,
  local: Receipts,
): Receipts {
  const completion = completionReceipt(row);
  if (!completion) return local;
  const persisted = storedReceipts(userId);
  const receipts = { ...local, ...persisted };
  if (!covers(receipts[row.key], completion)) receipts[row.key] = completion;
  if (userId && !covers(persisted[row.key], receipts[row.key])) {
    try {
      localStorage.setItem(
        conversationReadStorageKey(userId),
        JSON.stringify({ version: 2, receipts }),
      );
    } catch {
      // Opening a task still clears its dot when persistence is unavailable.
    }
  }
  window.dispatchEvent(
    new CustomEvent<ReadEventDetail>(CONVERSATION_READ_EVENT, {
      detail: { userId, receipts, key: row.key },
    }),
  );
  return receipts;
}

/** Detail views can mark a completion read even while its sidebar row is not rendered. */
export function markConversationRead(row: SidebarConversation, userId?: string): void {
  if (completionReceipt(row)) recordCompletion(row, userId, {});
}

/** Remember only completions the user opened; listing history does not mark it read. */
export function useConversationReadState(userId?: string): {
  isUnread: (row: SidebarConversation) => boolean;
  markRead: (row: SidebarConversation) => void;
} {
  const [state, setState] = useState<ReadState>({ userId, receipts: {} });
  const current = useRef(state);
  const owner = useRef(userId);
  owner.current = userId;

  useEffect(() => {
    const receipts = {
      ...storedReceipts(userId),
      // A child may mark an active completion before this mount effect runs.
      ...(current.current.userId === userId ? current.current.receipts : {}),
    };
    current.current = { userId, receipts };
    setState(current.current);
    const onRead = (event: Event) => {
      const detail = (event as CustomEvent<ReadEventDetail>).detail;
      if (owner.current !== userId || detail?.userId !== userId || !detail.receipts) return;
      const receipt = detail.receipts[detail.key];
      if (!receipt || covers(current.current.receipts[detail.key], receipt)) return;
      current.current = { userId, receipts: { ...current.current.receipts, ...detail.receipts } };
      setState(current.current);
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
      current.current = { userId, receipts: parseReceipts(event.newValue) };
      setState(current.current);
    };
    window.addEventListener(CONVERSATION_READ_EVENT, onRead);
    window.addEventListener('storage', onStorage);
    return () => {
      window.removeEventListener(CONVERSATION_READ_EVENT, onRead);
      window.removeEventListener('storage', onStorage);
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

  return { isUnread, markRead };
}

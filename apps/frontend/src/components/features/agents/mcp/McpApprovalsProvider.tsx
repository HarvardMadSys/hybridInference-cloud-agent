'use client';

import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { decideMcpInvocation, listMcpInvocations, type McpInvocationApi } from '@/lib/api/mcp';
import { onAgentRecordsChanged, PUSH_FALLBACK_POLL_MS, useAgentPushLive } from '../agentPush';

interface Approvals {
  invocations: McpInvocationApi[];
  loaded: boolean;
  loading: boolean;
  hasMore: boolean;
  error: string | null;
  decidingIds: Set<number>;
  decisionErrors: Record<number, string>;
  lastDecision: McpInvocationApi | null;
  refresh: () => void;
  loadMore: () => void;
  decide: (invocation: McpInvocationApi, approve: boolean) => void;
}

const EMPTY: Approvals = {
  invocations: [],
  loaded: false,
  loading: false,
  hasMore: false,
  error: null,
  decidingIds: new Set(),
  decisionErrors: {},
  lastDecision: null,
  refresh: () => {},
  loadMore: () => {},
  decide: () => {},
};
const ApprovalsContext = createContext<Approvals>(EMPTY);

export function useMcpApprovals(): Approvals {
  return useContext(ApprovalsContext);
}

/** A new account gets a new queue before any previous account's bodies can render. */
export function McpApprovalsProvider({
  ownerId,
  enabled,
  children,
}: {
  ownerId: string | undefined;
  enabled: boolean;
  children: React.ReactNode;
}) {
  if (!ownerId || !enabled) {
    return <ApprovalsContext.Provider value={EMPTY}>{children}</ApprovalsContext.Provider>;
  }
  return <OwnerApprovals key={ownerId}>{children}</OwnerApprovals>;
}

function OwnerApprovals({ children }: { children: React.ReactNode }) {
  const [invocations, setInvocations] = useState<McpInvocationApi[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [decidingIds, setDecidingIds] = useState(new Set<number>());
  const [decisionErrors, setDecisionErrors] = useState<Record<number, string>>({});
  const [lastDecision, setLastDecision] = useState<McpInvocationApi | null>(null);
  const mounted = useRef(true);
  const reading = useRef(false);
  const again = useRef<boolean | null>(null);
  const pageCount = useRef(1);
  const decisions = useRef(new Set<number>());
  // A read begun before a local decision must not restore its old approval button.
  const decided = useRef(new Set<number>());
  const pushLive = useAgentPushLive();

  const refresh = useCallback(async (background = false) => {
    if (background && document.visibilityState === 'hidden') return;
    if (reading.current) {
      // A queued user action takes priority over a background refresh.
      again.current = again.current === false ? false : background;
      return;
    }
    reading.current = true;
    setLoading(true);
    try {
      const rows = new Map<number, McpInvocationApi>();
      let beforeId: number | undefined;
      let more = false;
      // Refresh every loaded page. Otherwise expired calls from older pages
      // would stay visible after the head of the queue was read again.
      for (let page = 0; page < pageCount.current; page += 1) {
        if (background && document.visibilityState === 'hidden') return;
        const response = await listMcpInvocations({
          pendingOnly: true,
          limit: 50,
          ...(beforeId === undefined ? {} : { beforeId }),
        });
        if (!mounted.current) return;
        for (const invocation of response.invocations) {
          if (invocation.status === 'pending' && !decided.current.has(invocation.id)) {
            rows.set(invocation.id, invocation);
          }
        }
        const cursor = response.next_before_id;
        more = Boolean(response.has_more && cursor != null);
        if (!more || cursor == null || (beforeId !== undefined && cursor >= beforeId)) break;
        beforeId = cursor;
      }
      if (!mounted.current) return;
      setInvocations(
        [...rows.values()]
          .filter((invocation) => !decided.current.has(invocation.id))
          .sort((a, b) => b.id - a.id),
      );
      setHasMore(more);
      setLoaded(true);
      setError(null);
    } catch (cause) {
      if (mounted.current) {
        setError(cause instanceof Error ? cause.message : 'Could not load tool approvals.');
      }
    } finally {
      reading.current = false;
      if (mounted.current) {
        setLoading(false);
        if (again.current !== null) {
          const background = again.current;
          again.current = null;
          void refresh(background);
        }
      }
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    const refreshInBackground = () => void refresh(true);
    refreshInBackground();
    const timer = setInterval(refreshInBackground, pushLive ? PUSH_FALLBACK_POLL_MS : 3000);
    const unsubscribe = onAgentRecordsChanged((change) => {
      if (change.resync || change.mcp.length || change.jobs.some((job) => job.deleted)) {
        refreshInBackground();
      }
    });
    document.addEventListener('visibilitychange', refreshInBackground);
    return () => {
      clearInterval(timer);
      unsubscribe();
      document.removeEventListener('visibilitychange', refreshInBackground);
    };
  }, [refresh, pushLive]);

  useEffect(() => {
    const now = Date.now();
    const deadlines = invocations
      .map((invocation) => Date.parse(invocation.expires_at ?? ''))
      .filter((deadline) => Number.isFinite(deadline) && deadline > now);
    if (!deadlines.length) return;
    // Expiry does not depend on receiving another server event. Only future
    // deadlines schedule a read, so clock skew cannot create a refresh loop.
    const timer = setTimeout(
      () => void refresh(true),
      Math.min(Math.min(...deadlines) - now + 5, 2_147_483_647),
    );
    return () => clearTimeout(timer);
  }, [invocations, refresh]);

  function loadMore() {
    if (!hasMore || reading.current) return;
    pageCount.current += 1;
    void refresh();
  }

  async function decide(invocation: McpInvocationApi, approve: boolean) {
    if (decisions.current.has(invocation.id)) return;
    decisions.current.add(invocation.id);
    setDecidingIds(new Set(decisions.current));
    setDecisionErrors((current) => ({ ...current, [invocation.id]: '' }));
    try {
      const result = await decideMcpInvocation(invocation.id, approve);
      if (!mounted.current) return;
      if (result.status !== 'pending') {
        decided.current.add(invocation.id);
        setInvocations((current) => current.filter((call) => call.id !== invocation.id));
      }
      setLastDecision(result);
      void refresh();
    } catch (cause) {
      if (!mounted.current) return;
      setDecisionErrors((current) => ({
        ...current,
        [invocation.id]: cause instanceof Error ? cause.message : 'Could not record the decision.',
      }));
      // Another tab or the deadline may have won; re-read the conditional result.
      void refresh();
    } finally {
      decisions.current.delete(invocation.id);
      if (mounted.current) setDecidingIds(new Set(decisions.current));
    }
  }

  return (
    <ApprovalsContext.Provider
      value={{
        invocations,
        loaded,
        loading,
        hasMore,
        error,
        decidingIds,
        decisionErrors,
        lastDecision,
        refresh: () => void refresh(),
        loadMore,
        decide: (invocation, approve) => void decide(invocation, approve),
      }}
    >
      {children}
    </ApprovalsContext.Provider>
  );
}

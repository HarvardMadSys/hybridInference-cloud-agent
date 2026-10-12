'use client';

import { useEffect, useState } from 'react';
import { listMcpInvocations } from '@/lib/api/mcp';

import { onAgentRecordsChanged, PUSH_FALLBACK_POLL_MS, useAgentPushLive } from '../agentPush';

/** Keep pending decisions visible while the conversation's tool pane is closed. */
export function McpApprovalNotice({
  jobId,
  active,
  onReview,
}: {
  jobId: string;
  active: boolean;
  onReview: () => void;
}) {
  const [count, setCount] = useState(0);
  // A call held for approval is pushed the moment it is created or decided;
  // the clock is the backstop for when the owner's change stream is not up.
  const pushLive = useAgentPushLive();
  useEffect(() => {
    setCount(0);
    if (!active) return;
    let disposed = false;
    let loading = false;
    let again = false;
    const refresh = async () => {
      if (loading) {
        again = true;
        return;
      }
      loading = true;
      try {
        const result = await listMcpInvocations({ jobId, pendingOnly: true });
        if (!disposed)
          setCount(result.invocations.filter((call) => call.status === 'pending').length);
      } catch {
        // Keep an already visible decision available during a transient poll failure.
      } finally {
        loading = false;
        if (again && !disposed) {
          again = false;
          void refresh();
        }
      }
    };
    void refresh();
    const timer = setInterval(refresh, pushLive ? PUSH_FALLBACK_POLL_MS : 3000);
    const unsubscribe = onAgentRecordsChanged((change) => {
      if (change.resync || change.mcp.includes(jobId)) void refresh();
    });
    return () => {
      disposed = true;
      clearInterval(timer);
      unsubscribe();
    };
  }, [active, jobId, pushLive]);

  if (!count) return null;
  return (
    <div
      role="status"
      className="mb-3 flex items-center justify-between gap-3 rounded-xl border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-100"
    >
      <span>
        {count === 1
          ? 'A tool call needs your approval.'
          : `${count} tool calls need your approval.`}{' '}
        The agent will continue after you decide.
      </span>
      <button
        type="button"
        onClick={onReview}
        className="shrink-0 rounded-lg border border-current px-3 py-1.5 font-medium"
      >
        Review tools
      </button>
    </div>
  );
}

'use client';

import { useEffect, useState } from 'react';
import { useAuth } from '@/components/providers';
import { listMcpInvocations, type McpInvocationApi } from '@/lib/api/mcp';
import { onAgentRecordsChanged, PUSH_FALLBACK_POLL_MS, useAgentPushLive } from './agentPush';
import { useMcpApprovals } from './mcp/McpApprovalsProvider';
import type { AgentJob } from './types';
import { useNativeQuestions } from './useNativeQuestions';

export type SessionReview = 'question' | 'approval';

type ReviewJob = Pick<
  AgentJob,
  'id' | 'state' | 'threadId' | 'currentAttemptId' | 'cancelRequested'
>;

function isPending(
  call: McpInvocationApi,
  jobId: string,
  threadId?: string,
  attemptId?: number | null,
): boolean {
  const expires = Date.parse(call.expires_at ?? '');
  return (
    call.job_id === jobId &&
    call.thread_id === threadId &&
    typeof attemptId === 'number' &&
    call.attempt_id === attemptId &&
    call.status === 'pending' &&
    (!Number.isFinite(expires) || expires > Date.now())
  );
}

/** A live decision is independent of both the run outcome and unread results. */
export function useSessionReview(job: ReviewJob): SessionReview | null {
  const { state } = useAuth();
  const ownerId = state.user?.id;
  const active = job.state === 'running' && !job.cancelRequested;
  const questions = useNativeQuestions({
    threadId: job.threadId ?? '',
    jobId: job.id,
    attemptId: job.currentAttemptId,
    active,
  });
  const approvals = useMcpApprovals();
  const pushLive = useAgentPushLive();
  const decidedId =
    approvals.lastDecision?.status !== 'pending' ? approvals.lastDecision?.id : undefined;
  const pending = approvals.invocations.find(
    (call) => call.id !== decidedId && isPending(call, job.id, job.threadId, job.currentAttemptId),
  );
  // The owner queue is paginated. A row outside its loaded pages still needs
  // a marker, without loading the entire owner's queue for every conversation.
  const lookup = active && approvals.hasMore && !pending;
  // A local decision fences cached rows and reads that began before it.
  const scope = JSON.stringify([
    ownerId,
    job.id,
    job.threadId,
    job.currentAttemptId,
    active,
    decidedId,
  ]);
  const [fallback, setFallback] = useState<{
    scope: string;
    invocation: McpInvocationApi | null;
  } | null>(null);
  const [clock, setClock] = useState(0);
  const invocation = pending ?? (lookup && fallback?.scope === scope ? fallback.invocation : null);

  useEffect(() => {
    if (!lookup) {
      setFallback(null);
      return;
    }
    let disposed = false;
    let reading = false;
    let again = false;
    async function refresh() {
      if (disposed || document.visibilityState === 'hidden') return;
      if (reading) {
        again = true;
        return;
      }
      reading = true;
      try {
        const result = await listMcpInvocations({ jobId: job.id, pendingOnly: true, limit: 1 });
        if (!disposed)
          setFallback({
            scope,
            invocation:
              result.invocations.find(
                (call) =>
                  call.id !== decidedId &&
                  isPending(call, job.id, job.threadId, job.currentAttemptId),
              ) ?? null,
          });
      } catch {
        // Preserve a known pending decision during a temporary read failure.
      } finally {
        reading = false;
        if (again && !disposed) {
          again = false;
          void refresh();
        }
      }
    }
    void refresh();
    const timer = setInterval(refresh, pushLive ? PUSH_FALLBACK_POLL_MS : 3000);
    const unsubscribe = onAgentRecordsChanged((change) => {
      if (change.resync || change.mcp.includes(job.id)) void refresh();
    });
    document.addEventListener('visibilitychange', refresh);
    return () => {
      disposed = true;
      clearInterval(timer);
      unsubscribe();
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [lookup, job.id, job.threadId, job.currentAttemptId, scope, decidedId, pushLive, clock]);

  useEffect(() => {
    const expires = Date.parse(invocation?.expires_at ?? '');
    if (!Number.isFinite(expires) || expires <= Date.now()) return;
    const timer = setTimeout(
      () => setClock((value) => value + 1),
      Math.min(expires - Date.now() + 5, 2_147_483_647),
    );
    return () => clearTimeout(timer);
  }, [invocation, clock]);

  if (!active) return null;
  if (questions.request) return 'question';
  return invocation && isPending(invocation, job.id, job.threadId, job.currentAttemptId)
    ? 'approval'
    : null;
}

'use client';

import { useT } from '@/components/providers';
import { cancelAgentJob, forkAgentJob, restoreAgentJob } from '@/lib/api/agents';
import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
import toast from 'react-hot-toast';
import { hasLostContext } from '../conversationJob';
import type { AgentJob } from '../types';

/** Turn lifecycle mutations, including their duplicate-request guards and shortcuts. */
export function useJobRunActions({
  job,
  userId,
  onReload,
  onError,
}: {
  job: AgentJob;
  userId?: string;
  onReload?: () => void;
  onError: (error: string | null) => void;
}) {
  const router = useRouter();
  const t = useT();
  const isActive = job.state === 'running' || job.state === 'queued';
  const isArchived = Boolean(job.archivedAt);
  const contextLost = hasLostContext(job);
  const stopRequestedRef = useRef(false);
  const forkRequestedRef = useRef(false);
  const [stopping, setStopping] = useState(false);
  const [forking, setForking] = useState(false);
  const [restoring, setRestoring] = useState(false);
  useEffect(() => {
    stopRequestedRef.current = false;
    setStopping(false);
    forkRequestedRef.current = false;
    setForking(false);
  }, [job.id]);

  useEffect(() => {
    if (!isActive) {
      stopRequestedRef.current = false;
      setStopping(false);
    }
  }, [isActive]);

  const stop = useCallback(async () => {
    if (stopRequestedRef.current) return;
    stopRequestedRef.current = true;
    setStopping(true);
    onError(null);
    try {
      await cancelAgentJob(job.id);
      onReload?.();
    } catch (cause: unknown) {
      stopRequestedRef.current = false;
      setStopping(false);
      onError(cause instanceof Error ? cause.message : 'Could not stop this run');
    }
  }, [job.id, onReload, onError]);

  useEffect(() => {
    if (!isActive) return;
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (
        event.key !== 'Escape' ||
        event.repeat ||
        event.defaultPrevented ||
        target?.closest('input, textarea, select, [contenteditable="true"]')
      ) {
        return;
      }
      event.preventDefault();
      void stop();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [isActive, stop]);

  // Fork creates a second native session in this chat's shared workspace and
  // navigates to its copied display history; draft prefills Edit & rewind.
  async function forkFrom(anchorJobId: string, draft?: string) {
    if (forkRequestedRef.current || isArchived) return;
    if (
      job.workspaceShared &&
      typeof globalThis.confirm === 'function' &&
      !globalThis.confirm(
        'This creates another chat sharing the same workspace. Concurrent edits can conflict or overwrite each other. Continue?',
      )
    ) {
      return;
    }
    forkRequestedRef.current = true;
    setForking(true);
    onError(null);
    try {
      const fork = await forkAgentJob(anchorJobId);
      if (draft) {
        try {
          sessionStorage.setItem(`agent-rewind-draft-${fork.id}`, draft);
        } catch {
          // Blocked storage only loses the prefill, never the native fork.
        }
      }
      toast.success('Conversation forked');
      router.push(`/${fork.id}`);
      // Keep the source mutation guard closed until the child job actually
      // replaces it. App Router intentionally leaves this component showing
      // the source while the dynamic route loads; reopening here permits a
      // second click with a newly cleared idempotency key.
    } catch (cause: unknown) {
      forkRequestedRef.current = false;
      onError(cause instanceof Error ? cause.message : 'Could not fork the conversation');
      setForking(false);
    }
  }

  async function unarchive() {
    if (restoring) return;
    setRestoring(true);
    onError(null);
    try {
      await restoreAgentJob(job.id);
      onReload?.();
    } catch (cause: unknown) {
      onError(cause instanceof Error ? cause.message : t('sessionArchive.restoreFailed'));
    } finally {
      setRestoring(false);
    }
  }

  const canForkWorkspace = Boolean(
    job.sandboxId && job.workspaceShared && !isActive && !isArchived,
  );
  const canForkCurrent = Boolean(canForkWorkspace && job.nativeForkAvailable);
  const canReview = Boolean(userId && canForkCurrent && !contextLost);
  return {
    stopping,
    forking,
    restoring,
    stop,
    forkFrom,
    unarchive,
    canForkWorkspace,
    canForkCurrent,
    canReview,
  };
}

'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import toast from 'react-hot-toast';
import { forkAgentJob, getAgentJob, getAgentJobThread, type AgentJobApi } from '@/lib/api/agents';
import { announceAgentSessionsChanged, onAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { APIError } from '@/lib/utils/errors';
import { useT } from '@/components/providers/LocaleProvider';
import { hasLostContext, isBusyJob, latestJob } from './conversationJob';
import { sideChatKeys } from './sideChatStorage';
import { sideChatAnchor, type SideChatTab } from './useWorkspaceTabs';
import type { AgentJob } from './types';

export interface SideChatSession {
  anchorJobId: string;
  sandboxId: string;
  operation: { id: string; state: 'new' | 'unknown' | 'busy' | 'failed' };
  jobId?: string;
  threadId?: string;
}
type Status = 'preparing' | 'waiting' | 'ready' | 'error';
export const sideChatStorageKey = (userId: string, threadId: string) =>
  sideChatKeys(userId, threadId).session;

function read(key: string): SideChatSession | null {
  const raw = localStorage.getItem(key);
  if (!raw) return null;
  try {
    const value = JSON.parse(raw) as SideChatSession;
    if (
      !value ||
      !value.anchorJobId ||
      !value.sandboxId ||
      typeof value.anchorJobId !== 'string' ||
      typeof value.sandboxId !== 'string' ||
      typeof value.operation?.id !== 'string' ||
      !value.operation.id ||
      !['new', 'unknown', 'busy', 'failed'].includes(value.operation.state) ||
      (value.jobId !== undefined && typeof value.jobId !== 'string') ||
      (value.threadId !== undefined && typeof value.threadId !== 'string') ||
      Boolean(value.jobId) !== Boolean(value.threadId)
    )
      throw new Error();
    return value;
  } catch {
    // Do not cache malformed records or expose parser internals. Recovery is
    // explicit because discarding an unknown request may abandon a real child.
    throw new Error('sideChat.invalidRecord');
  }
}
function save(key: string, session: SideChatSession) {
  localStorage.setItem(key, JSON.stringify(session));
}
export function childSession(
  source: AgentJob,
  session: SideChatSession,
  child: AgentJobApi,
): SideChatSession | null {
  if (
    !child.id ||
    !child.thread_id ||
    child.thread_id === source.threadId ||
    (session.threadId && session.threadId !== child.thread_id) ||
    child.sandbox_id !== session.sandboxId ||
    child.sandbox_id !== source.sandboxId ||
    child.fork_anchor_job_id !== session.anchorJobId
  )
    return null;
  return { ...session, jobId: child.id, threadId: child.thread_id };
}

export function useSideChatSession(
  source: AgentJob,
  userId: string | undefined,
  active: boolean,
  sourceJobs: AgentJobApi[] = [],
  tab?: SideChatTab,
) {
  const t = useT();
  const keys = userId ? sideChatKeys(userId, source.threadId ?? source.id, tab?.id) : null;
  const key = keys?.session ?? '';
  const legacyKey =
    userId && (!tab || tab.id === 'side-chat')
      ? `agent-side-chat:${userId}:${source.threadId ?? source.id}`
      : '';
  const [session, setSession] = useState<SideChatSession | null>(null);
  const [status, setStatus] = useState<Status>('preparing');
  const [error, setError] = useState<string | null>(null);
  const [recoverable, setRecoverable] = useState(false);
  const [revision, setRevision] = useState(0);
  const sourceRef = useRef({ source, sourceJobs, tab });
  sourceRef.current = { source, sourceJobs, tab };
  const retry = useCallback(() => setRevision((n) => n + 1), []);

  useEffect(() => {
    if (!active || !key) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let delay = 3000;
    let busy = false;
    const snapshot = sourceRef.current;
    const canStart = () => !disposed && document.visibilityState !== 'hidden';
    const run = async () => {
      if (!canStart() || busy) return;
      busy = true;
      setStatus((previous) => (previous === 'waiting' ? previous : 'preparing'));
      setError(null);
      setRecoverable(false);
      try {
        // The lock spans read/POST/receipt persistence. A second window always
        // re-reads the first window's receipt before deciding to create a child.
        if (!navigator.locks) throw new Error('sideChat.storageUnavailable');
        const result = await navigator.locks.request(key, async () => {
          if (!canStart()) return null;
          let saved = read(key);
          if (!saved && legacyKey && sessionStorage.getItem(legacyKey)) {
            // Preserve receipts and unknown operation IDs from the first UI
            // version; a namespace change must never create another child.
            try {
              const legacy = JSON.parse(sessionStorage.getItem(legacyKey)!);
              const pending = sessionStorage.getItem(
                `agent-native-fork-request-${legacyKey}:${legacy.anchorJobId}`,
              );
              if (legacy.requestSent && !legacy.jobId && !pending) throw new Error();
              save(key, {
                ...legacy,
                operation: {
                  id: pending ?? crypto.randomUUID(),
                  state: pending ? 'unknown' : 'new',
                },
              });
              saved = read(key);
              sessionStorage.removeItem(legacyKey);
            } catch {
              throw new Error('sideChat.invalidRecord');
            }
          }
          if (!saved) {
            const { source, sourceJobs } = snapshot;
            if (
              !source.threadId ||
              !source.sandboxId ||
              !source.workspaceShared ||
              source.archivedAt
            )
              throw new Error('sideChat.unavailable');
            const anchor = snapshot.tab?.anchor ?? sideChatAnchor(source, sourceJobs);
            saved = {
              anchorJobId: anchor.jobId,
              sandboxId: anchor.sandboxId,
              operation: { id: crypto.randomUUID(), state: 'new' },
            };
            save(key, saved);
          }
          if (saved.jobId) {
            const thread = await getAgentJobThread(saved.jobId);
            if (!thread) {
              localStorage.removeItem(key);
              throw new Error('sideChat.deleted');
            }
            const latest = latestJob(thread.jobs);
            const restored =
              latest && thread.thread_id === saved.threadId
                ? childSession(snapshot.source, saved, latest)
                : null;
            if (!restored) throw new Error('sideChat.invalidChild');
            save(key, restored);
            return restored;
          }
          const anchor = await getAgentJob(saved.anchorJobId);
          if (!canStart()) return null;
          if (
            anchor.thread_id !== snapshot.source.threadId ||
            anchor.sandbox_id !== saved.sandboxId ||
            !snapshot.source.sandboxId
          )
            throw new Error('sideChat.sourceChanged');
          // Only an ambiguous POST bypasses liveness to recover the SAME key.
          // A known busy/failed response never grants that exception.
          if (saved.operation.state !== 'unknown') {
            if (
              anchor.archived_at ||
              hasLostContext({
                nativeState: anchor.native_state ?? undefined,
                contextStatus: anchor.context_status ?? undefined,
              }) ||
              ['failed', 'cancelled'].includes(anchor.state)
            )
              throw new Error('sideChat.unfinished');
            if (isBusyJob(anchor)) return null;
            if (!anchor.native_fork_available) throw new Error('sideChat.unavailable');
            if (saved.operation.state === 'failed')
              saved.operation = { id: crypto.randomUUID(), state: 'new' };
          }
          saved.operation.state = 'unknown';
          save(key, saved);
          try {
            const child = await forkAgentJob(saved.anchorJobId, { requestId: saved.operation.id });
            // Persist the receipt before validating it. Neither a validation
            // error nor a reload can turn this committed operation into a fork.
            if (child.id && child.thread_id)
              saved = { ...saved, jobId: child.id, threadId: child.thread_id };
            save(key, saved);
            const validated = childSession(snapshot.source, saved, child);
            if (!validated) throw new Error('sideChat.invalidChild');
            announceAgentSessionsChanged({ threadIds: [child.thread_id!] });
            if (canStart()) toast.success(t('sideChat.created'));
            return validated;
          } catch (cause) {
            if (
              cause instanceof APIError &&
              ['sandbox_busy', 'fork_in_progress', 'native_fork_failed'].includes(cause.code)
            ) {
              saved.operation.state = cause.code === 'native_fork_failed' ? 'failed' : 'busy';
              save(key, saved);
              if (cause.code !== 'native_fork_failed') return null;
            }
            throw cause;
          }
        });
        if (disposed) return;
        if (result) {
          setSession(result);
          setStatus('ready');
        } else {
          setStatus('waiting');
          timer = setTimeout(() => {
            void run();
          }, delay);
          delay = Math.min(delay * 2, 30000);
        }
      } catch (cause) {
        if (disposed) return;
        const message = cause instanceof Error ? cause.message : 'sideChat.openFailed';
        setStatus('error');
        setError(message);
        setRecoverable(
          [
            'sideChat.invalidRecord',
            'sideChat.invalidChild',
            'sideChat.sourceChanged',
            'sideChat.unfinished',
          ].includes(message),
        );
      } finally {
        busy = false;
      }
    };
    const wake = () => {
      if (timer) clearTimeout(timer);
      if (canStart()) void run();
    };
    void run();
    document.addEventListener('visibilitychange', wake);
    const storage = (event: StorageEvent) => {
      if (event.key === key) wake();
    };
    window.addEventListener('storage', storage);
    const unsubscribe = onAgentSessionsChanged((change) => {
      let saved: SideChatSession | null;
      try {
        saved = read(key);
      } catch {
        return;
      }
      if (saved?.threadId && change.threadIds.includes(saved.threadId)) {
        if (change.deleted) {
          localStorage.removeItem(key);
          setSession(null);
          setStatus('error');
          setError('sideChat.deleted');
        } else wake();
      }
    });
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', wake);
      window.removeEventListener('storage', storage);
      unsubscribe();
    };
  }, [active, key, legacyKey, revision, t]);

  const advance = useCallback(
    (job: AgentJobApi): boolean => {
      if (!session || !key) return false;
      const next = childSession(sourceRef.current.source, session, job);
      if (!next) return false;
      save(key, next);
      setSession(next);
      return true;
    },
    [key, session],
  );
  const restart = useCallback(async () => {
    if (!key || !navigator.locks) return;
    await navigator.locks.request(key, () => {
      localStorage.removeItem(key);
      sessionStorage.removeItem(legacyKey);
    });
    setSession(null);
    retry();
  }, [key, legacyKey, retry]);
  return { session, status, error, recoverable, retry, restart, advance };
}

'use client';

// One conversation's Context view data, for its main Context tab (which
// `/context` opens). The page holds one provider, so the conversation is read
// once and polled once while a turn runs, and not at all while the tab is not
// showing.
//
// The data is kept with the conversation it was read for. When the page moves
// to another conversation it is dropped before anything draws, so a surface
// never shows one conversation's context under another's title.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useLocale } from '@/components/providers';
import { getAgentThreadContext, type ThreadContextApi } from '@/lib/api/agents';
import { isActiveConversationState, isActiveJobState } from '../conversationJob';
import type { AgentJob } from '../types';

/** How often a running conversation's view is read again. */
export const CONTEXT_REFRESH_MS = 5_000;

export interface ThreadContextData {
  /** The conversation this data belongs to. */
  key: string;
  view: ThreadContextApi | null;
  missing: boolean;
  error: string | null;
  retry: () => void;
}

interface Store extends ThreadContextData {
  /** Count one showing surface while it is mounted and visible. */
  watch: () => () => void;
  /** Where this page shows the context when asked (`/context`). */
  open?: () => void;
}

const ThreadContextStore = createContext<Store | null>(null);

type ContextJob = Pick<AgentJob, 'id' | 'threadId' | 'state' | 'conversationState'>;

/** The conversation a job belongs to, as this page keys it. */
export function threadContextKey(job: Pick<AgentJob, 'id' | 'threadId'>): string {
  return job.threadId ?? job.id;
}

interface Loaded {
  key: string;
  view: ThreadContextApi | null;
  missing: boolean;
  error: string | null;
}

export function ThreadContextProvider({
  job,
  onOpen,
  children,
}: {
  job: ContextJob;
  /** How this page shows the context when the owner asks for it. */
  onOpen?: () => void;
  children: ReactNode;
}) {
  const { t } = useLocale();
  const key = threadContextKey(job);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [watchers, setWatchers] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const jobId = useRef(job.id);
  jobId.current = job.id;
  const watching = watchers > 0;
  // Only this conversation's data is ever handed out.
  const current = loaded?.key === key ? loaded : null;

  const load = useCallback(
    async (signal: AbortSignal) => {
      try {
        const next = await getAgentThreadContext(jobId.current, signal);
        if (signal.aborted) return;
        setLoaded({ key, view: next, missing: next === null, error: null });
      } catch (cause) {
        if (signal.aborted) return;
        const message =
          cause instanceof Error && cause.message ? cause.message : t('context.loadError');
        setLoaded((previous) =>
          previous?.key === key
            ? { ...previous, error: message }
            : { key, view: null, missing: false, error: message },
        );
      }
    },
    [key, t],
  );

  useEffect(() => {
    if (!watching) return undefined;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, watching, attempt, job.state, job.conversationState]);

  // A running turn adds requests; read again while it runs and something shows it.
  const active =
    isActiveJobState(job.state) ||
    isActiveConversationState(job.conversationState) ||
    Boolean(current?.view?.latest?.turn_active);
  useEffect(() => {
    if (!watching || !active) return undefined;
    const controller = new AbortController();
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void load(controller.signal);
    }, CONTEXT_REFRESH_MS);
    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [watching, active, load]);

  const watch = useCallback(() => {
    setWatchers((count) => count + 1);
    return () => setWatchers((count) => count - 1);
  }, []);
  const retry = useCallback(() => setAttempt((value) => value + 1), []);
  const store = useMemo<Store>(
    () => ({
      key,
      view: current?.view ?? null,
      missing: current?.missing ?? false,
      error: current?.error ?? null,
      retry,
      watch,
      open: onOpen,
    }),
    [key, current, retry, watch, onOpen],
  );
  return <ThreadContextStore.Provider value={store}>{children}</ThreadContextStore.Provider>;
}

/** How the page shows its conversation's context, when it can. */
export function useOpenThreadContext(): (() => void) | undefined {
  return useContext(ThreadContextStore)?.open;
}

/** Whether a provider already holds this conversation's data. */
export function useHasThreadContextStore(): boolean {
  return useContext(ThreadContextStore) !== null;
}

/**
 * The shared data, counted as one more surface showing it while `visible`.
 * Must be inside a {@link ThreadContextProvider}.
 */
export function useThreadContextData(visible: boolean): ThreadContextData {
  const store = useContext(ThreadContextStore);
  if (!store) throw new Error('useThreadContextData needs a ThreadContextProvider');
  const { watch } = store;
  useEffect(() => (visible ? watch() : undefined), [visible, watch]);
  return store;
}

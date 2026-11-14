'use client';

import { usePathname } from 'next/navigation';
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import { getAgentConfig, listAgentConversations, type AgentConfigApi } from '@/lib/api/agents';

import { MODEL_CONNECTIONS_CHANGED } from './composer/modelConnectionEvents';
import { modelSetupRequired } from './composer/providedModels';

// First-run guidance: who gets it, how far along they are, and where it may
// speak — decided once, for the whole app.
//
// Someone is new until they have started a conversation. Archived ones count —
// putting a task away does not make its owner a stranger — and deleted ones do
// not, since they are gone from that owner's history. Progress is not stored:
// each step is read from what the owner has actually done, the model
// connection, GitHub connection and repositories `/config` reports, then the
// first conversation. A model the deployment provides counts as the model
// connection. Only Skip is remembered, per account in this browser, and
// Settings can undo it.
//
// The sidebar's getting-started row, the new-task page and the conversation
// page all read this one answer. Three places each asking for themselves would
// cost three requests and, sooner or later, three different answers.

export type FirstRunStepId = 'model' | 'connect' | 'repository' | 'task';

/** The sidebar's next step, and how many of the steps are behind the owner. */
export interface FirstRunStep {
  next: FirstRunStepId;
  done: number;
  total: number;
}

/**
 * The steps between this owner and a first task, in order, each with whether
 * it is behind them. A step nobody can take here is left out; with nothing to
 * set up, there is no list, and the new-task page is the whole story.
 */
export function firstRunSteps(config: AgentConfigApi): { id: FirstRunStepId; done: boolean }[] {
  const steps: { id: FirstRunStepId; done: boolean }[] = [];
  // Every task runs on a model connection, so this comes first. It is behind
  // an owner who has one of their own that can run, and equally behind one the
  // deployment provides a model to: there is nothing for them to add. Where the
  // deployment does not let owners add one, the new-task page says so, and
  // there is nothing to do here.
  if (config.model_connections_enabled === true) {
    steps.push({ id: 'model', done: !modelSetupRequired(config) });
  }
  // Nobody can connect while the deployment's App is down or absent. The
  // new-task page says which; a step that cannot be taken is left out.
  const github =
    config.github_load_status !== 'unavailable' &&
    (config.github_connected || Boolean(config.github_install_url));
  if (github) {
    steps.push({ id: 'connect', done: config.github_connected });
    steps.push({ id: 'repository', done: config.github_connected && config.repos.length > 0 });
  }
  if (steps.length === 0) return steps;
  steps.push({ id: 'task', done: false });
  return steps;
}

export interface FirstRun {
  /** No conversation yet. False until the answer is in, so nothing flashes. */
  newcomer: boolean;
  /** The owner's only conversation, while there is exactly one. */
  onlyThreadId: string | null;
  /** What the getting-started row shows; `null` when it shows nothing. */
  step: FirstRunStep | null;
  /** Put the getting-started row away for this account, in this browser. */
  skip: () => void;
  /** A newcomer who put the row away, so Settings offers it back. */
  hidden: boolean;
  /** Bring the getting-started row back. */
  show: () => void;
}

const NOTHING_KNOWN: FirstRun = {
  newcomer: false,
  onlyThreadId: null,
  step: null,
  skip: () => {},
  hidden: false,
  show: () => {},
};

const FirstRunContext = createContext<FirstRun>(NOTHING_KNOWN);

export function useFirstRun(): FirstRun {
  return useContext(FirstRunContext);
}

/** The new-task box, so guidance elsewhere on the page can hand it the caret. */
export const NEW_TASK_PROMPT_ID = 'new-task-prompt';

const skippedKey = (userId: string) =>
  `agents.gettingStarted.skipped.${encodeURIComponent(userId)}`;

interface History {
  /** Conversations counted up to two: two is enough to know it is not one. */
  count: 0 | 1 | 2;
  onlyThreadId: string | null;
}

/**
 * Keyed by the account in the layout, so a different account starts over.
 *
 * The question is asked when the app loads and again on each page change, but
 * only until the owner has two conversations. Until then the answer can still
 * change what someone is told: the first task is created on one page and
 * explained on the next. After it, nothing depends on it, so it is not asked
 * again until the next load.
 */
export function FirstRunProvider({ userId, children }: { userId?: string; children: ReactNode }) {
  const pathname = usePathname() ?? '';
  // `undefined` until the first answer, `null` when none could be had. A
  // failed re-check keeps the answer already in hand.
  const [history, setHistory] = useState<History | null | undefined>(undefined);
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  // Unknown until storage has been read, so a skipped row never flashes back.
  const [skipped, setSkipped] = useState<boolean | null>(null);
  // Counts model connection changes, so saving one finishes its step at once.
  const [connectionsChanged, setConnectionsChanged] = useState(0);
  const settled = history?.count === 2;
  const newcomer = history?.count === 0;

  useEffect(() => {
    if (!userId || settled) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const { conversations } = await listAgentConversations({ includeArchived: true, limit: 2 });
        if (cancelled) return;
        const only = conversations.length === 1 ? conversations[0].job : null;
        setHistory({
          count: Math.min(conversations.length, 2) as History['count'],
          onlyThreadId: only ? (only.thread_id ?? only.id) : null,
        });
      } catch {
        if (!cancelled) setHistory((current) => current ?? null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [userId, pathname, settled]);

  useEffect(() => {
    if (!userId) return;
    try {
      setSkipped(window.localStorage.getItem(skippedKey(userId)) === '1');
    } catch {
      setSkipped(false);
    }
  }, [userId]);

  // A connection saved in Settings, in this tab or another, can finish the
  // model step without a page change here.
  useEffect(() => {
    if (!newcomer) return undefined;
    const changed = () => setConnectionsChanged((count) => count + 1);
    const storage = (event: StorageEvent) => {
      if (event.key === MODEL_CONNECTIONS_CHANGED) changed();
    };
    window.addEventListener(MODEL_CONNECTIONS_CHANGED, changed);
    window.addEventListener('storage', storage);
    return () => {
      window.removeEventListener(MODEL_CONNECTIONS_CHANGED, changed);
      window.removeEventListener('storage', storage);
    };
  }, [newcomer]);

  // Where the owner is with their model and GitHub, asked of newcomers only:
  // nobody else is shown a step.
  useEffect(() => {
    if (!newcomer || skipped !== false) return undefined;
    let cancelled = false;
    (async () => {
      try {
        const next = await getAgentConfig();
        if (!cancelled) setConfig(next);
      } catch {
        // No step without it; the new-task page says what went wrong.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [newcomer, skipped, pathname, connectionsChanged]);

  const skip = useCallback(() => {
    setSkipped(true);
    if (!userId) return;
    try {
      window.localStorage.setItem(skippedKey(userId), '1');
    } catch {
      // Put away for this visit only.
    }
  }, [userId]);

  const show = useCallback(() => {
    setSkipped(false);
    if (!userId) return;
    try {
      window.localStorage.removeItem(skippedKey(userId));
    } catch {
      // Back for this visit only.
    }
  }, [userId]);

  const step = useMemo((): FirstRunStep | null => {
    if (!newcomer || skipped !== false || !config) return null;
    const steps = firstRunSteps(config);
    const next = steps.find((item) => !item.done);
    if (!next) return null;
    return {
      next: next.id,
      done: steps.filter((item) => item.done).length,
      total: steps.length,
    };
  }, [newcomer, skipped, config]);

  const value = useMemo(
    (): FirstRun => ({
      newcomer,
      onlyThreadId: history?.count === 1 ? history.onlyThreadId : null,
      step,
      skip,
      hidden: newcomer && skipped === true,
      show,
    }),
    [newcomer, history, step, skip, skipped, show],
  );

  return <FirstRunContext.Provider value={value}>{children}</FirstRunContext.Provider>;
}

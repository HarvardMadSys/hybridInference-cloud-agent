'use client';

import '@/styles/terminal.css';

import { useEffect, useId, useMemo, useRef, useState } from 'react';

import {
  createAgentTerminal,
  deleteAgentTerminal,
  listAgentTerminals,
  type AgentTerminalApi,
} from '@/lib/api/agents';

import { TerminalPane } from './TerminalPane';
import { loadTerminalRuntime } from './terminalRuntime';

// Compatibility fallback for sandbox managers that predate advertised limits.
const DEFAULT_MAX_SESSIONS = 4;

// xterm's cell box for this pane's font (13px monospace, 1.25 line height),
// and the chrome around it: the pane's padding, borders and header, xterm's
// scrollbar, and the note under the panes. Both are first guesses:
// `fitAddon.fit()` measures for real once the pane mounts and corrects the
// server. Guessing close is still worth it — it is the difference between the
// first prompt painting at the right width and painting at 80 columns and
// reflowing.
const CELL_WIDTH_PX = 7.8;
const CELL_HEIGHT_PX = 16.25;
const PANE_CHROME_X_PX = 40;
const PANE_CHROME_Y_PX = 96;
const DEFAULT_SIZE = { rows: 24, cols: 80 };

function estimateSize(host: HTMLElement | null): { rows: number; cols: number } {
  if (!host?.clientWidth || !host.clientHeight) return DEFAULT_SIZE;
  return {
    cols: Math.min(
      400,
      Math.max(40, Math.floor((host.clientWidth - PANE_CHROME_X_PX) / CELL_WIDTH_PX)),
    ),
    rows: Math.min(
      200,
      Math.max(10, Math.floor((host.clientHeight - PANE_CHROME_Y_PX) / CELL_HEIGHT_PX)),
    ),
  };
}

/**
 * Whether a shell ended on its own. As on the sandbox manager, only that frees its slot:
 * one still closing, or whose cleanup failed, may still be running something.
 */
function hasExited(session: AgentTerminalApi, exited: ReadonlySet<string>): boolean {
  return session.state === 'exited' || exited.has(session.id);
}

function sameIds(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((id, index) => id === right[index]);
}

interface TerminalWorkspaceProps {
  jobId: string;
  /**
   * The workspace the terminals belong to. Every turn of a chat is its own
   * job, but the turns share one workspace and its terminals, so a new turn
   * must not reset them. Defaults to the job.
   */
  workspaceKey?: string;
  active: boolean;
  ready?: boolean;
}

export function TerminalWorkspace({
  jobId,
  workspaceKey = jobId,
  active,
  ready = true,
}: TerminalWorkspaceProps) {
  const [sessions, setSessions] = useState<AgentTerminalApi[]>([]);
  const [exitedIds, setExitedIds] = useState<ReadonlySet<string>>(() => new Set());
  const [maxSessions, setMaxSessions] = useState(DEFAULT_MAX_SESSIONS);
  const [maxInputBytes, setMaxInputBytes] = useState<number | undefined>();
  const [visibleIds, setVisibleIds] = useState<string[]>([]);
  // The pane someone just opened or picked, which takes keyboard focus.
  const [focusId, setFocusId] = useState<string | null>(null);
  const workspaceId = useId();
  const focusTabRef = useRef<{ paneIndex: number; terminalId: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [loadFailed, setLoadFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loadedWorkspaceRef = useRef<string | null>(null);
  const currentWorkspaceRef = useRef(workspaceKey);
  const workspaceGenerationRef = useRef(0);
  const jobIdRef = useRef(jobId);
  jobIdRef.current = jobId;
  const mountedRef = useRef(false);
  const sectionRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  function requestIsCurrent(requestWorkspace: string, generation: number): boolean {
    return (
      mountedRef.current &&
      currentWorkspaceRef.current === requestWorkspace &&
      workspaceGenerationRef.current === generation
    );
  }

  useEffect(() => {
    currentWorkspaceRef.current = workspaceKey;
    workspaceGenerationRef.current += 1;
    setSessions([]);
    setExitedIds(new Set());
    setMaxSessions(DEFAULT_MAX_SESSIONS);
    setMaxInputBytes(undefined);
    setVisibleIds([]);
    setFocusId(null);
    focusTabRef.current = null;
    setError(null);
    setLoadFailed(false);
    setLoading(false);
    setBusy(false);
    loadedWorkspaceRef.current = null;
  }, [workspaceKey]);

  useEffect(() => {
    // The click that needs these has not happened yet; the fetch belongs
    // beside the create request, not after it.
    if (active) {
      // Prefetch is best effort. A pane reports its own load failure and a
      // later open can retry after the network recovers.
      void loadTerminalRuntime().catch(() => {});
    }
  }, [active]);

  // Keep the panes pointing at terminals that still exist, and keep one open
  // while any do.
  useEffect(() => {
    setVisibleIds((current) => {
      const available = new Set(sessions.map((session) => session.id));
      const next = current.filter((id) => available.has(id));
      if (next.length === 0 && sessions.length > 0) next.push(sessions[0].id);
      return sameIds(next, current) ? current : next;
    });
  }, [sessions]);

  useEffect(() => {
    if (!active || !ready || loadFailed || loadedWorkspaceRef.current === workspaceKey) return;
    const requestWorkspace = workspaceKey;
    const generation = workspaceGenerationRef.current;
    loadedWorkspaceRef.current = requestWorkspace;
    setLoading(true);
    void listAgentTerminals(jobId, { includeLimits: true })
      .then((response) => {
        if (!requestIsCurrent(requestWorkspace, generation)) return;
        const items = Array.isArray(response) ? response : response.terminals;
        const maximum = Array.isArray(response) ? undefined : response.max_sessions;
        const inputMaximum = Array.isArray(response) ? undefined : response.max_input_bytes;
        setMaxInputBytes(
          typeof inputMaximum === 'number' && Number.isInteger(inputMaximum) && inputMaximum >= 4
            ? inputMaximum
            : undefined,
        );
        setMaxSessions(
          typeof maximum === 'number' && Number.isInteger(maximum) && maximum > 0
            ? maximum
            : DEFAULT_MAX_SESSIONS,
        );
        setSessions((current) => {
          const listed = new Set(items.map((item) => item.id));
          // Creating no longer waits for this request, so a terminal opened
          // while it was in flight is legitimately absent from its answer.
          return [...items, ...current.filter((session) => !listed.has(session.id))];
        });
        setVisibleIds((current) =>
          current.length ? current : items.length ? [items[0].id] : current,
        );
        // Opening the panel shows a terminal ready to type into, unless one
        // opened meanwhile already has that claim.
        if (items.length) setFocusId((focused) => focused ?? items[0].id);
      })
      .catch((cause: unknown) => {
        if (requestIsCurrent(requestWorkspace, generation)) {
          loadedWorkspaceRef.current = null;
          setLoadFailed(true);
          setError(cause instanceof Error ? cause.message : 'Could not load terminals');
        }
      })
      .finally(() => {
        if (requestIsCurrent(requestWorkspace, generation)) setLoading(false);
      });
  }, [active, jobId, loadFailed, ready, workspaceKey]);

  const visible = useMemo(
    () => visibleIds.map((id) => sessions.find((session) => session.id === id)).filter(Boolean),
    [sessions, visibleIds],
  ) as AgentTerminalApi[];

  useEffect(() => {
    // Selecting another shell remounts its pane. Restore keyboard navigation
    // to its tab after that commit, without moving focus into the shell.
    const target = focusTabRef.current;
    if (!target) return;
    focusTabRef.current = null;
    document
      .getElementById(`${workspaceId}-pane-${target.paneIndex}-tab-${target.terminalId}`)
      ?.focus();
  }, [visibleIds, workspaceId]);

  // A shell that has ended keeps its tab for its output, but not its slot.
  const occupied = sessions.filter((session) => !hasExited(session, exitedIds)).length;
  const awaitingInitialLoad =
    active && !loadFailed && (!ready || loadedWorkspaceRef.current !== workspaceKey);
  // Two different waits, and only one of them is a precondition.
  //
  // The initial listing is not: it answers "which terminals already exist",
  // and it is a full round trip, so gating the first click on it made the
  // click wait for an answer it did not need. The cap is still enforced here
  // once the listing lands, and by the sandbox manager regardless.
  //
  // Workspace readiness is. There is no container to open a PTY in until the
  // job reports `workspace_ready`, so a create issued before then fails at the
  // sandbox manager — which is what happened when this gate was removed along with the
  // other one. The button stays visible and disabled rather than absent: the
  // wait is short and aiming at a control that is about to work beats hunting
  // for one that is not there yet.
  const canCreate = ready && !loadFailed && occupied < maxSessions;

  function retryLoad() {
    loadedWorkspaceRef.current = null;
    setError(null);
    setLoadFailed(false);
  }

  async function create(mode: 'new' | 'split', paneIndex = 0) {
    if (!canCreate || busy) return;
    const requestJobId = jobId;
    const requestWorkspace = workspaceKey;
    const generation = workspaceGenerationRef.current;
    setBusy(true);
    setError(null);
    try {
      const session = await createAgentTerminal(requestJobId, estimateSize(sectionRef.current));
      if (!requestIsCurrent(requestWorkspace, generation)) {
        void deleteAgentTerminal(requestJobId, session.id).catch(() => {
          // The old workspace may already have been reclaimed.
        });
        return;
      }
      setSessions((current) => {
        const next = [...current, session];
        // The sandbox manager made room the same way: the oldest ended shells go first.
        while (next.length > maxSessions) {
          const index = next.findIndex((candidate) => hasExited(candidate, exitedIds));
          if (index < 0) break;
          next.splice(index, 1);
        }
        return next;
      });
      setVisibleIds((current) => {
        if (mode === 'split') {
          return current.length > 0 ? [current[0], session.id] : [session.id];
        }
        if (current.length > 1) {
          const next = [...current];
          next[paneIndex] = session.id;
          return next;
        }
        return [session.id];
      });
      setFocusId(session.id);
    } catch (cause: unknown) {
      if (requestIsCurrent(requestWorkspace, generation)) {
        setError(cause instanceof Error ? cause.message : 'Could not create terminal');
      }
    } finally {
      if (requestIsCurrent(requestWorkspace, generation)) setBusy(false);
    }
  }

  async function kill(terminalId: string) {
    if (busy) return;
    const requestJobId = jobId;
    const requestWorkspace = workspaceKey;
    const generation = workspaceGenerationRef.current;
    setBusy(true);
    setError(null);
    try {
      await deleteAgentTerminal(requestJobId, terminalId);
      if (!requestIsCurrent(requestWorkspace, generation)) return;
      setSessions((current) => current.filter((session) => session.id !== terminalId));
    } catch (cause: unknown) {
      if (!requestIsCurrent(requestWorkspace, generation)) return;
      try {
        const refreshed = await listAgentTerminals(requestJobId);
        if (!requestIsCurrent(requestWorkspace, generation)) return;
        if (!refreshed.some((session) => session.id === terminalId)) {
          setSessions(refreshed);
          return;
        }
      } catch {
        // Preserve the original kill error when reconciliation is unavailable.
      }
      setError(cause instanceof Error ? cause.message : 'Could not kill terminal');
    } finally {
      if (requestIsCurrent(requestWorkspace, generation)) setBusy(false);
    }
  }

  function exited(terminalId: string, exitCode: number) {
    setExitedIds((current) =>
      current.has(terminalId) ? current : new Set(current).add(terminalId),
    );
    if (exitCode !== 0) return;
    // `exit` or Ctrl-D closes the terminal, as it would close a terminal
    // window. A shell that failed or was stopped stays open so its last
    // words can be read.
    setSessions((current) => current.filter((session) => session.id !== terminalId));
    void deleteAgentTerminal(jobIdRef.current, terminalId).catch(() => {
      // It has already exited; the sandbox manager reaps what this cannot close.
    });
  }

  function select(paneIndex: number, terminalId: string, focus: 'terminal' | 'tab') {
    focusTabRef.current = focus === 'tab' ? { paneIndex, terminalId } : null;
    setVisibleIds((current) => {
      const next = [...current];
      const otherPane = paneIndex === 0 ? 1 : 0;
      if (next[otherPane] === terminalId) {
        next[otherPane] = next[paneIndex];
      }
      next[paneIndex] = terminalId;
      return next;
    });
    setFocusId(focus === 'terminal' ? terminalId : null);
  }

  return (
    <section
      ref={sectionRef}
      aria-label="Workspace terminal"
      className="flex h-full min-h-0 flex-col overflow-y-auto"
    >
      {error ? (
        <p
          role="alert"
          className="mb-2 shrink-0 rounded-lg bg-red-50 dark:bg-red-500/10 px-3 py-2 text-xs text-red-600 dark:text-red-400"
        >
          {error}
          {/* A terminal opened while the listing was out hides the empty
              state's Retry, and nothing more opens until the listing lands. */}
          {loadFailed && visible.length ? (
            <button type="button" onClick={retryLoad} className="ml-2 underline">
              Retry
            </button>
          ) : null}
        </p>
      ) : null}
      {visible.length ? (
        <div
          className={`grid min-h-0 flex-1 auto-rows-[minmax(16rem,1fr)] gap-2 overflow-y-auto ${
            visible.length === 2
              ? 'grid-cols-[repeat(auto-fit,minmax(min(100%,24rem),1fr))]'
              : 'grid-cols-1'
          }`}
        >
          {visible.map((terminal, paneIndex) => (
            <TerminalPane
              key={`${paneIndex}-${terminal.id}`}
              jobId={jobId}
              terminal={terminal}
              sessions={sessions}
              exitedIds={exitedIds}
              paneIndex={paneIndex}
              idPrefix={`${workspaceId}-pane-${paneIndex}`}
              ready={ready}
              autoFocus={terminal.id === focusId}
              canCreate={canCreate}
              canSplit={canCreate && visible.length < 2}
              busy={busy}
              maxInputBytes={maxInputBytes}
              onSelect={(terminalId, focus) => select(paneIndex, terminalId, focus)}
              onNew={() => void create('new', paneIndex)}
              onSplit={() => void create('split')}
              onKill={() => void kill(terminal.id)}
              onExit={exited}
            />
          ))}
        </div>
      ) : (
        <div className="flex min-h-80 flex-1 flex-col items-center justify-center rounded-xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-6 text-center shadow-sm">
          <span className="mb-3 rounded-lg border border-gray-200 dark:border-white/10 p-2 text-gray-500 dark:text-gray-400">
            <svg aria-hidden="true" className="h-5 w-5" fill="none" viewBox="0 0 24 24">
              <rect
                x="3.5"
                y="4"
                width="17"
                height="16"
                rx="2"
                stroke="currentColor"
                strokeWidth="1.6"
              />
              <path
                d="m7 9 3 3-3 3m5 0h4"
                stroke="currentColor"
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth="1.6"
              />
            </svg>
          </span>
          <p className="text-sm font-medium text-gray-800 dark:text-gray-200">
            {loading || awaitingInitialLoad ? 'Loading terminals…' : 'No open terminals'}
          </p>
          <p className="mt-1 max-w-sm text-xs leading-5 text-gray-500 dark:text-gray-400">
            Terminal changes appear in this workspace&apos;s Git and Files views, but do not
            automatically update an existing pull request.
          </p>
          {loadFailed ? (
            <button
              type="button"
              onClick={retryLoad}
              className="mt-4 inline-flex items-center rounded-md border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-3 py-2 text-xs font-medium text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-white/5"
            >
              Retry
            </button>
          ) : (
            <button
              type="button"
              aria-label="New terminal"
              onClick={() => void create('new')}
              disabled={!canCreate || busy}
              className="mt-4 inline-flex items-center gap-1.5 rounded-md bg-gray-900 dark:bg-gray-100 px-3 py-2 text-xs font-medium text-white dark:text-gray-900 hover:bg-gray-800 dark:hover:bg-white disabled:opacity-50"
            >
              <span aria-hidden="true" className="text-base leading-none">
                +
              </span>
              {busy ? 'Starting…' : 'New terminal'}
            </button>
          )}
        </div>
      )}
      {visible.length ? (
        <p className="mt-2 shrink-0 px-1 text-[10px] leading-4 text-gray-400 dark:text-gray-500">
          Changes appear in Git and Files, but do not automatically update an existing pull request.
        </p>
      ) : null}
    </section>
  );
}

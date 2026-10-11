'use client';

import { useEffect, useRef } from 'react';
import type { ITheme, Terminal } from '@xterm/xterm';

import { loadTerminalRuntime } from './terminalRuntime';

import { useTheme } from '@/components/providers';
import type { ResolvedTheme } from '@/lib/theme';
import {
  resizeAgentTerminal,
  streamAgentTerminal,
  writeAgentTerminalInput,
  type AgentTerminalApi,
} from '@/lib/api/agents';

// xterm paints to a canvas from a JS palette, so it is the one surface a
// `dark:` class cannot reach. Both palettes live here, and the effect below
// swaps them on the live terminal rather than rebuilding it — a rebuild would
// drop the scrollback and re-open the PTY stream just to change a colour.
const TERMINAL_THEMES: Record<ResolvedTheme, ITheme> = {
  light: {
    background: '#ffffff',
    foreground: '#202124',
    cursor: '#202124',
    cursorAccent: '#ffffff',
    selectionBackground: '#dbeafe',
    black: '#202124',
    red: '#b42318',
    green: '#34766f',
    yellow: '#9a6700',
    blue: '#0969da',
    magenta: '#8250df',
    cyan: '#267f99',
    white: '#f6f8fa',
    brightBlack: '#6e7781',
    brightRed: '#cf222e',
    brightGreen: '#1a7f37',
    brightYellow: '#bf8700',
    brightBlue: '#218bff',
    brightMagenta: '#a475f9',
    brightCyan: '#3192aa',
    brightWhite: '#ffffff',
  },
  dark: {
    // Matches the pane's own `dark:bg-gray-900`, so the canvas does not sit as
    // a visibly different rectangle inside its frame.
    background: '#111827',
    foreground: '#e5e7eb',
    cursor: '#e5e7eb',
    cursorAccent: '#111827',
    selectionBackground: '#1e40af',
    black: '#484f58',
    red: '#ff7b72',
    green: '#3fb950',
    yellow: '#d29922',
    blue: '#58a6ff',
    magenta: '#bc8cff',
    cyan: '#39c5cf',
    white: '#b1bac4',
    brightBlack: '#6e7681',
    brightRed: '#ffa198',
    brightGreen: '#56d364',
    brightYellow: '#e3b341',
    brightBlue: '#79c0ff',
    brightMagenta: '#d2a8ff',
    brightCyan: '#56d4dd',
    brightWhite: '#f0f6fc',
  },
};

// The window size the API accepts. A size outside it is refused, which would
// leave the shell at its previous size, so it is clamped instead.
const MIN_ROWS = 2;
const MAX_ROWS = 200;
const MIN_COLS = 20;
const MAX_COLS = 500;

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

// What xterm sends on its own, in answer to a query in its output: device
// attributes, status and cursor reports, mode and window reports (CSI ... c,
// n, R, $y, t, u), and DCS or OSC reports such as its version or a colour.
// Keystrokes take other shapes, so they still pass while history replays.
const TERMINAL_REPLY =
  /^(?:\x1b\[[?>]?[\d;]*(?:\$y|[cnRtu])|\x1bP[\s\S]*\x1b\\|\x1b\][\s\S]*(?:\x07|\x1b\\))$/;

function decodeBase64(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

/** xterm hands binary input over as a string of byte values. */
function binaryBytes(value: string): Uint8Array {
  const bytes = new Uint8Array(value.length);
  for (let index = 0; index < value.length; index += 1) {
    bytes[index] = value.charCodeAt(index) & 0xff;
  }
  return bytes;
}

/** Whether focusing the terminal would not pull it away from someone typing elsewhere. */
function focusIsFree(pane: HTMLElement | null): boolean {
  const active = document.activeElement;
  if (!active || active === document.body || pane?.contains(active)) return true;
  return !active.closest(
    'input, textarea, select, [contenteditable]:not([contenteditable="false"])',
  );
}

function terminalLabel(terminal: AgentTerminalApi, sessions: AgentTerminalApi[]): string {
  return `Terminal ${sessions.findIndex((candidate) => candidate.id === terminal.id) + 1}`;
}

function NewIcon() {
  return (
    <svg aria-hidden="true" className="h-4 w-4" fill="none" viewBox="0 0 24 24">
      <path d="M12 5v14M5 12h14" stroke="currentColor" strokeLinecap="round" strokeWidth="1.8" />
    </svg>
  );
}

function SplitIcon() {
  return (
    <svg aria-hidden="true" className="h-4 w-4" fill="none" viewBox="0 0 24 24">
      <rect x="3.5" y="4" width="17" height="16" rx="2" stroke="currentColor" strokeWidth="1.6" />
      <path d="M12 4v16" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  );
}

function KillIcon() {
  return (
    <svg aria-hidden="true" className="h-4 w-4" fill="none" viewBox="0 0 24 24">
      <path
        d="M5 7h14M9 7V4h6v3m2 0-.7 13H7.7L7 7m3 4v5m4-5v5"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.6"
      />
    </svg>
  );
}

interface TerminalPaneProps {
  jobId: string;
  terminal: AgentTerminalApi;
  sessions: AgentTerminalApi[];
  /** Terminals whose shell has ended, so their tabs can say so. */
  exitedIds?: ReadonlySet<string>;
  paneIndex: number;
  ready: boolean;
  /** Take keyboard focus once open, unless someone is typing elsewhere. */
  autoFocus?: boolean;
  canCreate: boolean;
  canSplit: boolean;
  busy: boolean;
  maxInputBytes?: number;
  onSelect: (terminalId: string) => void;
  onNew: () => void;
  onSplit: () => void;
  onKill: () => void;
  onExit?: (terminalId: string, exitCode: number) => void;
}

export function TerminalPane({
  jobId,
  terminal,
  sessions,
  exitedIds,
  paneIndex,
  ready,
  autoFocus = false,
  canCreate,
  canSplit,
  busy,
  maxInputBytes,
  onSelect,
  onNew,
  onSplit,
  onKill,
  onExit,
}: TerminalPaneProps) {
  const paneRef = useRef<HTMLElement>(null);
  const mountRef = useRef<HTMLDivElement>(null);
  const terminalRef = useRef<Terminal | null>(null);
  const { resolved } = useTheme();
  // The creation effect runs once per terminal and reads everything else
  // through these. A new turn of the same chat changes `jobId` without
  // changing the terminal, and must not rebuild it: that would drop the
  // screen and replay the whole stream.
  const themeRef = useRef(resolved);
  themeRef.current = resolved;
  const jobIdRef = useRef(jobId);
  jobIdRef.current = jobId;
  const onExitRef = useRef(onExit);
  onExitRef.current = onExit;
  const autoFocusRef = useRef(autoFocus);
  autoFocusRef.current = autoFocus;
  const inputLimitRef = useRef(maxInputBytes);
  inputLimitRef.current = maxInputBytes;
  // Set once the shell has exited or its stream is gone for good.
  const endedRef = useRef(terminal.state !== 'running');
  const resizeRef = useRef<(() => void) | null>(null);
  const tabListRef = useRef<HTMLDivElement>(null);
  const tabRefs = useRef(new Map<string, HTMLButtonElement>());
  const inputEnabled = ready && terminal.state === 'running';
  const inputEnabledRef = useRef(inputEnabled);
  inputEnabledRef.current = inputEnabled;

  useEffect(() => {
    // Deliberately no focus here: readiness changes while someone may be
    // typing in the chat, and keystrokes meant for it must not reach a shell.
    if (terminalRef.current) {
      terminalRef.current.options.disableStdin = !inputEnabled || endedRef.current;
    }
    // A size refused while the workspace was locked is sent now.
    if (inputEnabled) resizeRef.current?.();
  }, [inputEnabled]);

  // Repaint an already-open terminal when the theme changes.
  useEffect(() => {
    if (terminalRef.current) terminalRef.current.options.theme = TERMINAL_THEMES[resolved];
  }, [resolved]);

  useEffect(() => {
    const tabList = tabListRef.current;
    if (!tabList) return;

    const keepSelectedTabVisible = () => {
      const tab = tabRefs.current.get(terminal.id);
      if (!tab) return;

      const listRect = tabList.getBoundingClientRect();
      const tabRect = tab.getBoundingClientRect();
      if (tabRect.left < listRect.left) {
        tabList.scrollLeft += tabRect.left - listRect.left;
      } else if (tabRect.right > listRect.right) {
        tabList.scrollLeft += tabRect.right - listRect.right;
      }
    };

    keepSelectedTabVisible();
    const observer =
      typeof ResizeObserver === 'undefined'
        ? undefined
        : new ResizeObserver(keepSelectedTabVisible);
    observer?.observe(tabList);
    return () => observer?.disconnect();
  }, [sessions.length, terminal.id]);

  useEffect(() => {
    // xterm is mounted in an unpadded box: FitAddon measures the box it was
    // opened in, and padding there was counted as room for more cells, which
    // hid the bottom line (the prompt) under the pane's edge.
    const mount = mountRef.current;
    if (!mount) return;

    const terminalId = terminal.id;
    const controller = new AbortController();
    let disposed = false;
    let observer: ResizeObserver | undefined;
    let resizeTimer: ReturnType<typeof setTimeout> | undefined;
    let inputTimer: ReturnType<typeof setTimeout> | undefined;
    const disposables: Array<{ dispose: () => void }> = [];
    // Text and binary input in arrival order; adjacent input of one kind is
    // sent as one request.
    let pendingInput: Array<{ binary: boolean; data: string }> = [];
    let flushingInput = false;
    // Output up to `replayThrough` predates this screen: the first attach
    // names it. While xterm is still parsing it, a reply xterm sends answers
    // a query in that history — for a program long past waiting, or gone.
    // A reconnect's backlog is not history: it arrived late, but for this
    // screen, so its queries are answered as over any slow link.
    let replayThrough = 0;
    let attached = false;
    let replayWrites = 0;
    let lastSize: string | null = null;

    const acceptsInput = () => inputEnabledRef.current && !endedRef.current;

    void loadTerminalRuntime()
      .then(async ([{ Terminal: Xterm }, { FitAddon }]) => {
        if (disposed) return;

        const xterm = new Xterm({
          allowProposedApi: false,
          convertEol: false,
          cursorBlink: true,
          cursorStyle: 'block',
          disableStdin: !acceptsInput(),
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
          fontSize: 13,
          lineHeight: 1.25,
          scrollback: 5000,
          theme: TERMINAL_THEMES[themeRef.current],
        });
        const fitAddon = new FitAddon();
        xterm.loadAddon(fitAddon);
        xterm.open(mount);
        terminalRef.current = xterm;
        if (autoFocusRef.current && !xterm.options.disableStdin && focusIsFree(paneRef.current)) {
          xterm.focus();
        }

        const end = () => {
          endedRef.current = true;
          xterm.options.disableStdin = true;
          pendingInput = [];
        };

        const resize = () => {
          if (disposed) return;
          fitAddon.fit();
          if (!acceptsInput()) return;
          const rows = clamp(xterm.rows, MIN_ROWS, MAX_ROWS);
          const cols = clamp(xterm.cols, MIN_COLS, MAX_COLS);
          const size = `${rows}x${cols}`;
          if (size === lastSize) return;
          lastSize = size;
          void resizeAgentTerminal(jobIdRef.current, terminalId, rows, cols).catch(() => {
            // Not applied: send it again on the next resize, or once the
            // workspace unlocks.
            if (lastSize === size) lastSize = null;
          });
        };
        resizeRef.current = resize;
        resize();

        if (typeof ResizeObserver !== 'undefined') {
          observer = new ResizeObserver(() => {
            clearTimeout(resizeTimer);
            resizeTimer = setTimeout(resize, 80);
          });
          observer.observe(mount);
        }

        const flushInput = async () => {
          if (disposed || flushingInput) return;
          if (!acceptsInput()) {
            pendingInput = [];
            return;
          }
          flushingInput = true;
          try {
            while (!disposed && acceptsInput() && pendingInput.length) {
              const segment = pendingInput.shift()!;
              const data = segment.binary ? binaryBytes(segment.data) : segment.data;
              if (inputLimitRef.current === undefined) {
                await writeAgentTerminalInput(jobIdRef.current, terminalId, data);
              } else {
                await writeAgentTerminalInput(
                  jobIdRef.current,
                  terminalId,
                  data,
                  inputLimitRef.current,
                );
              }
            }
          } catch (cause: unknown) {
            pendingInput = [];
            if (!disposed) {
              const message =
                cause instanceof Error ? cause.message : 'Could not send terminal input';
              xterm.writeln(`\r\n\x1b[31m${message}\x1b[0m`);
            }
          } finally {
            flushingInput = false;
            if (!disposed && pendingInput.length && !inputTimer) {
              inputTimer = setTimeout(() => {
                inputTimer = undefined;
                void flushInput();
              }, 12);
            }
          }
        };

        const queueInput = (data: string, binary: boolean) => {
          if (!acceptsInput()) return;
          // Only xterm's replies to replayed queries are dropped; anything
          // typed meanwhile goes to the shell.
          if (replayWrites > 0 && !binary && TERMINAL_REPLY.test(data)) return;
          const last = pendingInput[pendingInput.length - 1];
          if (last && last.binary === binary) last.data += data;
          else pendingInput.push({ binary, data });
          if (!inputTimer && !flushingInput) {
            inputTimer = setTimeout(() => {
              inputTimer = undefined;
              void flushInput();
            }, 12);
          }
        };

        disposables.push(xterm.onData((data) => queueInput(data, false)));
        disposables.push(xterm.onBinary((data) => queueInput(data, true)));

        // Written in-band, unlike `reset()`, so it lands after output that
        // xterm has queued but not yet drawn.
        const write = (data: Uint8Array | string, seq: number) => {
          if (seq <= replayThrough) {
            replayWrites += 1;
            xterm.write(data, () => {
              replayWrites -= 1;
            });
          } else {
            xterm.write(data);
          }
        };

        await streamAgentTerminal(jobIdRef.current, terminalId, {
          after: 0,
          signal: controller.signal,
          onAttached: (event) => {
            if (attached) return;
            attached = true;
            replayThrough = event.seq;
          },
          onOutput: (event) => {
            if (!disposed) write(decodeBase64(event.data), event.seq);
          },
          onReset: (event) => {
            if (disposed) return;
            write('\x1bc\x1b[90m[Earlier output truncated]\x1b[0m\r\n', event.seq);
          },
          onExit: (event) => {
            if (disposed) return;
            end();
            write(
              `\r\n\x1b[90m[process exited with code ${event.exit_code}]\x1b[0m\r\n`,
              event.seq,
            );
            onExitRef.current?.(terminalId, event.exit_code);
          },
        }).catch((cause: unknown) => {
          if (disposed || controller.signal.aborted) return;
          // The stream will not come back (the terminal is gone), so neither
          // will any reply to input.
          end();
          const message = cause instanceof Error ? cause.message : 'Terminal stream disconnected';
          xterm.writeln(`\r\n\x1b[31m${message}\x1b[0m`);
        });
      })
      .catch((cause: unknown) => {
        if (!disposed) {
          mount.textContent = cause instanceof Error ? cause.message : 'Could not load terminal';
        }
      });

    return () => {
      disposed = true;
      controller.abort();
      observer?.disconnect();
      clearTimeout(resizeTimer);
      clearTimeout(inputTimer);
      pendingInput = [];
      resizeRef.current = null;
      for (const disposable of disposables) disposable.dispose();
      terminalRef.current?.dispose();
      terminalRef.current = null;
    };
  }, [terminal.id]);

  const selectedLabel = terminalLabel(terminal, sessions);

  return (
    <section
      ref={paneRef}
      aria-label={`${selectedLabel} pane`}
      className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden rounded-xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 shadow-sm"
    >
      <header className="flex min-h-11 shrink-0 items-center gap-1 border-b border-gray-200 dark:border-white/10 px-2.5">
        <span aria-hidden="true" className="ml-0.5 text-gray-500 dark:text-gray-400">
          <svg className="h-4 w-4" fill="none" viewBox="0 0 24 24">
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
        <div
          ref={tabListRef}
          role="tablist"
          aria-label={`Terminals in pane ${paneIndex + 1}`}
          className="flex min-w-0 flex-1 items-center gap-0.5 overflow-x-auto py-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden"
        >
          {sessions.map((candidate) => {
            const exited = exitedIds?.has(candidate.id) || candidate.state === 'exited';
            const label = `${terminalLabel(candidate, sessions)} · ${
              exited ? 'exited' : candidate.shell
            }`;
            const selected = candidate.id === terminal.id;
            return (
              <button
                key={candidate.id}
                ref={(node) => {
                  if (node) tabRefs.current.set(candidate.id, node);
                  else tabRefs.current.delete(candidate.id);
                }}
                type="button"
                role="tab"
                aria-selected={selected}
                title={label}
                disabled={busy}
                onClick={() => onSelect(candidate.id)}
                className={`shrink-0 rounded-md px-2 py-1 text-[13px] font-medium outline-none transition-colors focus-visible:ring-2 focus-visible:ring-blue-200 disabled:cursor-not-allowed disabled:opacity-50 ${
                  selected
                    ? 'bg-gray-100 dark:bg-white/5 text-gray-900 dark:text-gray-50'
                    : 'text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 hover:text-gray-800 dark:hover:text-gray-200'
                }`}
              >
                {label}
              </button>
            );
          })}
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-0.5">
          <button
            type="button"
            aria-label="New terminal"
            title={canCreate ? 'New terminal' : 'Maximum of four terminal sessions'}
            onClick={onNew}
            disabled={!canCreate || busy}
            className="rounded-md p-1.5 text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-white/10 hover:text-gray-900 dark:hover:text-gray-100 disabled:cursor-not-allowed disabled:opacity-35"
          >
            <NewIcon />
          </button>
          <button
            type="button"
            aria-label="Split terminal"
            title={canSplit ? 'Split terminal' : 'Two terminal panes are already visible'}
            onClick={onSplit}
            disabled={!canSplit || busy}
            className="rounded-md p-1.5 text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-white/10 hover:text-gray-900 dark:hover:text-gray-100 disabled:cursor-not-allowed disabled:opacity-35"
          >
            <SplitIcon />
          </button>
          <button
            type="button"
            aria-label={`Kill ${selectedLabel}`}
            title={`Kill ${selectedLabel}`}
            onClick={onKill}
            disabled={busy}
            className="rounded-md p-1.5 text-gray-500 dark:text-gray-400 hover:bg-red-50 dark:hover:bg-red-500/10 hover:text-red-600 dark:hover:text-red-400 disabled:cursor-not-allowed disabled:opacity-35"
          >
            <KillIcon />
          </button>
        </div>
      </header>
      <div
        role="application"
        aria-label={`${selectedLabel} terminal`}
        className="agent-xterm flex min-h-64 flex-1 flex-col bg-white dark:bg-gray-900 p-3"
      >
        <div ref={mountRef} className="relative min-h-0 min-w-0 flex-1" />
      </div>
    </section>
  );
}

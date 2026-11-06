'use client';

import type { HTMLAttributes, MouseEvent } from 'react';
import { hasActivityOverview, type ActivityOverview } from './activityOverview';
import type { MaximizeControl } from './FloatingActivityWindow';
import { DelegationReplyDetails, ToolActivityDetails } from './ToolActivityDetails';

export function ActivityIcon() {
  return (
    <svg
      aria-hidden="true"
      className="h-4 w-4"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={1.8}
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="m4 6 1.5 1.5L8 5M11 6h9m-16 6 1.5 1.5L8 11m3 1h9m-16 6 1.5 1.5L8 17m3 1h9"
      />
    </svg>
  );
}

/**
 * Whether a click on a subagent card belongs to something inside it rather
 * than the card: its own controls, its expandable instructions, or the end of
 * selecting its text.
 */
function ownClick(event: MouseEvent<HTMLElement>): boolean {
  const target = event.target as Element;
  if (target.closest('a, button, details, input, select, textarea')) return true;
  const selection = window.getSelection();
  return (
    !!selection && !selection.isCollapsed && event.currentTarget.contains(selection.anchorNode)
  );
}

/** A floating overview of the selected turn's recorded tasks and delegated agents. */
export function ActivityWindow({
  overview,
  turnNo,
  attemptNo,
  onClose,
  openSubagentKey,
  onOpenSubagent,
  moveHandleProps,
  maximize,
}: {
  overview: ActivityOverview;
  turnNo: number;
  attemptNo: number;
  onClose: () => void;
  /** The subagent whose conversation the page is showing, if any. */
  openSubagentKey?: string | null;
  onOpenSubagent: (key: string) => void;
  moveHandleProps?: HTMLAttributes<HTMLDivElement>;
  maximize?: MaximizeControl;
}) {
  if (!hasActivityOverview(overview)) return null;

  const taskActivity = overview.taskActivity?.tasks.length ? overview.taskActivity : undefined;

  return (
    <>
      <header className="flex shrink-0 items-start gap-3 rounded-t-xl border-b border-gray-200 px-4 py-3 dark:border-white/10">
        <div className="min-w-0 flex-1" {...moveHandleProps}>
          <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
            Tasks &amp; subagents
          </h2>
          <p className="mt-0.5 text-[11px] text-gray-500 dark:text-gray-400">
            Turn {turnNo}
            {attemptNo > 0 ? ` · Attempt ${attemptNo}` : ''}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {maximize ? (
            <button
              type="button"
              onClick={maximize.toggle}
              aria-label={
                maximize.maximized ? 'Restore tasks and subagents' : 'Maximize tasks and subagents'
              }
              title={maximize.maximized ? 'Restore · Esc' : 'Maximize'}
              className="rounded-md p-1 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-crimson dark:text-gray-500 dark:hover:bg-white/5 dark:hover:text-gray-200"
            >
              <svg
                aria-hidden="true"
                className="h-4 w-4"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1.8}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d={
                    maximize.maximized
                      ? 'M9 4v5H4m11-5v5h5M9 20v-5H4m11 5v-5h5'
                      : 'M4 9V4h5m6 0h5v5M4 15v5h5m6 0h5v-5'
                  }
                />
              </svg>
            </button>
          ) : null}
          <button
            type="button"
            onClick={onClose}
            aria-label="Close tasks and subagents"
            className="rounded-md p-1 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-crimson dark:text-gray-500 dark:hover:bg-white/5 dark:hover:text-gray-200"
          >
            <svg
              aria-hidden="true"
              className="h-4 w-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={1.8}
            >
              <path strokeLinecap="round" d="m6 6 12 12M18 6 6 18" />
            </svg>
          </button>
        </div>
      </header>
      <div
        role="region"
        aria-label="Tasks and subagents details"
        tabIndex={0}
        className="min-h-0 flex-1 space-y-6 overflow-x-hidden overflow-y-auto rounded-b-xl p-4 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-crimson/40"
      >
        {taskActivity ? (
          <section aria-label="Tasks overview" className="space-y-3">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Tasks
            </h3>
            <ToolActivityDetails activity={taskActivity} />
          </section>
        ) : null}
        {overview.subagents.length > 0 ? (
          <section aria-label="Subagents overview" className="space-y-3">
            <div className="flex items-center justify-between gap-2">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
                Subagents
              </h3>
              <span className="text-[11px] tabular-nums text-gray-400 dark:text-gray-500">
                {overview.subagents.length}
              </span>
            </div>
            <ul className="space-y-3">
              {overview.subagents.map((agent) => {
                const open = agent.key === openSubagentKey;
                const title = agent.activity.description ?? agent.activity.title;
                return (
                  <li
                    key={agent.key}
                    aria-current={open || undefined}
                    onClick={(event) => {
                      if (!open && !ownClick(event)) onOpenSubagent(agent.key);
                    }}
                    className={`relative min-w-0 space-y-2.5 rounded-lg border p-3 pr-10 ${
                      open
                        ? 'border-crimson/50 dark:border-crimson-light/50'
                        : 'cursor-pointer border-gray-200 transition-colors hover:border-gray-300 hover:bg-gray-50 dark:border-white/10 dark:hover:border-white/20 dark:hover:bg-white/5'
                    }`}
                  >
                    {open ? (
                      <span className="absolute right-3 top-3 text-[11px] font-medium text-crimson dark:text-crimson-light">
                        Current
                      </span>
                    ) : (
                      <button
                        type="button"
                        onClick={() => onOpenSubagent(agent.key)}
                        aria-label={`Switch subagent: ${title}`}
                        title="Switch to this subagent"
                        className="absolute right-2 top-2 rounded-md p-1 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-crimson dark:text-gray-500 dark:hover:bg-white/10 dark:hover:text-gray-200"
                      >
                        <svg
                          aria-hidden="true"
                          className="h-4 w-4"
                          fill="none"
                          viewBox="0 0 24 24"
                          stroke="currentColor"
                          strokeWidth={1.8}
                        >
                          <path strokeLinecap="round" strokeLinejoin="round" d="m9 6 6 6-6 6" />
                        </svg>
                      </button>
                    )}
                    <ToolActivityDetails activity={agent.activity} />
                    {agent.reply ? (
                      <DelegationReplyDetails reply={agent.reply} />
                    ) : agent.activity.states.length === 0 ? (
                      <p className="text-[11px] text-gray-400 dark:text-gray-500">
                        Agent state not recorded
                      </p>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          </section>
        ) : null}
      </div>
    </>
  );
}

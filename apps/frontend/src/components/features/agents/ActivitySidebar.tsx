'use client';

import type { buildActivityOverview } from './activityOverview';
import type { AgentEvent } from './types';
import { ToolActivityDetails } from './ToolActivityDetails';

type Overview = ReturnType<typeof buildActivityOverview>;
type ToolEvent = Extract<AgentEvent, { kind: 'tool_use' }>;

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

/** The selected turn's latest recorded task list and delegated agents. */
export function ActivitySidebar({
  overview,
  turnNo,
  attemptNo,
  onClose,
  stepKeyForEvent,
  onSelectStep,
}: {
  overview: Overview;
  turnNo: number;
  attemptNo: number;
  onClose: () => void;
  stepKeyForEvent: (event: ToolEvent) => string | undefined;
  onSelectStep: (stepKey: string) => void;
}) {
  return (
    <>
      <header className="flex shrink-0 items-start gap-3 border-b border-gray-200 px-4 py-3 dark:border-white/10">
        <div className="min-w-0 flex-1">
          <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
            Tasks &amp; subagents
          </h2>
          <p className="mt-0.5 text-[11px] text-gray-500 dark:text-gray-400">
            Turn {turnNo}
            {attemptNo > 0 ? ` · Attempt ${attemptNo}` : ''}
          </p>
        </div>
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
      </header>
      <div className="min-h-0 flex-1 space-y-6 overflow-y-auto p-4">
        <section aria-label="Tasks overview" className="space-y-3">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
            Tasks
          </h3>
          {overview.taskActivity ? (
            <ToolActivityDetails activity={overview.taskActivity} />
          ) : (
            <p className="text-xs leading-relaxed text-gray-400 dark:text-gray-500">
              No tasks recorded for this turn yet.
            </p>
          )}
        </section>
        <section aria-label="Subagents overview" className="space-y-3">
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Subagents
            </h3>
            {overview.subagents.length > 0 ? (
              <span className="text-[11px] tabular-nums text-gray-400 dark:text-gray-500">
                {overview.subagents.length}
              </span>
            ) : null}
          </div>
          {overview.subagents.length > 0 ? (
            <ul className="space-y-3">
              {overview.subagents.map((agent) => {
                const stepKey = stepKeyForEvent(agent.sourceEvent);
                return (
                  <li
                    key={agent.key}
                    className="min-w-0 space-y-2.5 rounded-lg border border-gray-200 p-3 dark:border-white/10"
                  >
                    <ToolActivityDetails activity={agent.activity} />
                    {agent.activity.states.length === 0 ? (
                      <p className="text-[11px] text-gray-400 dark:text-gray-500">
                        Agent state not recorded
                      </p>
                    ) : null}
                    {stepKey ? (
                      <button
                        type="button"
                        onClick={() => onSelectStep(stepKey)}
                        className="rounded text-[11px] font-medium text-crimson hover:underline focus-visible:outline-crimson dark:text-crimson-light"
                      >
                        View subagent execution
                      </button>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="text-xs leading-relaxed text-gray-400 dark:text-gray-500">
              No subagents recorded for this turn yet.
            </p>
          )}
        </section>
      </div>
    </>
  );
}

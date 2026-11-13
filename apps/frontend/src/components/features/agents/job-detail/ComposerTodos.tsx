'use client';

import { useT } from '@/components/providers';
import type { ActivityOverview } from '../conversation/activityOverview';
import { ActivityIcon } from '../conversation/ActivityWindow';

export interface ComposerTodosProps {
  /** The shown turn's recorded tasks and subagents; only its task list is summarized. */
  overview: ActivityOverview;
  /** Whether the Tasks and subagents window is showing. */
  open: boolean;
  toggle: () => void;
}

const COUNTED = [
  ['in_progress', 'composer.todosInProgress'],
  ['pending', 'composer.todosPending'],
  ['completed', 'composer.todosCompleted'],
] as const;

/**
 * A one-line summary of the task list the agent recorded, above the composer.
 * It reads the same recorded tasks as the Tasks and subagents window and
 * opens that window; nothing is drawn when the turn recorded no tasks.
 */
export function ComposerTodos({ overview, open, toggle }: ComposerTodosProps) {
  const t = useT();
  const tasks = overview.taskActivity?.tasks ?? [];
  if (!tasks.length) return null;
  const counts = COUNTED.flatMap(([status, key]) => {
    const count = tasks.filter((task) => task.status === status).length;
    return count ? [t(key, { count: String(count) })] : [];
  });
  const summary = (
    counts.length ? counts : [t('composer.todosCount', { count: String(tasks.length) })]
  ).join(' · ');
  return (
    <button
      type="button"
      onClick={(event) => {
        const strip = event.currentTarget;
        toggle();
        // Closing returns focus to the header's toggle; one closed from here stays here.
        if (open) strip.focus();
      }}
      aria-expanded={open}
      aria-controls="job-activity-window"
      aria-label={`${t('composer.todos')}: ${summary}`}
      className="ui-focus-ring mx-3 mb-1.5 flex w-[calc(100%-1.5rem)] min-w-0 items-center gap-2.5 rounded-xl border border-border/70 bg-surface-muted px-3 py-1.5 text-left text-xs text-muted transition-colors hover:bg-surface-hover hover:text-foreground [@media(pointer:coarse)]:min-h-11"
    >
      <ActivityIcon />
      <span className="shrink-0 font-medium text-foreground">{t('composer.todos')}</span>
      <span className="min-w-0 flex-1 truncate">{summary}</span>
      <svg
        aria-hidden="true"
        viewBox="0 0 20 20"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.6}
        className={`h-3.5 w-3.5 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="m6 12 4-4 4 4" />
      </svg>
    </button>
  );
}

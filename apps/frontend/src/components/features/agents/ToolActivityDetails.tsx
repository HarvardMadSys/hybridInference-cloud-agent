'use client';

import type { ReactNode } from 'react';
import {
  recordedAgentStates,
  recordedAgentTargets,
  recordedTasks,
  type ToolActivity,
} from './toolActivity';

const STATUS_LABELS: Record<string, string> = {
  pending: 'Pending',
  in_progress: 'In progress',
  completed: 'Completed',
  blocked: 'Blocked',
  failed: 'Failed',
  cancelled: 'Cancelled',
  deleted: 'Deleted',
  running: 'Running',
  pending_init: 'Starting',
  pendingInit: 'Starting',
  errored: 'Errored',
  shutdown: 'Closed',
  not_found: 'Not found',
  notFound: 'Not found',
};

function Status({ value }: { value?: string }) {
  const tone =
    value === 'completed'
      ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400'
      : value === 'failed' || value === 'errored'
        ? 'bg-rose-50 text-rose-700 dark:bg-rose-500/10 dark:text-rose-400'
        : value === 'in_progress' || value === 'running'
          ? 'bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-400'
          : 'bg-gray-100 text-gray-500 dark:bg-white/5 dark:text-gray-400';
  return (
    <span className={`shrink-0 rounded px-1.5 py-0.5 text-[10px] ${tone}`}>
      {value
        ? Object.hasOwn(STATUS_LABELS, value)
          ? STATUS_LABELS[value]
          : value
        : 'Status not recorded'}
    </span>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-wrap gap-x-2 gap-y-0.5 text-xs">
      <dt className="text-gray-400 dark:text-gray-500">{label}</dt>
      <dd className="min-w-0 break-words text-gray-600 dark:text-gray-300">{children}</dd>
    </div>
  );
}

/** A reading of recorded instructions and states, not a derived execution result. */
export function ToolActivityDetails({
  activity,
  output,
  isError,
}: {
  activity: ToolActivity;
  output?: string;
  isError?: boolean;
}) {
  if (activity.kind === 'tasks') {
    const resultTasks = !isError && output ? recordedTasks(output) : [];
    const tasks = activity.tasks.length ? activity.tasks : resultTasks;
    const completed = tasks.filter((task) => task.status === 'completed').length;
    return (
      <section aria-label={activity.title} className="space-y-2.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h3 className="text-xs font-medium text-gray-700 dark:text-gray-200">{activity.title}</h3>
          {tasks.length ? (
            <span className="text-[11px] tabular-nums text-gray-400 dark:text-gray-500">
              {completed} of {tasks.length} marked completed
            </span>
          ) : null}
        </div>
        {activity.explanation ? (
          <p className="break-words text-xs text-gray-500 dark:text-gray-400">
            {activity.explanation}
          </p>
        ) : null}
        {tasks.length ? (
          <ul className="space-y-2">
            {tasks.map((task, index) => (
              <li key={`${task.id ?? index}:${index}`} className="flex items-start gap-2">
                <span
                  aria-hidden="true"
                  className={`mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px] ${
                    task.status === 'completed'
                      ? 'border-emerald-300 text-emerald-600 dark:border-emerald-500/40 dark:text-emerald-400'
                      : 'border-gray-300 text-gray-400 dark:border-gray-600 dark:text-gray-500'
                  }`}
                >
                  {task.status === 'completed' ? '✓' : task.status === 'in_progress' ? '•' : ''}
                </span>
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                    {task.id ? (
                      <span className="min-w-0 break-all font-mono text-[10px] text-gray-400 dark:text-gray-500">
                        #{task.id}
                      </span>
                    ) : null}
                    <span className="min-w-0 break-words text-xs text-gray-700 dark:text-gray-200">
                      {task.subject}
                    </span>
                    <Status value={task.status} />
                  </div>
                  {task.description ? (
                    <details className="text-xs text-gray-500 dark:text-gray-400">
                      <summary className="cursor-pointer">Description</summary>
                      <p className="mt-1 whitespace-pre-wrap break-words">{task.description}</p>
                    </details>
                  ) : null}
                  {task.owner || task.blockedBy.length || task.blocks.length ? (
                    <dl className="space-y-0.5">
                      {task.owner ? <Field label="Owner">{task.owner}</Field> : null}
                      {task.blockedBy.length ? (
                        <Field label="Blocked by">
                          {task.blockedBy.map((id) => `#${id}`).join(', ')}
                        </Field>
                      ) : null}
                      {task.blocks.length ? (
                        <Field label="Blocks">{task.blocks.map((id) => `#${id}`).join(', ')}</Field>
                      ) : null}
                    </dl>
                  ) : null}
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-gray-400 dark:text-gray-500">
            Task details were not recorded in this call.
          </p>
        )}
      </section>
    );
  }

  const resultStates = !isError && output ? recordedAgentStates(output) : [];
  const states = resultStates.length ? resultStates : activity.states;
  const targets = [
    ...new Set([...activity.targets, ...(!isError && output ? recordedAgentTargets(output) : [])]),
  ];
  // One row of meta, not one per field: a delegation card carries at most a
  // handful of recorded details, and the transcript stacks many of them.
  const meta = [
    activity.agentType,
    activity.model,
    activity.background === undefined
      ? undefined
      : activity.background
        ? 'Background requested'
        : 'Foreground requested',
    targets.length
      ? `${targets.length === 1 ? 'Agent' : 'Agents'}: ${targets.join(', ')}`
      : undefined,
  ].filter((value): value is string => !!value);
  return (
    <section aria-label={activity.title} className="min-w-0 space-y-1.5">
      <h3 className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs font-medium text-gray-700 dark:text-gray-200">
        {activity.description ?? activity.title}
        {activity.description && activity.description !== activity.title ? (
          <>
            {' '}
            <span className="min-w-0 font-normal text-gray-400 dark:text-gray-500">
              {activity.title}
            </span>
          </>
        ) : null}
      </h3>
      {meta.length ? (
        <p className="flex min-w-0 flex-wrap gap-x-3 gap-y-0.5 text-xs text-gray-500 dark:text-gray-400">
          {meta.map((value) => (
            <span key={value} className="min-w-0 break-words">
              {value}
            </span>
          ))}
        </p>
      ) : null}
      {activity.prompt ? (
        <details className="w-fit rounded-md border border-gray-100 text-xs dark:border-white/5">
          <summary className="cursor-pointer px-2 py-1 text-gray-500 dark:text-gray-400">
            {activity.promptLabel}
          </summary>
          <p className="max-h-60 overflow-auto whitespace-pre-wrap break-words border-t border-gray-100 px-2 py-2 text-gray-600 dark:border-white/5 dark:text-gray-300">
            {activity.prompt}
          </p>
        </details>
      ) : null}
      {states.length ? (
        <ul className="space-y-1">
          {states.map((state) => (
            <li key={state.id} className="min-w-0">
              <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
                <span className="min-w-0 break-all font-mono text-[11px] text-gray-500 dark:text-gray-400">
                  {state.id}
                </span>
                <Status value={state.status} />
                {state.message ? (
                  <details className="text-xs text-gray-500 dark:text-gray-400">
                    <summary className="cursor-pointer">Message</summary>
                    <p className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words">
                      {state.message}
                    </p>
                  </details>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

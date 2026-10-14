'use client';

import { useId, useState, type ReactNode } from 'react';
import { Markdown } from '@/components/ui/Markdown';
import {
  recordedAgentStates,
  recordedAgentTargets,
  recordedTasks,
  type DelegationReply,
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

export function Status({ value }: { value?: string }) {
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
        <details className="w-fit max-w-full rounded-md border border-gray-100 text-xs dark:border-white/5">
          <summary className="cursor-pointer px-2 py-1 text-gray-500 dark:text-gray-400">
            {activity.promptLabel}
          </summary>
          <p className="max-h-60 overflow-auto whitespace-pre-wrap border-t border-gray-100 px-2 py-2 text-gray-600 [overflow-wrap:anywhere] dark:border-white/5 dark:text-gray-300">
            {activity.prompt}
          </p>
        </details>
      ) : null}
      {states.length ? (
        <ul className="space-y-1">
          {states.map((state) => (
            <li key={state.id} className="min-w-0 space-y-1.5">
              <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
                <span className="min-w-0 break-all font-mono text-[11px] text-gray-500 dark:text-gray-400">
                  {state.id}
                </span>
                <Status value={state.status} />
              </div>
              {state.message ? (
                // Codex reports a finished agent's answer, or its error, as
                // the message of the state it returns to the main agent.
                <DelegationReplyDetails
                  reply={{
                    state: state.status === 'errored' ? 'failed' : 'received',
                    text: state.message,
                  }}
                  showStatus={false}
                />
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

const LONG_REPLY_CHARACTERS = 600;
const LONG_REPLY_LINES = 8;

function ReplyNote({ children }: { children: ReactNode }) {
  return <p className="text-[11px] text-gray-400 dark:text-gray-500">{children}</p>;
}

/** What a delegating call returned to the main agent, read only from its recorded result. */
export function DelegationReplyDetails({
  reply,
  showStatus = true,
}: {
  reply: DelegationReply;
  /** Off where the agent's state is already shown beside the reply. */
  showStatus?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const bodyId = useId();

  if (reply.state === 'waiting') return <ReplyNote>Waiting for the agent&apos;s reply…</ReplyNote>;
  if (reply.state === 'not_recorded') {
    return <ReplyNote>The agent&apos;s reply was not recorded.</ReplyNote>;
  }
  if (reply.state === 'background') {
    return (
      <ReplyNote>
        Started in the background. This call returned only a launch notice, not the agent&apos;s
        reply.
      </ReplyNote>
    );
  }
  const status = !showStatus ? undefined : reply.state === 'received' ? reply.status : reply.state;
  const { text } = reply;
  if (!text && reply.state !== 'received') {
    return (
      <p className="flex flex-wrap items-center gap-2 text-[11px] text-gray-500 dark:text-gray-400">
        {status ? <Status value={status} /> : null}
        {reply.state === 'failed'
          ? 'The delegation call failed.'
          : 'The delegation call was interrupted.'}
      </p>
    );
  }
  const long =
    !!text && (text.length > LONG_REPLY_CHARACTERS || text.split('\n').length > LONG_REPLY_LINES);
  const collapsed = long && !expanded;
  return (
    <section aria-label="Subagent reply" className="min-w-0 space-y-1.5">
      <div className="flex flex-wrap items-center gap-2">
        <h4 className="text-[11px] font-medium text-gray-500 dark:text-gray-400">
          Returned to the main agent
        </h4>
        {status ? <Status value={status} /> : null}
      </div>
      {text ? (
        <div
          id={bodyId}
          className={`min-w-0 [overflow-wrap:anywhere] ${
            reply.state === 'failed'
              ? 'text-rose-700 dark:text-rose-300'
              : 'text-gray-700 dark:text-gray-200'
          } ${
            collapsed
              ? 'max-h-40 overflow-hidden [mask-image:linear-gradient(to_bottom,black_60%,transparent)]'
              : ''
          }`}
        >
          <Markdown text={text} />
        </div>
      ) : (
        <ReplyNote>No text was returned.</ReplyNote>
      )}
      {reply.truncated ? (
        <p role="note" className="text-[11px] text-amber-700 dark:text-amber-300">
          The stored reply was cut short; this is not the whole answer.
        </p>
      ) : null}
      {long ? (
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={bodyId}
          onClick={() => setExpanded((value) => !value)}
          className="rounded text-[11px] font-medium text-gray-500 hover:text-gray-800 focus-visible:outline-crimson dark:text-gray-400 dark:hover:text-gray-100"
        >
          {expanded ? 'Show less' : 'Show full reply'}
        </button>
      ) : null}
    </section>
  );
}

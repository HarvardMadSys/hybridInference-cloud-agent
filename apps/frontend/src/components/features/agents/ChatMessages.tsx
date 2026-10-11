'use client';

import { Fragment, useId, useState, type ReactNode } from 'react';
import toast from 'react-hot-toast';

import { Markdown } from '@/components/ui/Markdown';
import { useT } from '@/components/providers';
import { GoalIcon } from './ComposerPlusMenu';
import type { TranslationKey } from '@/lib/i18n/dictionaries/en';
import { lifecyclePhaseLabel } from './adapt';
import {
  eventBlockKey,
  messageBlockKey,
  type ConversationTurnDescription,
} from './conversationTurnNavigation';
import type { AgentEvent, AgentThreadMessage, GoalTurn } from './types';
import { ToolActivityDetails } from './ToolActivityDetails';
import { parseToolActivity } from './toolActivity';

function Chevron({ className = '' }: { className?: string }) {
  return (
    <svg
      aria-hidden="true"
      className={`h-4 w-4 ${className}`}
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="m9 18 6-6-6-6" />
    </svg>
  );
}

function ToolIcon({ tool }: { tool: string }) {
  const normalized = tool.toLowerCase();
  let path = 'M12 4v16m8-8H4';
  if (normalized.includes('read') || normalized.includes('search')) {
    path =
      'M12 6.25c-2.5-2-6.5-2-9 0v12c2.5-2 6.5-2 9 0 2.5-2 6.5-2 9 0v-12c-2.5-2-6.5-2-9 0Zm0 0v12';
  } else if (normalized.includes('bash') || normalized.includes('command')) {
    path =
      'm7 8 4 4-4 4m6 0h4M4 4h16a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z';
  } else if (normalized.includes('edit') || normalized.includes('write')) {
    path =
      'M16.86 4.49a1.5 1.5 0 0 1 2.12 0l.53.53a1.5 1.5 0 0 1 0 2.12L8.53 18.12 4 19.5l1.38-4.53L16.86 4.49Z';
  }
  return (
    <svg
      aria-hidden="true"
      className="h-4 w-4 shrink-0 text-gray-400 dark:text-gray-500"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d={path} />
    </svg>
  );
}

// What a first run's milestones mean, said once: on the first turn of the
// owner's first conversation, and never again. The rest need no gloss, and a
// later turn's publishing updates the draft PR rather than opening one.
const FIRST_RUN_CAPTIONS: Partial<Record<string, TranslationKey>> = {
  started: 'firstRun.started',
  workspace_preparing: 'firstRun.preparing',
  checked_out: 'firstRun.checkedOut',
  publishing: 'firstRun.publishing',
  shared_snapshot_publishing: 'firstRun.publishing',
};

function LifecycleRow({
  text,
  blockKey,
  firstRun = false,
}: {
  text: string;
  blockKey?: string;
  firstRun?: boolean;
}) {
  const t = useT();
  const { text: label, milestone } = lifecyclePhaseLabel(text);

  // A phase this build does not recognise is a diagnostic, not an achievement:
  // no tick, muted, and clearly a passthrough of what the runtime said.
  if (!milestone) {
    return (
      <div
        data-block-key={blockKey}
        className="flex items-center gap-2.5 py-1 text-xs text-gray-400 dark:text-gray-500"
      >
        <span className="h-1 w-1 rounded-full bg-gray-300 dark:bg-gray-600" />
        <span className="font-mono">{label}</span>
      </div>
    );
  }

  const caption = firstRun ? FIRST_RUN_CAPTIONS[text.trim().toLowerCase()] : undefined;
  const row = (
    <div
      data-block-key={caption ? undefined : blockKey}
      className={`flex items-center gap-2.5 ${caption ? 'pt-2' : 'py-2'} text-[13px] text-gray-500 dark:text-gray-400`}
    >
      <span className="flex h-4 w-4 items-center justify-center rounded-full bg-gray-100 dark:bg-white/5 text-gray-500 dark:text-gray-400">
        <svg
          aria-hidden="true"
          className="h-3 w-3"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2.5}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="m6 12 4 4 8-9" />
        </svg>
      </span>
      <span className="capitalize">{label}</span>
    </div>
  );
  if (!caption) return row;
  return (
    <div data-block-key={blockKey} className="pb-2">
      {row}
      <p className="ml-[26px] mt-0.5 text-xs leading-relaxed text-gray-400 dark:text-gray-500">
        {t(caption)}
      </p>
    </div>
  );
}

type ToolActivityItem = {
  event: Extract<AgentEvent, { kind: 'tool_use' }>;
  blockKey?: string;
};

function toolResultLabel(event: ToolActivityItem['event']): string {
  return event.resultState === 'unknown'
    ? 'Pairing unknown'
    : event.resultState === 'cancelled'
      ? 'Call interrupted'
      : event.outputIsError || event.resultState === 'failed'
        ? 'Call failed'
        : event.resultState === 'received' || event.output !== undefined
          ? 'Result received'
          : event.resultState === 'waiting'
            ? 'Waiting for result'
            : 'Result not recorded';
}

function RichToolActivity({
  event,
  blockKey,
}: {
  event: Extract<AgentEvent, { kind: 'tool_use' }>;
  blockKey?: string;
}) {
  const activity = parseToolActivity(event.tool, event.input);
  const resultLabel = toolResultLabel(event);
  if (activity) {
    return (
      <section
        data-block-key={blockKey}
        aria-label={activity.kind === 'tasks' ? 'Task activity' : 'Subagent activity'}
        className={`my-2 min-w-0 rounded-lg border border-gray-200 bg-white dark:border-white/10 dark:bg-gray-900 ${event.parentToolUseId ? 'ml-4 border-l-2' : ''}`}
      >
        <div className="flex flex-wrap items-center gap-2 border-b border-gray-100 px-3 py-2 dark:border-white/5">
          <ToolIcon tool={event.tool} />
          <span className="text-[13px] font-medium text-gray-700 dark:text-gray-300">
            {activity.kind === 'tasks' ? 'Tasks' : 'Subagent'}
          </span>
          <span className="break-all font-mono text-[11px] text-gray-500 dark:text-gray-400">
            {event.tool}
          </span>
          <span
            className={`ml-auto text-[11px] ${event.outputIsError ? 'text-red-700 dark:text-red-300' : 'text-gray-500 dark:text-gray-400'}`}
          >
            {resultLabel}
          </span>
        </div>
        <div className="space-y-2 px-3 py-3">
          <ToolActivityDetails
            activity={activity}
            output={event.output?.join('\n')}
            isError={event.outputIsError}
          />
          <details className="group">
            <summary className="cursor-pointer text-xs text-gray-500 dark:text-gray-400">
              Call details
            </summary>
            <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-gray-950 p-3 font-mono text-xs text-gray-200">
              {event.input === undefined ? event.detail : JSON.stringify(event.input, null, 2)}
            </pre>
            {event.output !== undefined ? (
              <pre
                className={`mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words rounded-md p-3 font-mono text-xs ${event.outputIsError ? 'bg-red-950 text-red-100' : 'bg-gray-950 text-gray-200'}`}
              >
                {event.output.join('\n')}
              </pre>
            ) : null}
          </details>
        </div>
      </section>
    );
  }
  return <ToolActivity tools={[{ event, blockKey }]} />;
}

function ToolActivity({ tools }: { tools: ToolActivityItem[] }) {
  const [expanded, setExpanded] = useState(false);
  const detailsId = useId();
  const label = tools.length === 1 ? 'tool call' : 'tool calls';

  return (
    <div className={`my-1 min-w-0 ${tools[0]?.event.parentToolUseId ? 'ml-4' : ''}`}>
      <div className="flex min-w-0 items-center gap-2 py-1 text-xs text-gray-500 dark:text-gray-400">
        <ul
          role="list"
          aria-label="Tool calls"
          tabIndex={0}
          className="flex min-w-0 flex-1 flex-nowrap items-center gap-1.5 overflow-x-auto rounded-md focus-visible:outline-crimson"
        >
          {tools.map(({ event, blockKey }, index) => (
            <li
              key={blockKey ?? index}
              data-block-key={blockKey}
              title={`${event.tool}: ${event.detail}`}
              className="inline-flex shrink-0 items-center gap-1.5 rounded-md bg-gray-100 px-2 py-1 dark:bg-white/5"
            >
              <ToolIcon tool={event.tool} />
              {event.parentToolUseId ? (
                <span
                  title={`Delegated by ${event.parentToolUseId}`}
                  className="text-[10px] text-violet-700 dark:text-violet-300"
                >
                  Subagent tool
                </span>
              ) : null}
              <span className="max-w-40 truncate font-medium text-gray-700 dark:text-gray-300">
                {event.tool}
              </span>
              {event.diffStat ? (
                <span className="max-w-20 truncate font-mono" title={event.diffStat}>
                  {event.diffStat}
                </span>
              ) : null}
              {event.resultState === 'unknown' || event.resultState === 'cancelled' ? (
                <span className="text-gray-500 dark:text-gray-400">{toolResultLabel(event)}</span>
              ) : event.outputIsError || event.resultState === 'failed' ? (
                <span className="text-rose-600 dark:text-rose-400">Failed</span>
              ) : null}
            </li>
          ))}
        </ul>
        <button
          type="button"
          aria-expanded={expanded}
          aria-controls={detailsId}
          onClick={() => setExpanded((value) => !value)}
          className="inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-1 text-[11px] transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-crimson dark:hover:bg-white/10 dark:hover:text-gray-100"
        >
          <Chevron className={`transition-transform ${expanded ? 'rotate-90' : ''}`} />
          {expanded ? `Hide ${label}` : `Show ${label}`}
        </button>
      </div>
      <div
        id={detailsId}
        hidden={!expanded}
        className="mt-1 space-y-3 rounded-lg border border-gray-200 bg-white px-3 py-2.5 dark:border-white/10 dark:bg-gray-900"
      >
        {tools.map(({ event, blockKey }, index) => {
          const call =
            event.input === undefined
              ? event.detail
              : typeof event.input === 'string'
                ? event.input
                : JSON.stringify(event.input, null, 2);
          return (
            <section
              key={blockKey ?? index}
              className="space-y-2 border-b border-gray-100 pb-3 last:border-b-0 last:pb-0 dark:border-white/5"
            >
              <p className="text-xs font-medium text-gray-700 dark:text-gray-300">
                {event.tool} · Call {index + 1}
              </p>
              <p className="text-xs text-gray-500 dark:text-gray-400">Tool call</p>
              <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-gray-50 px-3 py-2.5 font-mono text-xs leading-relaxed text-gray-700 dark:bg-white/5 dark:text-gray-300">
                {call}
              </pre>
              <p className="text-xs text-gray-500 dark:text-gray-400">Result</p>
              {event.output !== undefined ? (
                <pre
                  className={`max-h-72 overflow-auto whitespace-pre-wrap rounded-md px-3 py-2.5 font-mono text-xs leading-relaxed ${
                    event.outputIsError ? 'bg-red-950 text-red-100' : 'bg-gray-950 text-gray-200'
                  }`}
                >
                  {event.output.join('\n') || 'Command completed with no output.'}
                </pre>
              ) : (
                <p className="text-xs text-gray-400 dark:text-gray-500">
                  {event.resultState && event.resultState !== 'waiting'
                    ? toolResultLabel(event)
                    : 'Waiting for the tool result…'}
                </p>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}

/** Consecutive calls share one line; other events keep their original position. */
export function EventList({
  events,
  jobId,
  renderEvent,
  showActivityDetails = true,
}: {
  events: AgentEvent[];
  jobId: string;
  renderEvent: (event: AgentEvent, index: number, blockKey: string) => ReactNode;
  /** Keep task/delegation calls compact when their overview is shown elsewhere. */
  showActivityDetails?: boolean;
}) {
  const rows: ReactNode[] = [];
  for (let index = 0; index < events.length; index += 1) {
    const event = events[index];
    const blockKey = eventBlockKey(jobId, event, index);
    if (
      event.kind !== 'tool_use' ||
      (showActivityDetails && parseToolActivity(event.tool, event.input))
    ) {
      rows.push(<Fragment key={blockKey}>{renderEvent(event, index, blockKey)}</Fragment>);
      continue;
    }
    const tools: ToolActivityItem[] = [{ event, blockKey }];
    while (index + 1 < events.length) {
      const next = events[index + 1];
      if (
        next.kind !== 'tool_use' ||
        next.attemptNo !== event.attemptNo ||
        next.parentToolUseId !== event.parentToolUseId ||
        (showActivityDetails && parseToolActivity(next.tool, next.input))
      )
        break;
      index += 1;
      tools.push({ event: next, blockKey: eventBlockKey(jobId, next, index) });
    }
    rows.push(<ToolActivity key={blockKey} tools={tools} />);
  }
  return <>{rows}</>;
}

export function CopyIcon() {
  return (
    <svg
      aria-hidden="true"
      className="h-3.5 w-3.5"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <rect x="9" y="9" width="11" height="11" rx="2" />
      <path strokeLinecap="round" d="M5 15V7a2 2 0 0 1 2-2h8" />
    </svg>
  );
}

function ForkIcon() {
  return (
    <svg
      aria-hidden="true"
      className="h-3.5 w-3.5"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <circle cx="6" cy="5" r="2" />
      <circle cx="18" cy="5" r="2" />
      <circle cx="6" cy="19" r="2" />
      <path strokeLinecap="round" d="M6 7v10m12-10v3a4 4 0 0 1-4 4h-4" />
    </svg>
  );
}

export function RewindIcon() {
  return (
    <svg
      aria-hidden="true"
      className="h-3.5 w-3.5"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth={2}
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9 15 3 9l6-6M3 9h12a6 6 0 0 1 0 12h-3"
      />
    </svg>
  );
}

export async function copyMessage(content: string) {
  try {
    await navigator.clipboard.writeText(content);
    toast.success('Message copied');
  } catch {
    toast.error('Could not copy the message');
  }
}

export function MessageActionButton({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      title={label}
      className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-gray-400 dark:text-gray-500 hover:bg-gray-100 dark:hover:bg-white/10 hover:text-gray-700 dark:hover:text-gray-200 disabled:opacity-40"
    >
      {children}
    </button>
  );
}

/** Hover-revealed per-message actions; focus keeps them visible for keyboards. */
export function MessageActions({
  align = 'start',
  className = '',
  children,
}: {
  align?: 'start' | 'end';
  className?: string;
  children: ReactNode;
}) {
  return (
    <div
      className={`mt-1 flex gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100 ${
        align === 'end' ? 'justify-end' : ''
      } ${className}`}
    >
      {children}
    </div>
  );
}

function AssistantMessage({
  text,
  extraActions,
  footer,
  blockKey,
}: {
  text: string;
  extraActions?: ReactNode;
  footer?: ReactNode;
  /** Stable row identity, so a layout change never moves a reader off it. */
  blockKey?: string;
}) {
  const actions = (
    <MessageActions>
      <MessageActionButton label="Copy message" onClick={() => void copyMessage(text)}>
        <CopyIcon />
        Copy
      </MessageActionButton>
      {extraActions}
    </MessageActions>
  );
  return (
    <div data-block-key={blockKey} className="group py-3 text-gray-800 dark:text-gray-200">
      <Markdown text={text} />
      {footer ? (
        <div className="mt-2 flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
          {footer}
          {actions}
        </div>
      ) : (
        actions
      )}
    </div>
  );
}

export function ThreadTurn({
  message,
  busy,
  onFork,
  onRewind,
  footer,
  blockKey,
  goalTurn,
}: {
  message: AgentThreadMessage;
  footer?: ReactNode;
  busy?: boolean;
  onFork?: () => void;
  onRewind?: () => void;
  blockKey?: string;
  /** How this turn came to exist, and what it reported about the goal. */
  goalTurn?: GoalTurn;
}) {
  const t = useT();
  if (message.role === 'assistant') {
    return (
      <AssistantMessage
        text={message.content}
        footer={footer}
        blockKey={blockKey}
        extraActions={
          onFork ? (
            <MessageActionButton label="Fork from here" onClick={onFork} disabled={busy}>
              <ForkIcon />
              Fork from here
            </MessageActionButton>
          ) : undefined
        }
      />
    );
  }
  if (goalTurn?.source === 'goal') {
    // Not a bubble, because nobody wrote it. The round's body is the
    // instruction the platform generated, and it stays in the transcript for
    // anyone who wants to read it — collapsed, because it is the same standing
    // text every round and would otherwise be most of what the conversation
    // appears to be.
    const report = reportKey(goalTurn);
    return (
      <div data-block-key={blockKey} className="my-4">
        <details className="group rounded-lg border border-dashed border-gray-300 px-3 py-2 dark:border-white/15">
          <summary className="flex cursor-pointer list-none items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <GoalIcon className="h-3.5 w-3.5 shrink-0" />
            <span className="min-w-0 flex-1 truncate">
              {goalTurn.kind === 'repair'
                ? t('goal.turnRepair')
                : t('goal.turnWork', { round: goalTurn.round ?? 1 })}
            </span>
            {report ? (
              <span className="shrink-0 rounded bg-gray-100 px-1.5 py-0.5 text-[10px] dark:bg-white/10">
                {t(report)}
              </span>
            ) : null}
            {goalTurn.reportDetail ? (
              <span
                className="min-w-0 max-w-64 shrink-0 truncate text-[10px] text-gray-400 dark:text-gray-500"
                title={goalTurn.reportDetail}
              >
                {goalTurn.reportDetail}
              </span>
            ) : null}
          </summary>
          <div className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            <Markdown text={message.content} />
          </div>
        </details>
      </div>
    );
  }
  return <UserMessage text={message.content} {...{ blockKey, busy, onRewind }} />;
}

/**
 * The badge for what a round said about the goal, or nothing when there is
 * nothing to say.
 *
 * Read from the platform's own parse and never from the message text: a report
 * it could not read is shown as exactly that, and is never a completion. An
 * `absent` parse is a round that reliably made no report, which is a round that
 * simply keeps working.
 */
function reportKey(turn: GoalTurn): TranslationKey | undefined {
  if (turn.reportParse === 'invalid' || turn.reportParse === 'unavailable') {
    return 'goal.reportUnreadable';
  }
  if (turn.reportParse !== 'valid') return undefined;
  if (turn.reportOutcome === 'complete') return 'goal.reportComplete';
  if (turn.reportOutcome === 'needs_input') return 'goal.reportNeedsInput';
  return undefined;
}

function UserMessage({
  text,
  blockKey,
  busy,
  onRewind,
}: {
  text: string;
  blockKey?: string;
  busy?: boolean;
  onRewind?: () => void;
}) {
  return (
    <div data-block-key={blockKey} className="group my-3 ml-auto w-fit max-w-[88%]">
      <div className="rounded-2xl bg-gray-100 dark:bg-white/5 px-4 py-3 text-sm leading-relaxed text-gray-800 dark:text-gray-200">
        <Markdown text={text} />
      </div>
      <MessageActions align="end" className="mt-0">
        <MessageActionButton label="Copy message" onClick={() => void copyMessage(text)}>
          <CopyIcon />
          Copy
        </MessageActionButton>
        {onRewind ? (
          <MessageActionButton label="Edit & rewind" onClick={onRewind} disabled={busy}>
            <RewindIcon />
            Edit & rewind
          </MessageActionButton>
        ) : null}
      </MessageActions>
    </div>
  );
}

export function EventRow({
  event,
  onFork,
  busy,
  footer,
  blockKey,
  firstRun,
}: {
  event: AgentEvent;
  onFork?: () => void;
  busy?: boolean;
  footer?: ReactNode;
  /** Stable row identity, so a layout change never moves a reader off it. */
  blockKey?: string;
  /** The first turn of the owner's first conversation: milestones say what they mean. */
  firstRun?: boolean;
}) {
  if (event.kind === 'lifecycle')
    return <LifecycleRow text={event.text} blockKey={blockKey} firstRun={firstRun} />;
  if (event.kind === 'thinking') {
    return (
      <div
        data-block-key={blockKey}
        aria-label="Agent reasoning"
        className="flex items-center gap-2.5 py-2 text-[13px] text-gray-400 dark:text-gray-500"
      >
        <span className="h-1.5 w-1.5 rounded-full bg-gray-300 dark:bg-gray-600" />
        Agent is reasoning
      </div>
    );
  }
  if (event.kind === 'message') {
    return (
      <AssistantMessage
        text={event.text}
        footer={footer}
        blockKey={blockKey}
        extraActions={
          onFork ? (
            <MessageActionButton label="Fork from here" onClick={onFork} disabled={busy}>
              <ForkIcon />
              Fork from here
            </MessageActionButton>
          ) : undefined
        }
      />
    );
  }
  if (event.kind === 'user_message') return <UserMessage text={event.text} blockKey={blockKey} />;
  if (event.kind === 'tool_use') return <RichToolActivity event={event} blockKey={blockKey} />;
  if (event.kind === 'tool_result') {
    return (
      <details
        data-block-key={blockKey}
        className="my-1 rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900"
      >
        <summary className="cursor-pointer px-3 py-2 text-[13px] text-gray-500 dark:text-gray-400">
          Unmatched tool result
        </summary>
        <pre className="m-3 max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-gray-950 px-3 py-2.5 font-mono text-xs text-gray-200">
          {event.text || 'Command completed with no output.'}
        </pre>
      </details>
    );
  }
  if (event.kind === 'terminal') {
    return (
      <pre
        data-block-key={blockKey}
        className="my-1 max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-gray-950 px-3 py-2.5 font-mono text-xs text-gray-200"
      >
        {event.text}
      </pre>
    );
  }
  return (
    <p data-block-key={blockKey} className="py-1 text-[11px] text-gray-400 dark:text-gray-500">
      {event.text}
    </p>
  );
}

export function HistoryTurn({
  turn,
  rewindAnchorJobId,
  busy,
  onFork,
  onRewind,
  onOpenRun,
  showActivityDetails = true,
}: {
  turn: ConversationTurnDescription;
  rewindAnchorJobId?: string;
  busy: boolean;
  onFork?: (jobId: string) => void;
  onRewind?: (jobId: string, prompt: string) => void;
  onOpenRun?: (jobId: string, turnNo: number) => void;
  showActivityDetails?: boolean;
}) {
  // What the transcript renders is decided once, by the turn description the
  // navigation rail also reads.
  const { jobId } = turn;
  const lastEventMessageIndex = turn.events.reduce(
    (last, event, index) => (event.kind === 'message' ? index : last),
    -1,
  );
  const lastFallbackMessageId = turn.fallbackResponses.at(-1)?.id;

  return (
    <section aria-label={`Turn ${turn.displayNo}`} data-turn-key={turn.key}>
      {turn.promptCard !== null ? (
        <UserMessage text={turn.promptCard} blockKey={`${jobId}:prompt`} />
      ) : null}
      {/* The prompt opens the turn; a message sent into it reads where it arrived. */}
      {turn.messages
        .filter((message) => message.role === 'user' && !message.midTurn)
        .map((message) => (
          <ThreadTurn
            key={message.id}
            message={message}
            goalTurn={turn.goalTurn}
            blockKey={messageBlockKey(jobId, message.id)}
            busy={busy}
            onRewind={
              rewindAnchorJobId && onRewind
                ? () => onRewind(rewindAnchorJobId, message.content)
                : undefined
            }
          />
        ))}
      <div aria-label={`Turn ${turn.displayNo} agent activity`} className="mt-2">
        <EventList
          events={turn.events}
          jobId={jobId}
          showActivityDetails={showActivityDetails}
          renderEvent={(event, index, blockKey) => (
            <EventRow
              event={event}
              blockKey={blockKey}
              busy={busy}
              onFork={
                event.kind === 'message' && index === lastEventMessageIndex && onFork
                  ? () => onFork(turn.jobId)
                  : undefined
              }
            />
          )}
        />
        {turn.fallbackResponses.map((message) => (
          <ThreadTurn
            key={message.id}
            message={message}
            blockKey={messageBlockKey(jobId, message.id)}
            busy={busy}
            onFork={
              message.id === lastFallbackMessageId && onFork ? () => onFork(turn.jobId) : undefined
            }
          />
        ))}
      </div>
      {onOpenRun ? (
        <div className="mb-6 mt-3 flex justify-end text-[11px] text-gray-400 dark:text-gray-500">
          <button
            type="button"
            onClick={() => onOpenRun(turn.jobId, turn.displayNo)}
            className="rounded px-1.5 py-0.5 transition-colors hover:bg-gray-100 hover:text-gray-900 dark:hover:bg-white/10 dark:hover:text-gray-100"
          >
            View execution
          </button>
        </div>
      ) : null}
    </section>
  );
}

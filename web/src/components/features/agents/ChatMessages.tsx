'use client';

import type { ReactNode } from 'react';
import toast from 'react-hot-toast';

import { Markdown } from '@/components/ui/Markdown';
import { lifecyclePhaseLabel } from './adapt';
import {
  eventBlockKey,
  messageBlockKey,
  type ConversationTurnDescription,
} from './conversationTurnNavigation';
import type { AgentEvent, AgentThreadMessage } from './types';

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

function LifecycleRow({ text, blockKey }: { text: string; blockKey?: string }) {
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

  return (
    <div
      data-block-key={blockKey}
      className="flex items-center gap-2.5 py-2 text-[13px] text-gray-500 dark:text-gray-400"
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
}

function ToolActivity({
  event,
  blockKey,
}: {
  event: Extract<AgentEvent, { kind: 'tool_use' }>;
  blockKey?: string;
}) {
  return (
    <details
      data-block-key={blockKey}
      className="group my-1 rounded-lg border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900"
    >
      <summary className="flex cursor-pointer list-none items-center gap-2.5 px-3 py-2 text-[13px] text-gray-700 dark:text-gray-300 marker:content-none">
        <Chevron className="shrink-0 text-gray-400 dark:text-gray-500 transition-transform group-open:rotate-90" />
        <ToolIcon tool={event.tool} />
        <span className="font-medium">{event.tool}</span>
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-gray-500 dark:text-gray-400">
          {event.detail}
        </span>
        {event.diffStat ? (
          <span className="shrink-0 font-mono text-xs text-gray-500 dark:text-gray-400">
            {event.diffStat}
          </span>
        ) : null}
      </summary>
      <div className="border-t border-gray-100 dark:border-white/5 px-3 py-2.5">
        {event.output ? (
          <pre
            className={`max-h-72 overflow-auto whitespace-pre-wrap rounded-md px-3 py-2.5 font-mono text-xs leading-relaxed ${
              event.outputIsError ? 'bg-red-950 text-red-100' : 'bg-gray-950 text-gray-200'
            }`}
          >
            {event.output.join('\n')}
          </pre>
        ) : (
          <p className="text-xs text-gray-400 dark:text-gray-500">Waiting for the tool result…</p>
        )}
      </div>
    </details>
  );
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
  children,
}: {
  align?: 'start' | 'end';
  children: ReactNode;
}) {
  return (
    <div
      className={`mt-1 flex gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100 ${
        align === 'end' ? 'justify-end' : ''
      }`}
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
}: {
  message: AgentThreadMessage;
  footer?: ReactNode;
  busy?: boolean;
  onFork?: () => void;
  onRewind?: () => void;
  blockKey?: string;
}) {
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
  return <UserMessage text={message.content} {...{ blockKey, busy, onRewind }} />;
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
      <MessageActions align="end">
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
}: {
  event: AgentEvent;
  onFork?: () => void;
  busy?: boolean;
  footer?: ReactNode;
  /** Stable row identity, so a layout change never moves a reader off it. */
  blockKey?: string;
}) {
  if (event.kind === 'lifecycle') return <LifecycleRow text={event.text} blockKey={blockKey} />;
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
  if (event.kind === 'tool_use') return <ToolActivity event={event} blockKey={blockKey} />;
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
  if (event.kind === 'egress_denied') {
    return (
      <div
        data-block-key={blockKey}
        className="my-2 flex items-center gap-2 rounded-lg bg-red-50 dark:bg-red-500/10 px-3 py-2 text-[13px] text-red-700 dark:text-red-400"
      >
        <span className="font-medium">Network request blocked</span>
        <span className="font-mono text-xs">{event.host}</span>
        <span className="text-red-500 dark:text-red-400">× {event.attempts}</span>
      </div>
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
}: {
  turn: ConversationTurnDescription;
  rewindAnchorJobId?: string;
  busy: boolean;
  onFork?: (jobId: string) => void;
  onRewind?: (jobId: string, prompt: string) => void;
  onOpenRun?: (jobId: string, turnNo: number) => void;
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
      {turn.messages
        .filter((message) => message.role === 'user')
        .map((message) => (
          <ThreadTurn
            key={message.id}
            message={message}
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
        {turn.events.map((event, index) => (
          <EventRow
            key={eventBlockKey(jobId, event, index)}
            event={event}
            blockKey={eventBlockKey(jobId, event, index)}
            busy={busy}
            onFork={
              event.kind === 'message' && index === lastEventMessageIndex && onFork
                ? () => onFork(turn.jobId)
                : undefined
            }
          />
        ))}
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

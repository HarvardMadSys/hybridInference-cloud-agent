'use client';

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
  type ReactNode,
} from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import toast from 'react-hot-toast';

import { Markdown } from '@/components/ui/Markdown';
import { basePath } from '@/config/env';
import { useT } from '@/components/providers';
import {
  cancelAgentJob,
  followUpAgentJob,
  forkAgentJob,
  getAgentConfig,
  getAgentJobGit,
  restoreAgentJob,
  type AgentConfigApi,
  type AgentGitWorkspaceApi,
} from '@/lib/api/agents';

import { lifecyclePhaseLabel, ownerFailureMessage, toDiffFileDetails } from './adapt';
import { isComposerSubmit } from './composerKeys';
import {
  conversationNavigationScope,
  describeCurrentTurn,
  describeHistoryTurns,
  eventBlockKey,
  messageBlockKey,
  sameNavigationEntries,
  toNavigationEntry,
  type ConversationTurnDescription,
  type ConversationTurnNavigationEntry,
} from './conversationTurnNavigation';
import { highestEffort, offeredEfforts } from './effort';
import { LedgerUsage, useLedgerUsage } from './LedgerUsage';
import { PaneResizer } from './PaneResizer';
import { Picker } from './Picker';
import { BackToBottom, TurnRail } from './TurnNavigator';
import { TerminalWorkspace } from './TerminalWorkspace';
import { ThreadStatsBar } from './ThreadStatsBar';
import { ThreadActions } from './ThreadActions';
import type { AgentEvent, AgentJob, AgentThreadMessage } from './types';
import type { AgentJobApi, AgentJobEventApi, AgentThreadEventApi } from '@/lib/api/agents';
import { AttemptTurnFooter } from './AttemptTurnFooter';
import { TrajectoryView } from './TrajectoryView';
import {
  TrajectoryInspector,
  normalizeStepTab,
  type InspectorSelection,
  type RunTab,
  type StepTab,
} from './TrajectoryInspector';
import { buildTrajectorySteps, turnContextFromJobs } from './trajectorySteps';
import { shouldHoldReading, useConversationScroll } from './useConversationScroll';
import { useResizablePane } from './useResizablePane';
import { WorkspaceFiles } from './WorkspaceFiles';

type WorkspaceTab = 'git' | 'terminal' | 'files';

export const WORKSPACE_WIDTH_STORAGE_KEY = 'agents.workspace.width';
const WORKSPACE_DEFAULT_WIDTH = 560;
const WORKSPACE_MIN_WIDTH = 360;
const WORKSPACE_MAX_WIDTH = 1200;
// The transcript is the reason the page exists; the workspace stops widening
// before the conversation and its composer get squeezed into a column.
const TRANSCRIPT_MIN_WIDTH = 400;
// The rail needs room to sit beside the text rather than over it, and a band
// tall enough that its marks mean something. Otherwise the transcript keeps
// its normal scrolling, without adding a second navigation surface.
const RAIL_MIN_COLUMN_WIDTH = 720;
const RAIL_MIN_READING_HEIGHT = 200;
const RAIL_MAX_HEIGHT = 420;
const RAIL_BAND_MARGIN = 64;
const BACK_TO_BOTTOM_INSET = 16;

// The inspector is narrower than the workspace: it holds one step's detail,
// not a terminal. It still has to leave the trajectory list enough room to scan.
export const INSPECTOR_WIDTH_STORAGE_KEY = 'agents.inspector.width';
const INSPECTOR_DEFAULT_WIDTH = 400;
const INSPECTOR_MIN_WIDTH = 320;
const INSPECTOR_MAX_WIDTH = 640;
const TRAJECTORY_MIN_WIDTH = 420;

const WORKSPACE_TABS: Array<{ key: WorkspaceTab; label: string }> = [
  { key: 'git', label: 'Git' },
  { key: 'terminal', label: 'Terminal' },
  { key: 'files', label: 'Files' },
];

const DIFF_LINE_CLASS = {
  hunk: 'bg-gray-50 dark:bg-gray-950 py-1 text-gray-400 dark:text-gray-500',
  ctx: 'text-gray-600 dark:text-gray-400',
  add: 'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
  del: 'bg-red-50 dark:bg-red-500/10 text-red-700 dark:text-red-400',
} as const;

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

function CopyIcon() {
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

function RewindIcon() {
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

async function copyMessage(content: string) {
  try {
    await navigator.clipboard.writeText(content);
    toast.success('Message copied');
  } catch {
    toast.error('Could not copy the message');
  }
}

function MessageActionButton({
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
function MessageActions({
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

function ThreadTurn({
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
  return (
    <div data-block-key={blockKey} className="group my-3 ml-auto w-fit max-w-[88%]">
      <div className="rounded-2xl bg-gray-100 dark:bg-white/5 px-4 py-3 text-sm leading-relaxed text-gray-800 dark:text-gray-200">
        <Markdown text={message.content} />
      </div>
      <MessageActions align="end">
        <MessageActionButton label="Copy message" onClick={() => void copyMessage(message.content)}>
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

function EventRow({
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

function HistoryTurn({
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
      <div className="mb-6 mt-3 flex justify-end text-[11px] text-gray-400 dark:text-gray-500">
        {onOpenRun ? (
          <button
            type="button"
            onClick={() => onOpenRun(turn.jobId, turn.displayNo)}
            className="rounded px-1.5 py-0.5 transition-colors hover:bg-gray-100 hover:text-gray-900 dark:hover:bg-white/10 dark:hover:text-gray-100"
          >
            View execution
          </button>
        ) : null}
      </div>
    </section>
  );
}

function hasLostContext(job: AgentJob): boolean {
  return job.nativeState === 'lost' || job.contextStatus === 'lost';
}

function ownerRunNote(job: AgentJob): string | undefined {
  // Success detail contains runner/publication bookkeeping, not the answer.
  // The transcript carries the result; publication has its own status row.
  if (job.state === 'done' || job.state === 'needs_review') return undefined;
  // `stateNote` is already owner-facing: toDisplayJob translates `job.detail`
  // through ownerFailureMessage before it reaches this display model.
  return (
    job.stateNote ||
    (job.state === 'failed'
      ? 'The run stopped before it could finish. No failure details were recorded.'
      : undefined)
  );
}

function RunGuidance({ job }: { job: AgentJob }) {
  if (hasLostContext(job)) {
    return (
      <div className="mt-2 space-y-2 text-xs text-gray-600 dark:text-gray-400">
        <p>
          We couldn’t reconnect this chat. You can still review the conversation and any available
          workspace files. Start a new chat to continue.
        </p>
        <Link
          href="/"
          className="inline-flex rounded-md bg-white px-3 py-1.5 font-medium text-gray-700 ring-1 ring-gray-200 hover:bg-gray-50 dark:bg-gray-900 dark:text-gray-300 dark:ring-white/10 dark:hover:bg-white/5"
        >
          New chat
        </Link>
      </div>
    );
  }
  if (job.state === 'cancelled') {
    return (
      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
        Send a follow-up to continue from any saved intermediate changes.
      </p>
    );
  }
  if (job.state === 'failed') {
    return (
      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
        Review the failure details, then send a follow-up to try again.
      </p>
    );
  }
  return null;
}

function RunActions({
  job,
  onOpenDiff,
  showChanges = job.diffFiles.length > 0,
}: {
  job: AgentJob;
  onOpenDiff: () => void;
  showChanges?: boolean;
}) {
  return (
    <>
      {showChanges ? (
        <button
          type="button"
          onClick={onOpenDiff}
          className="rounded-md bg-white dark:bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-gray-700 dark:text-gray-300 shadow-sm ring-1 ring-inset ring-gray-200 dark:ring-white/10 hover:bg-gray-50 dark:hover:bg-white/5"
        >
          View changes
        </button>
      ) : null}
      {job.prUrl ? (
        <a
          href={job.prUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="rounded-md bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-[13px] font-medium text-white dark:text-gray-900 hover:bg-gray-800 dark:hover:bg-white"
        >
          Open draft PR
        </a>
      ) : null}
    </>
  );
}

function RunNotices({ job, showNetworkWarning }: { job: AgentJob; showNetworkWarning: boolean }) {
  const publicationNote =
    job.workspacePublicationState === 'failed'
      ? 'Could not publish changes'
      : job.workspacePublicationState === 'publishing'
        ? 'Publishing changes…'
        : null;
  return (
    <>
      {!job.prUrl && publicationNote ? (
        <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">{publicationNote}</p>
      ) : null}
      {showNetworkWarning && job.egressDenials > 0 ? (
        <p
          role="note"
          className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-500/10 dark:text-amber-300"
        >
          {job.egressDenials} network request{job.egressDenials === 1 ? ' was' : 's were'} blocked
          by this workspace’s network settings. This may have prevented part of your task from
          completing.
        </p>
      ) : null}
    </>
  );
}

function OutcomeCard({ job, onOpenDiff }: { job: AgentJob; onOpenDiff: () => void }) {
  if (job.state === 'running' || job.state === 'queued') return null;
  const successful = job.state === 'done' || job.state === 'needs_review';
  if (successful) {
    const hasActions = job.diffFiles.length > 0 || Boolean(job.prUrl);
    if (!hasActions && !hasLostContext(job)) return null;
    return (
      <div className="mt-6">
        {hasActions ? (
          <div className="flex flex-wrap items-center gap-3">
            <RunActions job={job} onOpenDiff={onOpenDiff} />
          </div>
        ) : null}
        <RunGuidance job={job} />
      </div>
    );
  }
  const runNote = ownerRunNote(job);
  return (
    <div className="mt-6 rounded-xl border border-gray-200 dark:border-white/10 bg-gray-50 dark:bg-gray-950 px-4 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-gray-900 dark:text-gray-50">
            {job.state === 'failed' ? 'Run failed' : 'Run stopped'}
          </p>
          {runNote ? (
            <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400 [overflow-wrap:anywhere]">
              {runNote}
            </p>
          ) : null}
          <RunGuidance job={job} />
        </div>
        <RunActions job={job} onOpenDiff={onOpenDiff} />
      </div>
    </div>
  );
}

type GitView = 'diff' | 'review' | 'commits';

function GitPanel({ job, active }: { job: AgentJob; active: boolean }) {
  const [view, setView] = useState<GitView>('diff');
  const [live, setLive] = useState<AgentGitWorkspaceApi | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  useEffect(() => {
    if (!active) return undefined;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getAgentJobGit(job.id)
      .then((result) => {
        if (!cancelled) setLive(result);
      })
      .catch((cause: unknown) => {
        if (!cancelled) {
          setLive(null);
          setError(cause instanceof Error ? cause.message : 'Could not load Git workspace');
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [active, job.id, job.state, refreshKey]);

  const files = useMemo(
    () => (live?.available ? toDiffFileDetails(live.patch) : (job.diffFileDetails ?? [])),
    [job.diffFileDetails, live],
  );
  const [selectedPath, setSelectedPath] = useState(files[0]?.path ?? '');
  const selected = files.find((file) => file.path === selectedPath) ?? files[0];
  const stat = useMemo(
    () =>
      files.reduce((total, file) => ({ add: total.add + file.add, del: total.del + file.del }), {
        add: 0,
        del: 0,
      }),
    [files],
  );

  useEffect(() => {
    if (files.length && !files.some((file) => file.path === selectedPath)) {
      setSelectedPath(files[0].path);
    }
  }, [files, selectedPath]);
  const showWorkspacePublicationStatus =
    job.workspaceShared &&
    Boolean(job.workspacePublicationState) &&
    job.workspacePublicationState !== 'published';

  return (
    <div className="space-y-4" aria-label="Git workspace">
      <header className="flex flex-wrap items-center gap-3 border-b border-gray-200 dark:border-white/10 pb-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-gray-800 dark:text-gray-200">
            {job.repo}
          </p>
          <p className="truncate font-mono text-xs text-gray-400 dark:text-gray-500">
            {live?.available ? live.branch || 'detached HEAD' : job.branch}
          </p>
        </div>
        {error || !live?.available ? (
          <span className="rounded bg-gray-100 dark:bg-white/5 px-2 py-1 text-[11px] text-gray-500 dark:text-gray-400">
            {error ? 'Unavailable' : loading ? 'Loading…' : 'Unavailable'}
          </span>
        ) : null}
        {showWorkspacePublicationStatus ? (
          <span
            className={`rounded px-2 py-1 text-[11px] ${
              job.workspacePublicationState === 'failed'
                ? 'bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-300'
                : 'bg-blue-50 text-blue-700 dark:bg-blue-500/10 dark:text-blue-300'
            }`}
          >
            {job.workspacePublicationState === 'failed'
              ? 'Shared snapshot needs review'
              : 'Shared snapshot pending'}
          </span>
        ) : null}
        <button
          type="button"
          onClick={() => setRefreshKey((value) => value + 1)}
          disabled={loading}
          className="ml-auto rounded-md border border-gray-200 dark:border-white/10 px-2.5 py-1 text-xs text-gray-500 dark:text-gray-400 hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-50"
        >
          {loading ? 'Refreshing…' : 'Refresh'}
        </button>
      </header>

      <div
        className="flex items-center gap-5 border-b border-gray-200 dark:border-white/10"
        role="tablist"
      >
        {(['diff', 'review', 'commits'] as GitView[]).map((item) => (
          <button
            key={item}
            type="button"
            role="tab"
            aria-selected={view === item}
            onClick={() => setView(item)}
            className={`border-b-2 px-1 pb-2 text-xs font-medium capitalize ${
              view === item
                ? 'border-gray-900 dark:border-gray-100 text-gray-900 dark:text-gray-50'
                : 'border-transparent text-gray-400 dark:text-gray-500 hover:text-gray-700 dark:hover:text-gray-200'
            }`}
          >
            {item}
          </button>
        ))}
        {view === 'diff' && files.length ? (
          <span className="ml-auto pb-2 font-mono text-[11px]">
            <span className="text-emerald-600 dark:text-emerald-400">+{stat.add}</span>{' '}
            <span className="text-red-500 dark:text-red-400">−{stat.del}</span>
          </span>
        ) : null}
      </div>

      {error ? (
        <p className="text-xs text-red-600 dark:text-red-400">{ownerFailureMessage(error)}</p>
      ) : null}

      {view === 'diff' && files.length ? (
        <section className="overflow-hidden rounded-xl border border-gray-200 dark:border-white/10 lg:grid lg:grid-cols-[minmax(14rem,0.35fr)_minmax(0,1fr)]">
          <div className="border-b border-gray-200 dark:border-white/10 bg-gray-50/60 dark:bg-gray-900/60 lg:border-b-0 lg:border-r">
            <div className="flex items-center px-3 py-2.5 text-xs text-gray-500 dark:text-gray-400">
              <span>
                {files.length} changed file{files.length === 1 ? '' : 's'}
              </span>
              <span className="ml-auto font-mono">Changes</span>
            </div>
            <div className="max-h-[34rem] overflow-auto border-t border-gray-200 dark:border-white/10">
              {files.map((file) => (
                <button
                  key={file.path}
                  type="button"
                  onClick={() => setSelectedPath(file.path)}
                  className={`flex w-full items-center gap-3 border-b border-gray-100 dark:border-white/5 px-3 py-2.5 text-left text-xs last:border-b-0 ${
                    selected?.path === file.path
                      ? 'bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-50'
                      : 'text-gray-600 dark:text-gray-400 hover:bg-white dark:hover:bg-white/10'
                  }`}
                >
                  <span className="min-w-0 flex-1 truncate font-mono">{file.path}</span>
                  <span className="shrink-0 font-mono">
                    <span className="text-emerald-600 dark:text-emerald-400">+{file.add}</span>{' '}
                    <span className="text-red-500 dark:text-red-400">−{file.del}</span>
                  </span>
                </button>
              ))}
            </div>
          </div>
          <div className="min-w-0 bg-white dark:bg-gray-900">
            <div className="border-b border-gray-100 dark:border-white/5 px-4 py-2.5 font-mono text-xs text-gray-600 dark:text-gray-400">
              {selected?.path}
            </div>
            <pre className="max-h-[34rem] overflow-auto font-mono text-xs leading-relaxed">
              <code>
                {selected?.lines.map((line, index) => (
                  <span
                    key={index}
                    className={`block min-w-max px-4 ${DIFF_LINE_CLASS[line.marker]}`}
                  >
                    {line.text || ' '}
                  </span>
                ))}
              </code>
            </pre>
          </div>
        </section>
      ) : view === 'diff' ? (
        <div className="rounded-xl border border-dashed border-gray-200 dark:border-white/10 px-5 py-12 text-center text-sm text-gray-400 dark:text-gray-500">
          No changes yet.
        </div>
      ) : view === 'review' ? (
        <section className="space-y-4 rounded-xl border border-gray-200 dark:border-white/10 p-5">
          <div className="flex items-center gap-3">
            <div>
              <p className="text-sm font-medium text-gray-800 dark:text-gray-200">
                {files.length} changed file{files.length === 1 ? '' : 's'}
              </p>
              <p className="mt-1 text-xs text-gray-400 dark:text-gray-500">
                Compared with {job.baseRef || job.baseSha || 'the pinned base'}
              </p>
            </div>
            <p className="ml-auto font-mono text-xs">
              <span className="text-emerald-600 dark:text-emerald-400">+{stat.add}</span>{' '}
              <span className="text-red-500 dark:text-red-400">−{stat.del}</span>
            </p>
          </div>
          {files.length ? (
            <ul className="divide-y divide-gray-100 dark:divide-white/5 border-y border-gray-100 dark:border-white/5">
              {files.map((file) => (
                <li key={file.path} className="flex gap-3 py-2.5 font-mono text-xs">
                  <span className="min-w-0 flex-1 truncate text-gray-600 dark:text-gray-400">
                    {file.path}
                  </span>
                  <span className="text-emerald-600 dark:text-emerald-400">+{file.add}</span>
                  <span className="text-red-500 dark:text-red-400">−{file.del}</span>
                </li>
              ))}
            </ul>
          ) : null}
          {job.prUrl ? (
            <a
              href={job.prUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex rounded-md bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-xs font-medium text-white dark:text-gray-900 hover:bg-gray-800 dark:hover:bg-white"
            >
              Open pull request
            </a>
          ) : (
            <p className="text-xs text-gray-400 dark:text-gray-500">
              Pull request not published yet.
            </p>
          )}
        </section>
      ) : (
        <section className="overflow-hidden rounded-xl border border-gray-200 dark:border-white/10">
          {live?.available && live.commits.length ? (
            <ul className="divide-y divide-gray-100 dark:divide-white/5">
              {live.commits.map((commit) => (
                <li key={commit.sha} className="px-4 py-3">
                  <div className="flex items-start gap-3">
                    <span className="font-mono text-xs text-gray-400 dark:text-gray-500">
                      {commit.short_sha}
                    </span>
                    <div className="min-w-0">
                      <p className="truncate text-sm text-gray-700 dark:text-gray-300">
                        {commit.subject}
                      </p>
                      <p className="mt-1 text-[11px] text-gray-400 dark:text-gray-500">
                        {commit.author} · {commit.authored_at}
                      </p>
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="px-5 py-12 text-center text-sm text-gray-400 dark:text-gray-500">
              Commit history is available for live workspaces.
            </p>
          )}
        </section>
      )}
    </div>
  );
}

export function JobDetail({
  job,
  events = [],
  threadJobs = [],
  onReload,
}: {
  job: AgentJob;
  /** The stored stream, for the trajectory tab. Absent in fixtures. */
  events?: (AgentJobEventApi | AgentThreadEventApi)[];
  /** The conversation's turn rows, so Run details can describe a past turn. */
  threadJobs?: AgentJobApi[];
  onReload?: () => void;
}) {
  const router = useRouter();
  const t = useT();
  const usage = useLedgerUsage(job);
  const liveAttempt = useMemo(
    () => (job.attempts.length > 0 ? job.attempts[job.attempts.length - 1].no : 0),
    [job.attempts],
  );
  const [attemptNo, setAttemptNo] = useState(liveAttempt);
  // What the remount used to do. Moving to the next turn of a conversation no
  // longer rebuilds this component, so the attempt selection has to follow the
  // turn explicitly — otherwise turn two would open showing turn one's chosen
  // attempt.
  useEffect(() => {
    setAttemptNo(liveAttempt);
    // Only when the turn changes: re-running this on every attempt update
    // would fight the reader's own selection.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.id]);
  const [title, setTitle] = useState(job.title);
  useEffect(() => setTitle(job.title), [job.title, job.threadId]);
  const [tab, setTab] = useState<'chat' | 'trajectory'>('chat');
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
  const [workspaceTab, setWorkspaceTab] = useState<WorkspaceTab>('git');
  // The right region holds either the workspace or one selected step, never
  // both: two panels would hide the trajectory that explains either of them.
  const [inspector, setInspector] = useState<InspectorSelection | null>(null);
  const [trajectoryQuery, setTrajectoryQuery] = useState('');
  const [focusJobId, setFocusJobId] = useState<string | null>(null);
  const inspectorTriggerRef = useRef<HTMLElement | null>(null);
  const trajectorySteps = useMemo(
    () => buildTrajectorySteps(events, turnContextFromJobs(threadJobs)),
    [events, threadJobs],
  );
  const [followUp, setFollowUp] = useState('');
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState(job.model);
  const [effort, setEffort] = useState(job.reasoningEffort ?? '');
  const [submitting, setSubmitting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [forking, setForking] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const stopRequestedRef = useRef(false);
  const forkRequestedRef = useRef(false);
  const workspacePane = useResizablePane({
    storageKey: WORKSPACE_WIDTH_STORAGE_KEY,
    defaultWidth: WORKSPACE_DEFAULT_WIDTH,
    minWidth: WORKSPACE_MIN_WIDTH,
    maxWidth: WORKSPACE_MAX_WIDTH,
    side: 'end',
    maxViewportFraction: 0.75,
    siblingMinWidth: TRANSCRIPT_MIN_WIDTH,
  });
  const inspectorPane = useResizablePane({
    storageKey: INSPECTOR_WIDTH_STORAGE_KEY,
    defaultWidth: INSPECTOR_DEFAULT_WIDTH,
    minWidth: INSPECTOR_MIN_WIDTH,
    maxWidth: INSPECTOR_MAX_WIDTH,
    side: 'end',
    maxViewportFraction: 0.6,
    siblingMinWidth: TRAJECTORY_MIN_WIDTH,
  });
  // Both panes size themselves against the region that holds them: only that
  // element knows how much is left once the sidebar has taken its share. The
  // workspace and the inspector never open together, but they share the
  // container, so both refs are attached to it. Without the inspector's, its
  // floor for the list has nothing to measure against, and a drag can squeeze
  // the list past the width it needs to stay readable.
  const { containerRef: workspaceContainerRef } = workspacePane;
  const { containerRef: inspectorContainerRef } = inspectorPane;
  const layoutContainerRef = useCallback(
    (node: HTMLElement | null) => {
      workspaceContainerRef(node);
      inspectorContainerRef(node);
    },
    [inspectorContainerRef, workspaceContainerRef],
  );

  useEffect(() => setAttemptNo(liveAttempt), [liveAttempt]);

  // The model remains a per-turn choice. A retained live chat reuses its CLI
  // when the process-fixed settings match; changing the model makes only that
  // member process restart and native-resume inside the same sandbox.
  useEffect(() => {
    let cancelled = false;
    getAgentConfig()
      .then((loaded) => {
        if (cancelled) return;
        setConfig(loaded);
        setModels(loaded.models ?? []);
      })
      .catch(() => {
        // A list we could not load is not a list to pick from: the composer
        // falls back to naming the inherited model, which is what a turn with
        // no override runs on anyway.
        if (!cancelled) {
          setConfig(null);
          setModels([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // The parent's model leads even when this deployment has stopped offering
  // it, so the control cannot show one model while the turn would run another.
  const modelOptions = useMemo(() => {
    if (models.length === 0) return [];
    return models.includes(job.model) ? models : [job.model, ...models];
  }, [job.model, models]);

  // Against the model this turn would run — the switched-to one, or the
  // inherited one — because a pair offered last turn need not be offered now.
  const efforts = useMemo(
    () => offeredEfforts(config, job.runtime, model || job.model),
    [config, job.runtime, job.model, model],
  );
  useEffect(() => {
    // Only once the config has arrived. Before that the offered set is empty
    // because nothing is *known*, not because nothing is offered — and acting
    // on it would wipe the inherited level a moment before the answer lands.
    if (!config) return;
    // The inherited level leads; a turn that inherited nothing, or one whose
    // switched-to model does not offer what it inherited, falls to the deepest
    // the new pair has.
    if (!efforts.includes(effort)) setEffort(highestEffort(efforts));
  }, [config, efforts, effort]);

  // Edit & rewind creates the native child immediately, then leaves the
  // replacement prompt as a one-shot draft on that child's landing page.
  useEffect(() => {
    const key = `agent-rewind-draft-${job.id}`;
    let draft: string | null = null;
    try {
      draft = sessionStorage.getItem(key);
      if (draft) sessionStorage.removeItem(key);
    } catch {
      return;
    }
    if (draft) setFollowUp(draft);
  }, [job.id]);

  const selectedAttempt = job.attempts.find((attempt) => attempt.no === attemptNo);
  const runNote = ownerRunNote(job);
  const isArchived = Boolean(job.archivedAt);
  const isActive = job.state === 'running' || job.state === 'queued';
  const isSuccessful = job.state === 'done' || job.state === 'needs_review';
  // `contextStatus` describes the selected attempt. `nativeState` is the
  // current membership truth, so it also closes the composer when someone is
  // viewing an older successful turn after a later admitted turn was lost.
  const contextLost = hasLostContext(job);
  const latestTerminalEvent = [...job.events]
    .reverse()
    .find(
      (event) =>
        event.kind === 'lifecycle' &&
        event.attemptNo === job.currentAttemptNo &&
        (event.text === 'workspace_preparing' ||
          event.text === 'workspace_ready' ||
          event.text === 'workspace_finalizing'),
    );
  const latestTerminalPhase =
    latestTerminalEvent?.kind === 'lifecycle' ? latestTerminalEvent.text : undefined;
  const terminalWorkspaceReady =
    !isActive ||
    (job.state === 'running' &&
      job.currentAttemptNo !== undefined &&
      latestTerminalPhase === 'workspace_ready');

  useEffect(() => {
    stopRequestedRef.current = false;
    setStopping(false);
    forkRequestedRef.current = false;
    setForking(false);
  }, [job.id]);

  useEffect(() => {
    if (!isActive) {
      stopRequestedRef.current = false;
      setStopping(false);
    }
  }, [isActive]);

  function openWorkspace(tab: WorkspaceTab) {
    setWorkspaceTab(tab);
    setWorkspaceOpen(true);
    setInspector(null);
  }

  const toggleWorkspace = useCallback(() => {
    setWorkspaceOpen((current) => {
      if (!current) setInspector(null);
      return !current;
    });
  }, []);

  const closeInspector = useCallback(() => {
    setInspector(null);
    // Cleared so reopening the same turn scrolls back to it.
    setFocusJobId(null);
    const trigger = inspectorTriggerRef.current;
    inspectorTriggerRef.current = null;
    // Escape closes the panel, not the reader's place in it.
    if (trigger && document.contains(trigger)) trigger.focus();
  }, []);

  // Remember what opened the panel so closing it can hand focus back.
  const rememberInspectorTrigger = useCallback(() => {
    const active = document.activeElement;
    inspectorTriggerRef.current = active instanceof HTMLElement ? active : null;
  }, []);

  const selectStep = useCallback(
    (stepKey: string) => {
      const step = trajectorySteps.find((candidate) => candidate.key === stepKey);
      rememberInspectorTrigger();
      setWorkspaceOpen(false);
      setInspector((current) => {
        const keepTab = current?.kind === 'step' && current.stepKey === stepKey;
        const tab: StepTab = keepTab ? current.tab : 'summary';
        return { kind: 'step', stepKey, tab: step ? normalizeStepTab(step, tab) : 'summary' };
      });
    },
    [rememberInspectorTrigger, trajectorySteps],
  );

  const openRun = useCallback(
    (jobId: string, turnNo: number) => {
      rememberInspectorTrigger();
      setWorkspaceOpen(false);
      setInspector({ kind: 'run', jobId, turnNo, tab: 'overview' });
      setTab('trajectory');
      setFocusJobId(jobId);
    },
    [rememberInspectorTrigger],
  );

  const selectInspectorTab = useCallback((next: StepTab | RunTab) => {
    setInspector((current) => {
      if (!current) return current;
      return current.kind === 'step'
        ? { ...current, tab: next as StepTab }
        : { ...current, tab: next as RunTab };
    });
  }, []);

  // Step and run details explain the trajectory; Chat has no place to keep them.
  useEffect(() => {
    if (tab !== 'chat') return;
    setInspector(null);
    setFocusJobId(null);
  }, [tab]);

  // Escape closes this layer only. Controls that consume Escape themselves —
  // the composer, the terminal, any dialog — keep it.
  useEffect(() => {
    if (!inspector) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.repeat || event.defaultPrevented) return;
      const target = event.target instanceof Element ? event.target : null;
      if (
        target?.closest(
          'input, textarea, select, [contenteditable="true"], [role="dialog"], #job-workspace-pane',
        )
      ) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      closeInspector();
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [closeInspector, inspector]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const hasOnePrimaryModifier = event.metaKey !== event.ctrlKey;
      if (
        event.key.toLowerCase() !== 'j' ||
        !hasOnePrimaryModifier ||
        event.altKey ||
        event.shiftKey
      ) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      if (!event.repeat) toggleWorkspace();
    };
    // Capture before terminal/editor handlers consume the chord or send Ctrl+J to the shell.
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [toggleWorkspace]);

  const stop = useCallback(async () => {
    if (stopRequestedRef.current) return;
    stopRequestedRef.current = true;
    setStopping(true);
    setActionError(null);
    try {
      await cancelAgentJob(job.id);
      onReload?.();
    } catch (cause: unknown) {
      stopRequestedRef.current = false;
      setStopping(false);
      setActionError(cause instanceof Error ? cause.message : 'Could not stop this run');
    }
  }, [job.id, onReload]);

  useEffect(() => {
    if (!isActive) return;
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (
        event.key !== 'Escape' ||
        event.repeat ||
        event.defaultPrevented ||
        target?.closest('input, textarea, select, [contenteditable="true"]')
      ) {
        return;
      }
      event.preventDefault();
      void stop();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [isActive, stop]);

  async function submitFollowUp(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const prompt = followUp.trim();
    if (!prompt || submitting || contextLost || isArchived) return;
    setSubmitting(true);
    setActionError(null);
    try {
      const child = await followUpAgentJob(job.id, {
        prompt,
        // Only an actual switch is sent. Omitted, the turn inherits the
        // parent's model, which was validated when it was chosen.
        ...(model && model !== job.model ? { model } : {}),
        // Only an actual change is sent; an omitted field inherits the parent's
        // level, which is what leaving the control alone means. A thread that
        // carried no level and now shows one differs, so it is sent.
        ...(effort && effort !== job.reasoningEffort ? { reasoning_effort: effort } : {}),
      });
      // A router.push remounts the dynamic [jobId] segment even with
      // scroll:false and a stable JobDetail key. Native history updates Next's
      // usePathname without replacing the page, preserving the conversation
      // while useAgentJob loads the next turn. Reload and Back keep real URLs.
      window.history.pushState(null, '', `${basePath}/${encodeURIComponent(child.id)}`);
      // The reader asked for this turn, so following its output is what they
      // asked for — the one place besides "back to bottom" that resumes it.
      scroll.followLatest();
      // Also what the remount used to do. Without it the composer stayed
      // disabled with the sent text still in it, because nothing else ever
      // cleared either.
      setFollowUp('');
      setSubmitting(false);
    } catch (cause: unknown) {
      setActionError(cause instanceof Error ? cause.message : 'Could not queue the follow-up');
      setSubmitting(false);
    }
  }

  // Fork creates a second native session in this chat's shared workspace and
  // navigates to its copied display history; draft prefills Edit & rewind.
  async function forkFrom(anchorJobId: string, draft?: string) {
    if (forkRequestedRef.current || isArchived) return;
    if (
      job.workspaceShared &&
      typeof globalThis.confirm === 'function' &&
      !globalThis.confirm(
        'This creates another chat sharing the same workspace. Concurrent edits can conflict or overwrite each other. Continue?',
      )
    ) {
      return;
    }
    forkRequestedRef.current = true;
    setForking(true);
    setActionError(null);
    try {
      const fork = await forkAgentJob(anchorJobId);
      if (draft) {
        try {
          sessionStorage.setItem(`agent-rewind-draft-${fork.id}`, draft);
        } catch {
          // Blocked storage only loses the prefill, never the native fork.
        }
      }
      toast.success('Conversation forked');
      router.push(`/${fork.id}`);
      // Keep the source mutation guard closed until the child job actually
      // replaces it. App Router intentionally leaves this component showing
      // the source while the dynamic route loads; reopening here permits a
      // second click with a newly cleared idempotency key.
    } catch (cause: unknown) {
      forkRequestedRef.current = false;
      setActionError(cause instanceof Error ? cause.message : 'Could not fork the conversation');
      setForking(false);
    }
  }

  async function unarchive() {
    if (restoring) return;
    setRestoring(true);
    setActionError(null);
    try {
      await restoreAgentJob(job.id);
      onReload?.();
    } catch (cause: unknown) {
      setActionError(cause instanceof Error ? cause.message : t('sessionArchive.restoreFailed'));
    } finally {
      setRestoring(false);
    }
  }

  const canForkWorkspace = Boolean(
    job.sandboxId && job.workspaceShared && !isActive && !isArchived,
  );
  const canForkCurrent = Boolean(canForkWorkspace && job.nativeForkAvailable);
  const selectedAttemptIsCurrent = attemptNo === 0 || attemptNo === liveAttempt;
  const usageFooter = selectedAttemptIsCurrent ? (
    <LedgerUsage job={job} usage={usage} />
  ) : undefined;

  // Turns the transcript renders, described once. The Chat below and the rail
  // beside it read the same description, so a mark cannot address a turn the
  // body does not draw. The stored turns are memoized because they are
  // immutable; the newest one is rebuilt every commit because its rows are what
  // the transcript is drawing right now.
  const currentAttemptNo = job.currentAttemptNo;
  const historyTurns = describeHistoryTurns(job);
  const currentTurn = describeCurrentTurn(job, { attemptNo, currentAttemptNo });
  const threadMessages = currentTurn.messages;
  const visibleEvents = currentTurn.events;
  const currentRewindAnchor = job.historyTurns
    ? [...job.historyTurns].reverse().find((turn) => turn.forkable)?.jobId
    : undefined;

  // The rail renders two host elements per turn while `toDisplayJob()` hands
  // the page a rebuilt job per streamed event. Handing it the entries it was
  // already showing keeps those marks from being rebuilt for a delta that only
  // reaches the newest turn's body.
  const previousEntriesRef = useRef<ConversationTurnNavigationEntry[]>([]);
  const navigationEntries = useMemo(() => {
    const fresh = [
      ...historyTurns.map((turn) => toNavigationEntry(turn)),
      toNavigationEntry(currentTurn, { attemptNo, currentAttemptNo }),
    ];
    const previous = previousEntriesRef.current;
    if (sameNavigationEntries(previous, fresh)) return previous;
    previousEntriesRef.current = fresh;
    return fresh;
  }, [attemptNo, currentAttemptNo, currentTurn, historyTurns]);
  const turnKeys = useMemo(() => navigationEntries.map((entry) => entry.key), [navigationEntries]);
  const conversationKey = conversationNavigationScope(job).conversationKey;
  const scroll = useConversationScroll({
    turnKeys,
    conversationKey,
    active: tab === 'chat',
  });
  // Announced only for a jump the reader asked for, never for a streamed
  // event: a transcript that talks on every token is one nobody can listen to.
  const [announcedTurn, setAnnouncedTurn] = useState<string | null>(null);
  const jumpToTurn = useCallback(
    (key: string) => {
      const entry = navigationEntries.find((candidate) => candidate.key === key);
      setAnnouncedTurn(
        entry === undefined ? null : t('turnNavigation.turn', { turn: entry.displayNo }),
      );
      scroll.jumpToTurn(key);
    },
    [navigationEntries, scroll, t],
  );
  // What the live region says: where the reader's last jump landed, or that it
  // could not land. Never a token, and never both at once.
  const navigationNotice =
    scroll.unavailableKey === null ? (announcedTurn ?? '') : t('turnNavigation.unavailable');
  const showRail =
    tab === 'chat' &&
    navigationEntries.length > 1 &&
    scroll.viewport.width >= RAIL_MIN_COLUMN_WIDTH &&
    scroll.viewport.readingHeight >= RAIL_MIN_READING_HEIGHT &&
    scroll.viewport.finePointer;
  const composerHeight = scroll.viewport.composerHeight;

  // Switching to an older run changes what the body shows, so it is a reading
  // position, not a live one: the reader settled on a fixed snapshot. The
  // selection also moves by itself — a queued turn starts, a retry arrives —
  // and that must not stop the newest output from being followed.
  const attemptSelection = useRef({ jobId: job.id, attemptNo });
  const chosenAttemptRef = useRef<number | null>(null);
  useEffect(() => {
    const previous = attemptSelection.current;
    attemptSelection.current = { jobId: job.id, attemptNo };
    if (
      !shouldHoldReading(previous, attemptSelection.current, chosenAttemptRef.current, liveAttempt)
    )
      return;
    chosenAttemptRef.current = null;
    scroll.holdReading();
    // `scroll` changes identity on every measurement; the judgement above is
    // what decides whether this is the reader's choice at all.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attemptNo, job.id, liveAttempt]);

  const currentReplyId = [...threadMessages]
    .reverse()
    .find((message) => message.role === 'assistant' && message.jobId === job.id)?.id;
  const lastThreadAssistantMessageId = [...threadMessages]
    .reverse()
    .find((message) => message.role === 'assistant')?.id;
  const lastVisibleMessageIndex = visibleEvents.reduce(
    (last, event, index) => (event.kind === 'message' ? index : last),
    -1,
  );
  // The workspace and the inspector share one right-hand region, so either of
  // them narrows the transcript in exactly the same way.
  const panelOpen = workspaceOpen || inspector !== null;
  return (
    <section className="flex h-full min-h-0 flex-col bg-gray-50/40 dark:bg-gray-950">
      <header className="relative z-20 shrink-0 border-b border-gray-200/80 dark:border-white/10 bg-white/95 dark:bg-gray-900/95 px-6 shadow-[0_1px_0_rgba(17,24,39,0.02)] backdrop-blur">
        {isArchived ? (
          <div className="flex items-center gap-2 pt-3 text-xs text-gray-500 dark:text-gray-400">
            <Link
              href="/archived"
              className="hover:text-crimson hover:underline dark:hover:text-crimson-light"
            >
              {t('sessionArchive.back')}
            </Link>
            <span aria-hidden="true">/</span>
            <span>{t('sessionArchive.badge')}</span>
          </div>
        ) : null}
        <div className="flex w-full items-center gap-3 py-3.5 pr-14">
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-base font-semibold tracking-[-0.01em] text-gray-950 dark:text-gray-50">
              {title}
            </h1>
            <div className="mt-0.5 flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
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
                  d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"
                />
              </svg>
              <span className="truncate">{job.repo}</span>
            </div>
          </div>
          <ThreadActions
            key={job.threadId ?? job.id}
            job={{ ...job, title }}
            canFork={canForkCurrent}
            forking={forking}
            onFork={() => void forkFrom(job.id)}
            onRename={setTitle}
          />
          <nav aria-label="Run views" className="mr-3 flex shrink-0 items-center gap-1 text-[13px]">
            {(['chat', 'trajectory'] as const).map((view) => (
              <button
                key={view}
                type="button"
                onClick={() => setTab(view)}
                aria-current={tab === view ? 'page' : undefined}
                className={`rounded-lg px-2.5 py-1 capitalize transition-colors ${
                  tab === view
                    ? 'bg-gray-100 dark:bg-white/5 font-medium text-gray-900 dark:text-gray-50'
                    : 'text-gray-500 dark:text-gray-400 hover:text-gray-800 dark:hover:text-gray-200'
                }`}
              >
                {view}
              </button>
            ))}
          </nav>
          {job.diffFiles.length ? (
            <button
              type="button"
              onClick={() => openWorkspace('git')}
              className="hidden rounded-md px-2.5 py-1.5 text-[13px] font-medium text-gray-600 dark:text-gray-400 transition-colors hover:bg-gray-100 dark:hover:bg-white/10 hover:text-gray-900 dark:hover:text-gray-100 sm:block"
            >
              Changes
              <span className="ml-1 rounded bg-gray-100 dark:bg-white/5 px-1.5 py-0.5 text-[11px]">
                {job.diffFiles.length}
              </span>
            </button>
          ) : null}
          {isActive ? (
            <button
              type="button"
              onClick={() => void stop()}
              disabled={stopping}
              aria-label="Stop"
              title="Stop run (Esc)"
              className="inline-flex items-center gap-1.5 rounded-md bg-gray-900 dark:bg-gray-100 px-2.5 py-1.5 text-[13px] font-medium text-white dark:text-gray-900 transition-colors hover:bg-gray-800 dark:hover:bg-white disabled:opacity-60"
            >
              <span className="h-2.5 w-2.5 rounded-sm bg-white dark:bg-gray-900" />
              {stopping ? 'Stopping…' : 'Stop'}
              {!stopping ? (
                <kbd
                  aria-hidden="true"
                  className="rounded bg-white/15 px-1 text-[10px] dark:bg-gray-900/15"
                >
                  Esc
                </kbd>
              ) : null}
            </button>
          ) : null}
        </div>
        <button
          type="button"
          onClick={toggleWorkspace}
          aria-label={workspaceOpen ? 'Close workspace' : 'Open workspace'}
          aria-expanded={workspaceOpen}
          aria-controls="job-workspace-pane"
          aria-keyshortcuts="Meta+J Control+J"
          title={`${workspaceOpen ? 'Close workspace' : 'Open workspace'} (⌘J / Ctrl+J)`}
          className={`absolute right-6 top-1/2 inline-flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-md border transition-colors ${
            workspaceOpen
              ? 'border-gray-900 dark:border-gray-100 bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900 hover:bg-gray-800 dark:hover:bg-white'
              : 'border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-white/10 hover:text-gray-900 dark:hover:text-gray-100'
          }`}
        >
          <svg
            aria-hidden="true"
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.8}
          >
            <rect x="3.5" y="4" width="17" height="16" rx="2" />
            <path d="M14.5 4v16" />
          </svg>
        </button>
      </header>

      <div
        ref={layoutContainerRef}
        className={`min-h-0 flex-1 overflow-hidden ${panelOpen ? 'block lg:flex' : 'flex'}`}
      >
        {/* The rail's positioning layer: a sibling of the scroller, so it never
            adds to the transcript's scrollHeight, and inside the Chat column,
            so it stays beside the conversation when the workspace opens. */}
        <div className="relative flex min-w-0 flex-1">
          <div
            ref={scroll.containerRef}
            role="region"
            aria-label="Task progress"
            // A scrollable region belongs in the tab order: without it the
            // transcript can only be scrolled by focusing something inside it,
            // and the one thing this feature must never do is move a reader
            // who is looking somewhere else.
            tabIndex={0}
            className={`min-w-0 flex-1 flex-col overflow-y-auto focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-crimson/40 ${
              panelOpen ? 'hidden lg:flex' : 'flex'
            }`}
          >
            {/* The rail is an overlay, so the text gives back the width it
                occupies rather than running underneath the marks. The
                trajectory is a wide table of steps, so it keeps the width the
                inspector is not holding instead of a reading column. */}
            <div
              className={`flex w-full flex-1 flex-col pb-6 pt-7 ${
                tab === 'trajectory'
                  ? 'px-6'
                  : `mx-auto max-w-4xl ${showRail ? 'pl-6 pr-12' : 'px-6'}`
              }`}
            >
              {tab === 'trajectory' ? (
                <div className="flex-1">
                  <TrajectoryView
                    events={events}
                    steps={trajectorySteps}
                    query={trajectoryQuery}
                    onQueryChange={setTrajectoryQuery}
                    selectedKey={inspector?.kind === 'step' ? inspector.stepKey : null}
                    onSelectStep={selectStep}
                    onOpenRun={openRun}
                    focusJobId={focusJobId}
                  />
                </div>
              ) : (
                <div className="flex-1">
                  {historyTurns.map((turn, index) => {
                    const rewindAnchorJobId = [...historyTurns.slice(0, index)]
                      .reverse()
                      .find((candidate) => candidate.forkable)?.jobId;
                    return (
                      <HistoryTurn
                        key={turn.key}
                        turn={turn}
                        rewindAnchorJobId={rewindAnchorJobId}
                        busy={forking}
                        onFork={
                          turn.forkable && canForkWorkspace
                            ? (anchorJobId) => void forkFrom(anchorJobId)
                            : undefined
                        }
                        onRewind={
                          rewindAnchorJobId && canForkWorkspace
                            ? (anchorJobId, prompt) => void forkFrom(anchorJobId, prompt)
                            : undefined
                        }
                        onOpenRun={openRun}
                      />
                    );
                  })}
                  <section
                    aria-label={`Turn ${currentTurn.displayNo}`}
                    data-turn-key={currentTurn.key}
                  >
                    {threadMessages.map((message) => {
                      const rewindAnchor =
                        message.role === 'user' && currentRewindAnchor !== message.jobId
                          ? currentRewindAnchor
                          : undefined;
                      return (
                        <ThreadTurn
                          key={message.id}
                          message={message}
                          blockKey={messageBlockKey(message.jobId, message.id)}
                          footer={
                            lastVisibleMessageIndex < 0 && message.id === currentReplyId
                              ? usageFooter
                              : undefined
                          }
                          busy={forking}
                          onFork={
                            message.role === 'assistant' &&
                            message.id === lastThreadAssistantMessageId &&
                            isSuccessful &&
                            canForkCurrent
                              ? () => void forkFrom(message.jobId)
                              : undefined
                          }
                          onRewind={
                            rewindAnchor && canForkWorkspace
                              ? () => void forkFrom(rewindAnchor, message.content)
                              : undefined
                          }
                        />
                      );
                    })}

                    {job.historyIncludesPrompt ? null : (
                      <div
                        data-block-key={`${job.id}:prompt`}
                        className="group my-4 rounded-xl border border-gray-200/90 dark:border-white/10 bg-white dark:bg-gray-900 px-4 py-3 shadow-subtle"
                      >
                        <Markdown text={job.prompt || job.title} />
                        <MessageActions align="end">
                          <MessageActionButton
                            label="Copy message"
                            onClick={() => void copyMessage(job.prompt || job.title)}
                          >
                            <CopyIcon />
                            Copy
                          </MessageActionButton>
                          {currentRewindAnchor && canForkWorkspace ? (
                            <MessageActionButton
                              label="Edit & rewind"
                              onClick={() =>
                                void forkFrom(currentRewindAnchor, job.prompt || job.title)
                              }
                              disabled={forking}
                            >
                              <RewindIcon />
                              Edit & rewind
                            </MessageActionButton>
                          ) : null}
                        </MessageActions>
                      </div>
                    )}

                    {runNote && job.state !== 'failed' ? (
                      <p className="mb-2 text-xs text-gray-400 dark:text-gray-500">{runNote}</p>
                    ) : null}

                    {job.attempts.length > 1 ? (
                      <details
                        className="my-3 text-xs"
                        onToggle={(event) => {
                          if (!event.currentTarget.open) {
                            chosenAttemptRef.current = liveAttempt;
                            setAttemptNo(liveAttempt);
                          }
                        }}
                      >
                        <summary className="cursor-pointer text-gray-500 dark:text-gray-400">
                          {t('conversation.executionHistory', { count: job.attempts.length })}
                          {attemptNo !== liveAttempt
                            ? ` · ${t('conversation.viewingExecution', { number: attemptNo })}`
                            : ''}
                        </summary>
                        <p className="mt-2 text-gray-400 dark:text-gray-500">
                          {t('conversation.executionHistoryHint')}
                        </p>
                        <div className="mt-2 flex flex-wrap items-center gap-1.5">
                          {job.attempts.map((attempt) => (
                            <button
                              key={attempt.no}
                              type="button"
                              aria-pressed={attempt.no === attemptNo}
                              onClick={() => {
                                chosenAttemptRef.current = attempt.no;
                                setAttemptNo(attempt.no);
                              }}
                              className={`rounded-md px-2 py-1 ${
                                attempt.no === attemptNo
                                  ? 'bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900'
                                  : 'bg-gray-100 dark:bg-white/5 text-gray-500 dark:text-gray-400 hover:bg-gray-200 dark:hover:bg-white/15'
                              }`}
                            >
                              {attempt.no} · {attempt.status}
                            </button>
                          ))}
                        </div>
                      </details>
                    ) : null}

                    {selectedAttempt?.status === 'superseded' && selectedAttempt.note ? (
                      <div className="my-3 rounded-lg border border-amber-200 dark:border-amber-500/25 bg-amber-50 dark:bg-amber-500/10 px-3 py-2 text-[13px] text-amber-800 dark:text-amber-300">
                        {selectedAttempt.note}
                      </div>
                    ) : null}

                    <div aria-label="Agent activity" className="mt-2">
                      {visibleEvents.map((event, index) => (
                        <EventRow
                          key={eventBlockKey(job.id, event, index)}
                          event={event}
                          blockKey={eventBlockKey(job.id, event, index)}
                          busy={forking}
                          footer={index === lastVisibleMessageIndex ? usageFooter : undefined}
                          onFork={
                            event.kind === 'message' &&
                            index === lastVisibleMessageIndex &&
                            isSuccessful &&
                            canForkCurrent &&
                            selectedAttemptIsCurrent
                              ? () => void forkFrom(job.id)
                              : undefined
                          }
                        />
                      ))}
                      {job.liveNote ? (
                        <div className="flex items-center gap-2.5 py-3 text-[13px] text-gray-500 dark:text-gray-400">
                          <span className="h-2 w-2 animate-pulse rounded-full bg-blue-500" />
                          {job.liveNote}
                        </div>
                      ) : null}
                      {job.state === 'queued' && visibleEvents.length === 0 ? (
                        <div className="flex items-center gap-2.5 py-3 text-[13px] text-gray-500 dark:text-gray-400">
                          <span className="h-2 w-2 animate-pulse rounded-full bg-gray-400 dark:bg-gray-600" />
                          Waiting to start
                        </div>
                      ) : null}
                    </div>

                    <AttemptTurnFooter job={job} attemptNo={attemptNo} />

                    {/* The inspector's Run details open on the turn the reader
                        is looking at, which is this one. */}
                    <div className="mt-2">
                      <button
                        type="button"
                        onClick={() => openRun(job.id, job.turnNo ?? 1)}
                        className="rounded-md px-1.5 py-0.5 text-[11px] font-medium text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100"
                      >
                        View execution
                      </button>
                    </div>

                    <OutcomeCard job={job} onOpenDiff={() => openWorkspace('git')} />
                    {selectedAttemptIsCurrent ? (
                      <RunNotices
                        job={job}
                        showNetworkWarning={
                          !visibleEvents.some((event) => event.kind === 'egress_denied')
                        }
                      />
                    ) : null}
                    {usageFooter && lastVisibleMessageIndex < 0 && currentReplyId === undefined ? (
                      <div className="mt-4">{usageFooter}</div>
                    ) : null}
                  </section>
                </div>
              )}

              <div
                ref={scroll.composerRef}
                className="sticky bottom-0 z-10 -mx-2 mt-10 bg-gradient-to-t from-gray-50 via-gray-50/95 px-2 pb-2 pt-8 dark:from-gray-950 dark:via-gray-950/95"
              >
                {isArchived ? (
                  <div className="flex flex-wrap items-center justify-between gap-4 rounded-xl border border-gray-200 bg-white p-4 dark:border-white/10 dark:bg-gray-900">
                    <div className="min-w-0 flex-1 basis-64">
                      <p className="text-sm font-medium text-gray-900 dark:text-gray-100">
                        {t('sessionArchive.title')}
                      </p>
                      <p className="mt-1 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
                        {t(
                          contextLost
                            ? 'sessionArchive.contextLost'
                            : isActive
                              ? 'sessionArchive.running'
                              : 'sessionArchive.body',
                        )}
                      </p>
                    </div>
                    <button
                      type="button"
                      disabled={restoring}
                      onClick={() => void unarchive()}
                      className="rounded-lg bg-gray-900 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-gray-700 disabled:opacity-50 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white"
                    >
                      {t(
                        restoring
                          ? 'sessionArchive.restoring'
                          : contextLost
                            ? 'sessionArchive.restore'
                            : 'sessionArchive.continue',
                      )}
                    </button>
                  </div>
                ) : (
                  <form
                    onSubmit={(event) => void submitFollowUp(event)}
                    className="rounded-xl border border-gray-200/90 dark:border-white/10 bg-white dark:bg-gray-900 shadow-[0_16px_40px_-28px_rgba(17,24,39,0.55)] transition focus-within:border-crimson/30 focus-within:ring-4 focus-within:ring-crimson/[0.05]"
                  >
                    <textarea
                      rows={2}
                      value={followUp}
                      onChange={(event) => setFollowUp(event.target.value)}
                      disabled={contextLost}
                      onKeyDown={(event) => {
                        if (!isComposerSubmit(event)) return;
                        event.preventDefault();
                        // Through the form, so the keyboard and the button take the
                        // same path and cannot drift apart.
                        event.currentTarget.form?.requestSubmit();
                      }}
                      placeholder={
                        contextLost ? 'Unable to reconnect this chat' : 'Add a follow-up'
                      }
                      aria-label="Add a follow-up"
                      className="w-full resize-none rounded-t-xl border-0 bg-transparent px-4 pt-3 text-sm leading-relaxed text-gray-900 dark:text-gray-50 placeholder:text-gray-400 dark:placeholder:text-gray-500 focus:outline-none focus:ring-0"
                    />
                    <div className="flex items-center gap-1.5 px-3 pb-2.5">
                      {modelOptions.length > 0 ? (
                        <>
                          {/* The harness stays the thread's; the model is offered
                          again. A model change restarts this member's CLI and
                          native-resumes it in the same retained workspace; it
                          does not replace or clone the sandbox. */}
                          <span className="shrink-0 text-[11px] text-gray-400 dark:text-gray-500">
                            {contextLost
                              ? 'Start a new chat ·'
                              : job.state === 'cancelled'
                                ? `Restores any saved changes · ${job.runtime} ·`
                                : `Inherits ${job.runtime} ·`}
                          </span>
                          <Picker
                            label="Model for this turn"
                            value={model}
                            options={modelOptions}
                            onChange={setModel}
                          />
                          <Picker
                            label="Reasoning for this turn"
                            value={effort}
                            options={efforts}
                            onChange={setEffort}
                          />
                          {isActive ? (
                            <span className="min-w-0 truncate text-[11px] text-gray-400 dark:text-gray-500">
                              · queued after this run
                            </span>
                          ) : null}
                        </>
                      ) : (
                        <span className="min-w-0 flex-1 truncate text-[11px] text-gray-400 dark:text-gray-500">
                          {contextLost
                            ? `Start a new chat · ${job.runtime} · ${job.model}`
                            : job.state === 'cancelled'
                              ? `Restores any saved changes · ${job.runtime} · ${job.model}`
                              : `Inherits ${job.runtime} · ${job.model}`}
                          {isActive ? ' · queued after this run' : ''}
                        </span>
                      )}
                      <button
                        type="submit"
                        disabled={contextLost || !followUp.trim() || submitting}
                        className="ml-auto inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-crimson text-white shadow-sm transition-colors hover:bg-crimson-dark disabled:cursor-not-allowed disabled:opacity-40"
                        aria-label="Send follow-up"
                        title="Send follow-up — ⏎ to send, ⇧⏎ for a new line"
                      >
                        {submitting ? (
                          <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" />
                        ) : (
                          <svg
                            aria-hidden="true"
                            className="h-4 w-4"
                            fill="none"
                            viewBox="0 0 24 24"
                            stroke="currentColor"
                            strokeWidth={2}
                          >
                            <path
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              d="M12 19V5m0 0-6 6m6-6 6 6"
                            />
                          </svg>
                        )}
                      </button>
                    </div>
                  </form>
                )}
                <ThreadStatsBar job={job} />
                {actionError ? (
                  <p
                    className="mt-2 text-center text-xs text-red-600 dark:text-red-400"
                    role="alert"
                  >
                    {ownerFailureMessage(actionError)}
                  </p>
                ) : null}
              </div>
            </div>
          </div>

          {/* Above the transcript, below its composer and the page header:
              the design's order is body, rail preview, composer, header. */}
          {showRail ? (
            <div
              className="pointer-events-none absolute right-3 top-0 z-0 flex flex-col justify-center"
              style={{ bottom: composerHeight }}
            >
              <TurnRail
                entries={navigationEntries}
                activeKey={scroll.activeKey}
                onNavigate={jumpToTurn}
                maxHeightPx={Math.min(
                  RAIL_MAX_HEIGHT,
                  scroll.viewport.readingHeight - RAIL_BAND_MARGIN,
                )}
              />
            </div>
          ) : null}

          {tab === 'chat' ? (
            <div
              className="pointer-events-none absolute left-0 z-0 w-full max-w-full"
              style={{
                bottom: composerHeight + BACK_TO_BOTTOM_INSET,
                // Match the scroller's usable width, including when a native
                // scrollbar occupies space beside the conversation.
                width: scroll.viewport.width || undefined,
              }}
            >
              <div
                className={`mx-auto flex w-full max-w-4xl flex-col items-end gap-2 ${showRail ? 'pl-6 pr-12' : 'px-6'}`}
              >
                {/* One live region for both answers: where a jump landed, and
                  when it could not land at all. It is always in the DOM, since
                  a region added at the same moment as its text announces
                  nothing. */}
                <p
                  role="status"
                  aria-live="polite"
                  className={
                    scroll.unavailableKey === null
                      ? 'sr-only'
                      : 'rounded-full bg-white/95 px-3 py-1 text-xs text-gray-600 shadow-card dark:bg-gray-900/95 dark:text-gray-300'
                  }
                >
                  {navigationNotice}
                </p>
                {scroll.awayFromBottom ? (
                  <BackToBottom
                    hasNewContent={scroll.hasNewContent}
                    onReturn={scroll.returnToBottom}
                  />
                ) : null}
              </div>
            </div>
          ) : null}
        </div>

        {workspaceOpen ? (
          <PaneResizer
            pane={workspacePane}
            label="Resize workspace"
            controls="job-workspace-pane"
            className="hidden lg:block"
          />
        ) : null}

        <aside
          id="job-workspace-pane"
          role="region"
          aria-label="Job workspace"
          hidden={!workspaceOpen}
          style={{ '--job-workspace-width': `${workspacePane.width}px` } as CSSProperties}
          className={`${
            workspaceOpen ? 'flex' : 'hidden'
          } h-full min-h-0 min-w-0 flex-col overflow-hidden bg-white dark:bg-gray-900 lg:w-[var(--job-workspace-width)] lg:shrink-0`}
        >
          <nav
            aria-label="Workspace views"
            role="tablist"
            className="sticky top-0 z-10 flex gap-1 overflow-x-auto border-b border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-4 text-[13px] font-medium"
          >
            {WORKSPACE_TABS.map((tab) => (
              <button
                key={tab.key}
                id={`workspace-tab-${tab.key}`}
                type="button"
                role="tab"
                aria-controls={`workspace-panel-${tab.key}`}
                aria-selected={workspaceTab === tab.key}
                onClick={() => openWorkspace(tab.key)}
                className={`shrink-0 border-b-2 px-3 py-3 ${
                  workspaceTab === tab.key
                    ? 'border-gray-900 dark:border-gray-100 text-gray-900 dark:text-gray-50'
                    : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100'
                }`}
              >
                {tab.label}
                {tab.key === 'git' && job.diffFiles.length ? (
                  <span className="ml-1.5 rounded bg-gray-100 dark:bg-white/5 px-1.5 py-0.5 text-[10px] text-gray-500 dark:text-gray-400">
                    {job.diffFiles.length}
                  </span>
                ) : null}
              </button>
            ))}
          </nav>
          <div
            id="workspace-panel-git"
            role="tabpanel"
            aria-labelledby="workspace-tab-git"
            hidden={workspaceTab !== 'git'}
            className="min-h-0 flex-1 overflow-y-auto p-5"
          >
            <GitPanel job={job} active={workspaceOpen && workspaceTab === 'git'} />
          </div>
          <div
            id="workspace-panel-terminal"
            role="tabpanel"
            aria-labelledby="workspace-tab-terminal"
            hidden={workspaceTab !== 'terminal'}
            className="min-h-0 flex-1 p-3"
          >
            {isArchived ? (
              <p className="p-4 text-sm text-gray-500 dark:text-gray-400">
                {t('sessionArchive.terminal')}
              </p>
            ) : (
              <TerminalWorkspace
                key={job.id}
                jobId={job.id}
                active={workspaceOpen && workspaceTab === 'terminal'}
                ready={terminalWorkspaceReady}
              />
            )}
          </div>
          <div
            id="workspace-panel-files"
            role="tabpanel"
            aria-labelledby="workspace-tab-files"
            hidden={workspaceTab !== 'files'}
            className="min-h-0 flex-1 overflow-y-auto p-5"
          >
            <WorkspaceFiles job={job} active={workspaceOpen && workspaceTab === 'files'} />
          </div>
        </aside>

        {inspector ? (
          <PaneResizer
            pane={inspectorPane}
            label="Resize step details"
            controls="job-inspector-pane"
            className="hidden lg:block"
          />
        ) : null}

        <aside
          id="job-inspector-pane"
          role="region"
          aria-label={inspector?.kind === 'run' ? 'Run details' : 'Step details'}
          hidden={!inspector}
          style={{ '--job-inspector-width': `${inspectorPane.width}px` } as CSSProperties}
          className={`${
            inspector ? 'flex' : 'hidden'
          } h-full min-h-0 min-w-0 flex-col overflow-hidden border-l border-gray-200 bg-white dark:border-white/10 dark:bg-gray-900 lg:w-[var(--job-inspector-width)] lg:shrink-0`}
        >
          {inspector ? (
            <TrajectoryInspector
              selection={inspector}
              steps={trajectorySteps}
              events={events}
              jobs={threadJobs}
              currentJob={job}
              query={trajectoryQuery}
              onClearQuery={() => setTrajectoryQuery('')}
              onSelectTab={selectInspectorTab}
              onSelectStep={selectStep}
              onClose={closeInspector}
              onOpenGitDiff={() => openWorkspace('git')}
            />
          ) : null}
        </aside>
      </div>
    </section>
  );
}

'use client';

import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type FormEvent,
} from 'react';
import { useRouter } from 'next/navigation';
import Link from 'next/link';
import dynamic from 'next/dynamic';
import toast from 'react-hot-toast';

import { WorkspaceLinkProvider } from '@/components/ui/Markdown';
import { basePath } from '@/config/env';
import { useT } from '@/components/providers';
import {
  cancelAgentJob,
  forkAgentJob,
  getAgentConfig,
  getAgentJobGit,
  restoreAgentJob,
  type AgentConfigApi,
  type AgentGitWorkspaceApi,
} from '@/lib/api/agents';
import { submitThreadMessage, type ThreadMessageResultApi } from '@/lib/api/pendingMessages';
import { APIError } from '@/lib/utils/errors';

import { ownerFailureMessage, siteMisconfiguredReason, toDiffFileDetails } from './adapt';
import {
  conversationNavigationScope,
  describeCurrentTurn,
  describeHistoryTurns,
  messageBlockKey,
  sameNavigationEntries,
  toNavigationEntry,
  type ConversationTurnNavigationEntry,
} from './conversationTurnNavigation';
import { highestEffort, offeredEfforts } from './effort';
import { LedgerUsage, useLedgerUsage } from './LedgerUsage';
import { PaneResizer } from './PaneResizer';
import { Picker } from './Picker';
import { ComposerPlusMenu } from './ComposerPlusMenu';
import { ComposerCommandMenu } from './ComposerCommandMenu';
import { PromptRewrite } from './PromptRewrite';
import { composerCommandMatches } from './composerCommands';
import { GoalBar } from './goal/GoalBar';
import { isGoalRevisionConflict, writeGoal } from './goal/goalApi';
import { GOAL_COMMAND_PREFIX, useGoalCommand } from './goal/useGoalCommand';
import { hasLiveWork } from './goal/liveWork';
import { SkillSelectionDialog } from './skills/SkillPicker';
import { AttachmentStrip } from './attachments/AttachmentStrip';
import { useAttachments } from './attachments/useAttachments';
import { filesFromDataTransfer } from '@/lib/dataTransferFiles';
import { useThreadComposerMcp } from './mcp/useThreadComposerMcp';
import type { SelectedSkillApi } from '@/lib/api/skills';
import { BackToBottom, TurnRail } from './TurnNavigator';
import { ThreadStatsBar } from './ThreadStatsBar';
import { LiveAgentStatus } from './LiveAgentStatus';
import { ActivityIcon, ActivityWindow } from './ActivityWindow';
import { buildActivityOverview, hasActivityOverview } from './activityOverview';
import { ThreadActions } from './ThreadActions';
import { McpSessionPanel } from './mcp/McpSessionPanel';
import { NativeQuestionPrompt } from './NativeQuestionPrompt';
import type { AgentEvent, AgentJob } from './types';
import type { AgentJobApi, AgentJobEventApi, AgentThreadEventApi } from '@/lib/api/agents';
import { AttemptTurnFooter } from './AttemptTurnFooter';
import {
  normalizeStepTab,
  type InspectorSelection,
  type RunTab,
  type StepTab,
} from './trajectoryInspectorTabs';
import { buildTrajectorySteps, toolStepKey, turnContextFromJobs } from './trajectorySteps';
import { shouldHoldReading, useConversationScroll } from './useConversationScroll';
import { notDeliveredText, PendingMessageQueue } from './PendingMessageQueue';
import { isSteerChord } from './composerKeys';
import { usePendingMessages } from './usePendingMessages';
import { useResizablePane } from './useResizablePane';
import {
  composerModels,
  composerOverrides,
  ComposerTextarea,
  ComposerMessageActions,
} from './ConversationComposer';
import { useComposerDraft } from './useComposerDraft';
import { useTranscriptPresentation, TranscriptPresentationControl } from './transcriptPresentation';
import { TurnDeliverables } from './TurnDeliverables';
import { createRecordedOutputsSelector, type RecordedFileSelection } from './turnDeliverables';
import {
  hasLostContext,
  isActiveConversationState,
  isBusyJob,
  latestJob,
  ownerRunNote,
} from './conversationJob';
import { useFirstRun } from './firstRun';
import { codeReviewPrompt } from './codeReview';
import { watchPullRequestPrompt, watchPullRequestRequestId } from './watchPullRequest';
import { WorkspaceTabBar } from './WorkspaceTabBar';
import { useWorkspaceTabs, type WorkspaceTab } from './useWorkspaceTabs';
import {
  CopyIcon,
  RewindIcon,
  copyMessage,
  MessageActionButton,
  MessageActions,
  MessageContent,
  ThreadTurn,
  EventRow,
  EventList,
  HistoryTurn,
} from './ChatMessages';

function PanelLoading({ children }: { children: string }) {
  return (
    <p role="status" className="p-4 text-sm text-gray-500 dark:text-gray-400">
      {children}
    </p>
  );
}

// These panels are optional: loading a conversation should not also load its
// terminal, file browser, side chats, and execution inspectors. Each loading
// view also keeps its suspended import inside the panel in the App Router.
const TerminalWorkspace = dynamic(
  () => import('./TerminalWorkspace').then((module) => module.TerminalWorkspace),
  { loading: () => <PanelLoading>Loading terminal…</PanelLoading> },
);
const WorkspaceFiles = dynamic(
  () => import('./WorkspaceFiles').then((module) => module.WorkspaceFiles),
  { loading: () => <PanelLoading>Loading files…</PanelLoading> },
);
const TrajectoryView = dynamic(
  () => import('./TrajectoryView').then((module) => module.TrajectoryView),
  { loading: () => <PanelLoading>Loading trajectory…</PanelLoading> },
);
const TrajectoryInspector = dynamic(
  () => import('./TrajectoryInspector').then((module) => module.TrajectoryInspector),
  { loading: () => <PanelLoading>Loading details…</PanelLoading> },
);
const RecordedDiffPanel = dynamic(
  () => import('./RecordedDiffPanel').then((module) => module.RecordedDiffPanel),
  { loading: () => <PanelLoading>Loading changes…</PanelLoading> },
);
const SideChat = dynamic(() => import('./SideChat').then((module) => module.SideChat), {
  loading: () => <PanelLoading>Loading side chat…</PanelLoading>,
});

const EMPTY_STORED_EVENTS: (AgentJobEventApi | AgentThreadEventApi)[] = [];
const EMPTY_THREAD_JOBS: AgentJobApi[] = [];
const EMPTY_TRAJECTORY_STEPS: ReturnType<typeof buildTrajectorySteps> = [];

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

const DIFF_LINE_CLASS = {
  hunk: 'bg-gray-50 dark:bg-gray-950 py-1 text-gray-400 dark:text-gray-500',
  ctx: 'text-gray-600 dark:text-gray-400',
  add: 'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
  del: 'bg-red-50 dark:bg-red-500/10 text-red-700 dark:text-red-400',
} as const;

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
        {job.failureCause === 'repository_too_large'
          ? 'A follow-up would fail the same way. Ask an admin to raise the repository import limit, or use a smaller repository.'
          : 'Review the failure details, then send a follow-up to try again.'}
      </p>
    );
  }
  return null;
}

function ReviewButton({ onReview }: { onReview?: () => void }) {
  const t = useT();
  return (
    <button
      type="button"
      onClick={onReview}
      disabled={!onReview}
      title={t(onReview ? 'review.start' : 'review.unavailable')}
      className="rounded-md bg-white dark:bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-gray-700 dark:text-gray-300 shadow-sm ring-1 ring-inset ring-gray-200 dark:ring-white/10 hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40"
    >
      {t('review.tab')}
    </button>
  );
}

function WatchPullRequestButton({ onWatchPr }: { onWatchPr?: () => void }) {
  const t = useT();
  return (
    <button
      type="button"
      onClick={onWatchPr}
      disabled={!onWatchPr}
      title={t(onWatchPr ? 'watchPr.start' : 'watchPr.unavailable')}
      className="rounded-md bg-white dark:bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-gray-700 dark:text-gray-300 shadow-sm ring-1 ring-inset ring-gray-200 dark:ring-white/10 hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40"
    >
      {t('watchPr.action')}
    </button>
  );
}

function RunActions({
  job,
  onOpenDiff,
  onReview,
  onWatchPr,
  showChanges = job.diffFiles.length > 0,
}: {
  job: AgentJob;
  onOpenDiff: () => void;
  onReview?: () => void;
  onWatchPr?: () => void;
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
        <>
          <a
            href={job.prUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="rounded-md bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-[13px] font-medium text-white dark:text-gray-900 hover:bg-gray-800 dark:hover:bg-white"
          >
            Open draft PR
          </a>
          <ReviewButton onReview={onReview} />
          <WatchPullRequestButton onWatchPr={onWatchPr} />
        </>
      ) : null}
    </>
  );
}

function RunNotices({ job }: { job: AgentJob }) {
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
    </>
  );
}

/**
 * The first draft PR someone gets: where it is, that merging is theirs, and
 * that the conversation goes on. Said once; later turns get the plain actions.
 */
function FirstRunOutcome({
  job,
  onOpenDiff,
  onReview,
  onWatchPr,
}: {
  job: AgentJob;
  onOpenDiff: () => void;
  onReview?: () => void;
  onWatchPr?: () => void;
}) {
  const t = useT();
  return (
    <div className="mt-6 flex gap-3 rounded-xl border border-gray-200 dark:border-white/10 bg-gray-50 dark:bg-gray-950 px-4 py-3.5">
      <span
        aria-hidden="true"
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-emerald-50 text-emerald-600 ring-1 ring-inset ring-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-400 dark:ring-emerald-500/25"
      >
        <svg
          className="h-3.5 w-3.5"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2.5}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="m6 12 4 4 8-9" />
        </svg>
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium text-gray-900 dark:text-gray-50">
          {t('firstRun.doneTitle')}
        </p>
        <p className="mt-0.5 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
          {t('firstRun.doneBody')}
        </p>
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <RunActions job={job} onOpenDiff={onOpenDiff} onReview={onReview} onWatchPr={onWatchPr} />
        </div>
      </div>
    </div>
  );
}

/**
 * A turn the site's own setup made impossible. An alert rather than a run
 * outcome: the owner cannot fix it, and a follow-up fails the same way until
 * an admin finishes setting up the site, which the reason says.
 */
function SiteMisconfiguredAlert({
  job,
  note,
  onOpenDiff,
}: {
  job: AgentJob;
  note: string | undefined;
  onOpenDiff: () => void;
}) {
  return (
    <div
      role="alert"
      className="mt-6 rounded-xl border border-amber-200 dark:border-amber-500/25 bg-amber-50 dark:bg-amber-500/10 px-4 py-3"
    >
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-amber-900 dark:text-amber-200">
            Site misconfigured
          </p>
          {note ? (
            <p className="mt-0.5 text-xs text-amber-800 dark:text-amber-300 [overflow-wrap:anywhere]">
              {siteMisconfiguredReason(note)}
            </p>
          ) : null}
        </div>
        <RunActions job={job} onOpenDiff={onOpenDiff} />
      </div>
    </div>
  );
}

function OutcomeCard({
  job,
  onOpenDiff,
  onReview,
  onWatchPr,
  firstRun = false,
}: {
  job: AgentJob;
  onOpenDiff: () => void;
  onReview?: () => void;
  onWatchPr?: () => void;
  firstRun?: boolean;
}) {
  if (job.state === 'running' || job.state === 'queued') return null;
  const successful = job.state === 'done' || job.state === 'needs_review';
  if (successful) {
    const hasActions = job.diffFiles.length > 0 || Boolean(job.prUrl);
    if (!hasActions && !hasLostContext(job)) return null;
    if (firstRun && job.prUrl)
      return (
        <FirstRunOutcome
          job={job}
          onOpenDiff={onOpenDiff}
          onReview={onReview}
          onWatchPr={onWatchPr}
        />
      );
    return (
      <div className="mt-6">
        {hasActions ? (
          <div className="flex flex-wrap items-center gap-3">
            <RunActions
              job={job}
              onOpenDiff={onOpenDiff}
              onReview={onReview}
              onWatchPr={onWatchPr}
            />
          </div>
        ) : null}
        <RunGuidance job={job} />
      </div>
    );
  }
  const runNote = ownerRunNote(job);
  if (job.failureCause === 'site_misconfigured') {
    return <SiteMisconfiguredAlert job={job} note={runNote} onOpenDiff={onOpenDiff} />;
  }
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

function GitPanel({
  job,
  active,
  onReview,
  onWatchPr,
}: {
  job: AgentJob;
  active: boolean;
  onReview?: () => void;
  onWatchPr?: () => void;
}) {
  const [view, setView] = useState<GitView>('diff');
  const gitId = useId();
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

  return (
    <div className="agent-git-workspace min-w-0 space-y-4" aria-label="Git workspace">
      <header className="flex flex-wrap items-center gap-3 border-b border-gray-200 dark:border-white/10 pb-3">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium text-gray-800 dark:text-gray-200">
            {job.repo}
          </p>
          <p className="truncate font-mono text-xs text-gray-400 dark:text-gray-500">
            {live?.available ? live.branch || 'detached HEAD' : job.branch}
          </p>
        </div>
        {/* Silent while the workspace is still answering: a badge that appears
            only to vanish reads as a fault. The Refresh button carries the wait. */}
        {error || (!loading && !live?.available) ? (
          <span className="rounded bg-gray-100 dark:bg-white/5 px-2 py-1 text-[11px] text-gray-500 dark:text-gray-400">
            Unavailable
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
        aria-label="Git views"
      >
        {(['diff', 'review', 'commits'] as GitView[]).map((item, index, views) => (
          <button
            key={item}
            type="button"
            role="tab"
            id={`${gitId}-tab-${item}`}
            aria-controls={`${gitId}-panel-${item}`}
            aria-selected={view === item}
            tabIndex={view === item ? 0 : -1}
            onClick={() => setView(item)}
            onKeyDown={(event) => {
              const next =
                event.key === 'ArrowRight'
                  ? (index + 1) % views.length
                  : event.key === 'ArrowLeft'
                    ? (index + views.length - 1) % views.length
                    : event.key === 'Home'
                      ? 0
                      : event.key === 'End'
                        ? views.length - 1
                        : null;
              if (next === null) return;
              event.preventDefault();
              setView(views[next]);
              document.getElementById(`${gitId}-tab-${views[next]}`)?.focus();
            }}
            className={`ui-tab min-h-9 px-2 py-2 text-xs font-medium capitalize ${
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

      <div id={`${gitId}-panel-${view}`} role="tabpanel" aria-labelledby={`${gitId}-tab-${view}`}>
        {view === 'diff' && files.length ? (
          <section className="agent-git-diff overflow-hidden rounded-xl border border-gray-200 dark:border-white/10">
            <div className="agent-git-file-list border-b border-gray-200 dark:border-white/10 bg-gray-50/60 dark:bg-gray-900/60">
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
                    aria-pressed={selected?.path === file.path}
                    title={file.path}
                    className={`ui-focus-ring flex w-full items-center gap-3 border-b border-gray-100 dark:border-white/5 px-3 py-2.5 text-left text-xs last:border-b-0 ${
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
              <label className="agent-git-file-picker border-b border-border bg-surface-muted px-3 py-2 text-xs text-muted">
                <span>
                  {files.length} changed file{files.length === 1 ? '' : 's'}
                </span>
                <select
                  aria-label="Changed file"
                  value={selected?.path ?? ''}
                  onChange={(event) => setSelectedPath(event.target.value)}
                  className="ui-input mt-1 w-full min-w-0 py-2 font-mono text-xs"
                >
                  {files.map((file) => (
                    <option key={file.path} value={file.path}>
                      {file.path} (+{file.add} −{file.del})
                    </option>
                  ))}
                </select>
              </label>
              <div className="agent-git-selected-path border-b border-gray-100 dark:border-white/5 px-4 py-2.5 font-mono text-xs text-gray-600 dark:text-gray-400">
                {selected?.path}
              </div>
              <pre
                tabIndex={0}
                role="region"
                aria-label={`Diff for ${selected?.path ?? 'selected file'}`}
                className="ui-focus-ring max-h-[34rem] overflow-auto font-mono text-xs leading-relaxed"
              >
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
              <div className="flex flex-wrap items-center gap-3">
                <a
                  href={job.prUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex rounded-md bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-xs font-medium text-white dark:text-gray-900 hover:bg-gray-800 dark:hover:bg-white"
                >
                  Open pull request
                </a>
                <ReviewButton onReview={onReview} />
                <WatchPullRequestButton onWatchPr={onWatchPr} />
              </div>
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
    </div>
  );
}

export function JobDetail({
  job,
  userId,
  events = EMPTY_STORED_EVENTS,
  threadJobs = EMPTY_THREAD_JOBS,
  onReload,
}: {
  job: AgentJob;
  userId?: string;
  /** The stored stream, for the trajectory tab. Absent in fixtures. */
  events?: (AgentJobEventApi | AgentThreadEventApi)[];
  /** The conversation's turn rows, so Run details can describe a past turn. */
  threadJobs?: AgentJobApi[];
  onReload?: () => void;
}) {
  const router = useRouter();
  const t = useT();
  const usage = useLedgerUsage(job);
  const threadId = job.threadId ?? job.id;
  const workspaceScope = JSON.stringify([userId, threadId]);
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
  const [openWorkspaceScope, setOpenWorkspaceScope] = useState<string | null>(null);
  const workspaceOpen = openWorkspaceScope === workspaceScope;
  const setWorkspaceOpen = useCallback(
    (open: boolean) => setOpenWorkspaceScope(open ? workspaceScope : null),
    [workspaceScope],
  );
  const [initializedWorkspace, setInitializedWorkspace] = useState<{
    scope: string;
    tabs: ReadonlySet<WorkspaceTab>;
  } | null>(null);
  useEffect(() => {
    setOpenWorkspaceScope(null);
    setInitializedWorkspace(null);
  }, [workspaceScope]);
  const workspaceTriggerRef = useRef<HTMLElement | null>(null);
  const focusWorkspaceRef = useRef(false);
  const [activityOpen, setActivityOpen] = useState(true);
  const [wideLayout, setWideLayout] = useState(false);
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia('(min-width: 1024px)');
    const sync = () => setWideLayout(query.matches);
    sync();
    query.addEventListener('change', sync);
    return () => query.removeEventListener('change', sync);
  }, []);
  const activityToggleRef = useRef<HTMLButtonElement>(null);
  const workspaceTabs = useWorkspaceTabs(userId, job, threadJobs);
  const workspaceTab = workspaceTabs.selected;
  // Mount a panel only when first used, then retain it while hidden so closing
  // the workspace preserves terminals, file previews, and side chat drafts.
  const workspaceInitialized = workspaceOpen || initializedWorkspace?.scope === workspaceScope;
  const terminalInitialized =
    (workspaceOpen && workspaceTab === 'terminal') ||
    (initializedWorkspace?.scope === workspaceScope && initializedWorkspace.tabs.has('terminal'));
  const filesInitialized =
    (workspaceOpen && workspaceTab === 'files') ||
    (initializedWorkspace?.scope === workspaceScope && initializedWorkspace.tabs.has('files'));
  useEffect(() => {
    if (!workspaceOpen) return;
    setInitializedWorkspace((current) => {
      if (current?.scope === workspaceScope && current.tabs.has(workspaceTab)) return current;
      const tabs = new Set(current?.scope === workspaceScope ? current.tabs : []);
      tabs.add(workspaceTab);
      return { scope: workspaceScope, tabs };
    });
  }, [workspaceOpen, workspaceScope, workspaceTab]);
  // The workspace and the step inspector take turns in the same right region.
  const [inspector, setInspector] = useState<InspectorSelection | null>(null);
  const focusInspectorRef = useRef(false);
  const [trajectoryQuery, setTrajectoryQuery] = useState('');
  const [focusJobId, setFocusJobId] = useState<string | null>(null);
  const inspectorTriggerRef = useRef<HTMLElement | null>(null);
  const trajectoryContext = useMemo(() => {
    const context = turnContextFromJobs(threadJobs);
    if (
      !context.inputs?.some((input) => input.jobId === job.id) &&
      typeof job.prompt === 'string'
    ) {
      context.inputs = [
        ...(context.inputs ?? []),
        {
          jobId: job.id,
          turnNo: job.turnNo ?? 1,
          text: job.prompt,
          at: job.createdAt ?? null,
        },
      ];
    }
    return context;
  }, [threadJobs, job.id, job.prompt, job.turnNo, job.createdAt]);
  // The diagnostic projection can be large. Keep a getter for transcript
  // clicks, and construct it only while a reader uses the trajectory/details.
  const getTrajectorySteps = useMemo(() => {
    let steps: ReturnType<typeof buildTrajectorySteps> | undefined;
    return () => {
      steps ??= buildTrajectorySteps(events, trajectoryContext);
      return steps;
    };
  }, [events, trajectoryContext]);
  const trajectorySteps =
    tab === 'trajectory' || inspector !== null ? getTrajectorySteps() : EMPTY_TRAJECTORY_STEPS;
  const draft = useComposerDraft({ userId, threadId });
  const { value: followUp, setValue: setFollowUp } = draft;
  const currentDraftScope = useRef(draft.capture().scope);
  currentDraftScope.current = draft.capture().scope;
  const composerMounted = useRef(true);
  useEffect(() => {
    composerMounted.current = true;
    return () => {
      composerMounted.current = false;
    };
  }, []);
  const [presentation, setPresentation] = useTranscriptPresentation(userId, threadId);
  const [requestedFile, setRequestedFile] = useState<{
    scope: string;
    path: string;
    token: number;
  } | null>(null);
  const [recordedSelection, setRecordedFile] = useState<{
    scope: string;
    selection: RecordedFileSelection;
  } | null>(null);
  const recordedFile =
    recordedSelection?.scope === workspaceScope ? recordedSelection.selection : null;
  const recordedOutputsSelector = useMemo(() => createRecordedOutputsSelector(), []);
  const recordedOutputs = useMemo(
    () => recordedOutputsSelector.select(threadJobs, events),
    [recordedOutputsSelector, threadJobs, events],
  );
  const [chosenSkills, setChosenSkills] = useState<SelectedSkillApi[] | null>(null);
  // What to say when `/goal` was typed with nothing after it. Guidance, not a
  // failure: nothing was created and nothing was sent.
  const [goalHint, setGoalHint] = useState<string | null>(null);
  const followUpBox = useRef<HTMLTextAreaElement>(null);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [dropping, setDropping] = useState(false);
  const [readingFiles, setReadingFiles] = useState(false);
  // Follow-ups append to the thread's latest turn, even from an older URL.
  const inheritedSkills = threadJobs.reduce(
    (latest, turn) =>
      (turn.turn_no ?? 1) > latest.turnNo
        ? { turnNo: turn.turn_no ?? 1, skills: turn.skills ?? [] }
        : latest,
    { turnNo: job.turnNo ?? 1, skills: job.skills ?? [] },
  ).skills;
  // Unsent composer choices belong to the conversation, including when a PR
  // action advances it to another turn without sending the owner's draft.
  useEffect(() => setChosenSkills(null), [threadId]);
  // The same rule for the goal guidance. This component is deliberately not
  // remounted when the conversation moves to its next turn, so anything that
  // belongs to *one* message has to be reset explicitly when that turn does.
  useEffect(() => setGoalHint(null), [job.id]);
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  const attachments = useAttachments(t, config?.attachment_limits);
  const mcp = useThreadComposerMcp(job.threadId);
  const selectedSkills = chosenSkills ?? inheritedSkills;
  const mcpBlockedReason =
    config?.mcp_runtimes && !config.mcp_runtimes.includes(job.runtime)
      ? t('attach.mcpUnsupported', { runtime: job.runtime })
      : undefined;
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState(job.model);
  const [effort, setEffort] = useState(job.reasoningEffort ?? '');
  const [submitting, setSubmitting] = useState(false);
  const [rewriting, setRewriting] = useState(false);
  const sendRequestedRef = useRef(false);
  const watchPrRequestedRef = useRef(false);
  const [watchPrAcceptedJobId, setWatchPrAcceptedJobId] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const [forking, setForking] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  // The goal this conversation already has, as the page last read it. Every
  // goal write travels with this revision, so an edit made in another tab is
  // refused rather than silently overwritten.
  const currentGoal = job.goal ?? null;
  // The first turn of the owner's first conversation explains itself as it
  // goes, and says where its draft PR is when it has one.
  const { onlyThreadId } = useFirstRun();
  const firstRun = (job.turnNo ?? 1) === 1 && onlyThreadId === threadId;
  const goalCommand = useGoalCommand({
    value: followUp,
    setValue: setFollowUp,
    focusComposer: () => followUpBox.current?.focus(),
    onNeedsObjective: () => setGoalHint(t('goal.needsObjective')),
    onApply: applyGoalObjective,
    onError: setActionError,
  });
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
  // Measure every pane against the space remaining after the task sidebar.
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
    return composerModels(models, job.model);
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
  }, [job.id, setFollowUp]);

  const selectedAttempt = job.attempts.find((attempt) => attempt.no === attemptNo);
  const runNote = ownerRunNote(job);
  const isArchived = Boolean(job.archivedAt);
  const isActive = job.state === 'running' || job.state === 'queued';
  const isSuccessful = job.state === 'done' || job.state === 'needs_review';
  // `contextStatus` describes the selected attempt. `nativeState` is the
  // current membership truth, so it also closes the composer when someone is
  // viewing an older successful turn after a later admitted turn was lost.
  const contextLost = hasLostContext(job);
  const latestQuestionTurn = latestJob(
    threadJobs.filter((turn) => !turn.thread_id || turn.thread_id === threadId),
  );
  let questionTurn = {
    id: job.id,
    attemptId: job.currentAttemptId,
    active: job.state === 'running' && !contextLost,
  };
  if (latestQuestionTurn && (latestQuestionTurn.turn_no ?? 1) > (job.turnNo ?? 1)) {
    questionTurn = {
      id: latestQuestionTurn.id,
      attemptId: latestQuestionTurn.current_attempt_id,
      active:
        latestQuestionTurn.state === 'running' &&
        !latestQuestionTurn.cancel_requested &&
        latestQuestionTurn.native_state !== 'lost',
    };
  }
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
    setWatchPrAcceptedJobId(null);
  }, [job.id]);

  useEffect(() => {
    if (!isActive) {
      stopRequestedRef.current = false;
      setStopping(false);
    }
  }, [isActive]);

  function openWorkspace(tab: WorkspaceTab) {
    if (!workspaceOpen) {
      const active = document.activeElement;
      workspaceTriggerRef.current = active instanceof HTMLElement ? active : null;
    }
    focusWorkspaceRef.current = true;
    setRecordedFile(null);
    workspaceTabs.select(tab);
    setWorkspaceOpen(true);
    setInspector(null);
  }

  function openWorkspaceFile(path: string) {
    setRequestedFile((current) => ({
      scope: workspaceScope,
      path,
      token: (current?.token ?? 0) + 1,
    }));
    openWorkspace('files');
  }

  function openRecordedFile(selection: RecordedFileSelection) {
    openWorkspace('git');
    setRecordedFile({ scope: workspaceScope, selection });
  }

  const toggleWorkspace = useCallback(() => {
    if (workspaceOpen) {
      setWorkspaceOpen(false);
      const trigger = workspaceTriggerRef.current;
      workspaceTriggerRef.current = null;
      window.requestAnimationFrame(() => {
        if (trigger && document.contains(trigger) && !trigger.closest('[hidden]')) trigger.focus();
        else
          document
            .querySelector<HTMLButtonElement>('button[aria-controls="job-workspace-pane"]')
            ?.focus();
      });
    } else {
      const active = document.activeElement;
      workspaceTriggerRef.current = active instanceof HTMLElement ? active : null;
      focusWorkspaceRef.current = true;
      setInspector(null);
      setWorkspaceOpen(true);
    }
  }, [workspaceOpen, setWorkspaceOpen]);

  useEffect(() => {
    if (!workspaceOpen || !focusWorkspaceRef.current) return;
    focusWorkspaceRef.current = false;
    document.getElementById(`workspace-tab-${workspaceTab}`)?.focus();
  }, [workspaceOpen, workspaceTab, requestedFile?.token, recordedFile]);

  const closeInspector = useCallback(() => {
    setInspector(null);
    setFocusJobId(null);
    const trigger = inspectorTriggerRef.current;
    inspectorTriggerRef.current = null;
    // The transcript becomes visible in the next commit on narrow screens.
    window.requestAnimationFrame(() => {
      if (trigger && document.contains(trigger)) trigger.focus();
      else if (trigger) activityToggleRef.current?.focus();
    });
  }, []);

  // Remember what opened the panel so closing it can hand focus back.
  const rememberInspectorTrigger = useCallback(() => {
    const active = document.activeElement;
    if (!(active instanceof HTMLElement && active.closest('#job-inspector-pane'))) {
      inspectorTriggerRef.current = active instanceof HTMLElement ? active : null;
    }
    focusInspectorRef.current = true;
  }, []);

  useEffect(() => {
    if (!inspector || !focusInspectorRef.current) return;
    focusInspectorRef.current = false;
    document
      .querySelector<HTMLButtonElement>('#job-inspector-pane button[aria-label="Close details"]')
      ?.focus();
  }, [inspector]);

  const selectStep = useCallback(
    (stepKey: string) => {
      const step = getTrajectorySteps().find((candidate) => candidate.key === stepKey);
      rememberInspectorTrigger();
      setWorkspaceOpen(false);
      setInspector((current) => {
        const keepTab = current?.kind === 'step' && current.stepKey === stepKey;
        const tab: StepTab = keepTab ? current.tab : 'summary';
        return { kind: 'step', stepKey, tab: step ? normalizeStepTab(step, tab) : 'summary' };
      });
    },
    [rememberInspectorTrigger, setWorkspaceOpen, getTrajectorySteps],
  );

  const inspectTranscriptEvent = useCallback(
    (event: AgentEvent, eventJobId: string) => {
      const step = getTrajectorySteps().find(
        (candidate) =>
          candidate.jobId === eventJobId &&
          (candidate.sourceEvents.some((source) => source.id === event.sourceEventId) ||
            (event.kind === 'tool_use' &&
              event.id &&
              candidate.callId === event.id &&
              candidate.attemptId ===
                threadJobs.find((turn) => turn.id === eventJobId)?.current_attempt_id)),
      );
      if (step) selectStep(step.key);
    },
    [selectStep, getTrajectorySteps, threadJobs],
  );

  const openRun = useCallback(
    (jobId: string, turnNo: number) => {
      rememberInspectorTrigger();
      setWorkspaceOpen(false);
      setInspector({ kind: 'run', jobId, turnNo, tab: 'overview' });
      setTab('trajectory');
      setFocusJobId(jobId);
    },
    [rememberInspectorTrigger, setWorkspaceOpen],
  );

  const selectInspectorTab = useCallback((next: StepTab | RunTab) => {
    setInspector((current) => {
      if (!current) return current;
      return current.kind === 'step'
        ? { ...current, tab: next as StepTab }
        : { ...current, tab: next as RunTab };
    });
  }, []);

  // Returning to Chat closes details selected from the trajectory.
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
      const target = event.target instanceof Element ? event.target : null;
      if (target?.closest('[role="dialog"]')) return;
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

  /**
   * The plus menu's Goal row: put `/goal ` in the box and leave the caret after
   * it. Nothing opens, because there is nothing left to open — the band above
   * the messages is where a goal is read and managed, and this row is how the
   * sentence that sets one gets started.
   */
  function insertGoalCommand() {
    setGoalHint(null);
    setFollowUp((current) => {
      // Switching menus while typing a command completes its partial token.
      if (composerCommandMatches(current).some((entry) => entry.name === 'goal')) {
        return GOAL_COMMAND_PREFIX;
      }
      // Already the command: nothing to add, and prefixing again would make
      // `/goal /goal …` an objective nobody wrote.
      if (/^\s*\/goal(\s|$)/i.test(current)) return current;
      // Anything already typed *is* the objective — the row means "make this
      // message a goal", and an objective is a whole sentence.
      return current.trim() ? `${GOAL_COMMAND_PREFIX}${current}` : GOAL_COMMAND_PREFIX;
    });
    followUpBox.current?.focus();
  }

  /**
   * `/goal <objective>` on a conversation that already exists.
   *
   * The thread's own endpoint, not a message: the owner's set *is* the
   * authorization, and the platform schedules the first round from it, so
   * nothing has to be sent that says "begin". A refusal is thrown rather than
   * drawn here — the command reports it and keeps the text, because the retry
   * is the same command.
   */
  async function applyGoalObjective(objective: string): Promise<void> {
    try {
      // No round bound travels with it: the goal the owner asked for is
      // bounded by nothing. A bound the conversation already carries is a
      // choice somebody made elsewhere, and replacing the objective does not
      // silently restate it — nor silently drop it, which is why the field is
      // simply absent here rather than echoed back.
      await writeGoal(threadId, objective, currentGoal?.revision);
    } catch (cause) {
      // The revision this page held is the stale thing the server just
      // refused, so the conversation has to be read back before the owner
      // tries again. The refusal itself still travels: the command keeps the
      // text, and the retry is the same command.
      if (isGoalRevisionConflict(cause)) onReload?.();
      throw cause;
    }
    setGoalHint(null);
    setActionError(null);
    onReload?.();
  }

  async function submitFollowUp(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    await sendDraft();
  }

  /** Send what the composer holds; the result, or `null` if nothing went. */
  async function sendDraft(): Promise<ThreadMessageResultApi | null> {
    if (
      sendRequestedRef.current ||
      rewriting ||
      watchPrRequestedRef.current ||
      contextLost ||
      isArchived ||
      attachments.uploading ||
      readingFiles ||
      mcp.saving
    )
      return null;
    const snapshot = draft.capture();
    const prompt = snapshot.value.trim();
    if (!prompt) return null;
    sendRequestedRef.current = true;
    setSubmitting(true);
    setActionError(null);
    try {
      // Commands use their own operation and are never queued as messages.
      if (await goalCommand.submit()) return null;
      // One request whatever the conversation is doing: idle, the message is
      // the next turn when this returns; busy, it waits in the queue above the
      // composer. ``queue.apply`` shows the card or, for a turn, moves to it.
      const result = await submitThreadMessage(threadId, {
        prompt,
        ...(chosenSkills !== null ? { skill_ids: chosenSkills.map((skill) => skill.id) } : {}),
        ...(attachments.ids.length ? { attachment_ids: attachments.ids } : {}),
        // Omitted controls inherit the parent's validated model and effort.
        ...composerOverrides(job, model, effort),
      });
      draft.acknowledge(snapshot);
      if (!composerMounted.current || currentDraftScope.current !== snapshot.scope) return result;
      queue.apply(result);
      // The queued message retains its uploads; the next draft starts without them.
      setChosenSkills(null);
      attachments.reset();
      return result;
    } catch (cause: unknown) {
      if (!composerMounted.current || currentDraftScope.current !== snapshot.scope) return null;
      setActionError(cause instanceof Error ? cause.message : 'Could not send the message');
      return null;
    } finally {
      sendRequestedRef.current = false;
      if (composerMounted.current) setSubmitting(false);
    }
  }

  /**
   * Cmd/Ctrl+Enter while a turn that can take it is live: the draft goes into
   * that turn, before the model's next tool call, instead of after it. Only the
   * draft: what already waits was left for after the turn, and each has its
   * own ↑ for changing that.
   */
  async function steerNow() {
    const scope = draft.capture().scope;
    const drafted = followUp.trim() ? await sendDraft() : null;
    if (!composerMounted.current || currentDraftScope.current !== scope) return;
    // Nothing to add, or nothing running: the draft simply became the next turn.
    if (!drafted?.pending || (drafted.pending.attachments ?? []).length) return;
    const sent = await queue.sendNow(drafted.pending);
    if (!composerMounted.current || currentDraftScope.current !== scope) return;
    if (!sent.ok) {
      setActionError(sent.error);
      return;
    }
    const reason = sent.result.not_delivered_reason;
    if (reason) toast(notDeliveredText(reason, t));
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
  const canReview = Boolean(userId && canForkCurrent && !contextLost);
  function openReview() {
    if (!canReview) return;
    workspaceTabs.review(codeReviewPrompt(job, threadJobs));
    setWorkspaceOpen(true);
    setInspector(null);
  }
  const selectedAttemptIsCurrent = attemptNo === 0 || attemptNo === liveAttempt;
  const usageFooter = selectedAttemptIsCurrent ? (
    <LedgerUsage job={job} usage={usage} />
  ) : undefined;

  // The transcript and rail share descriptions. Composer edits cannot change
  // them; streaming updates rebuild only the description whose source changed.
  const currentAttemptNo = job.currentAttemptNo;
  const historicalRows = job.historyTurns;
  const historyTurns = useMemo(
    () => describeHistoryTurns({ historyTurns: historicalRows }),
    [historicalRows],
  );
  const currentTurn = useMemo(
    () => describeCurrentTurn(job, { attemptNo, currentAttemptNo }),
    [job, attemptNo, currentAttemptNo],
  );
  const threadMessages = currentTurn.messages;
  const visibleEvents = currentTurn.events;
  const activityOverview = useMemo(() => buildActivityOverview(visibleEvents), [visibleEvents]);
  const hasActivity = hasActivityOverview(activityOverview);
  const activityVisible = activityOpen && hasActivity && !workspaceOpen && inspector === null;
  const closeActivity = useCallback(() => {
    setActivityOpen(false);
    activityToggleRef.current?.focus();
  }, []);
  const toggleActivity = useCallback(() => {
    if (activityVisible) {
      closeActivity();
      return;
    }
    setWorkspaceOpen(false);
    setInspector(null);
    setFocusJobId(null);
    setActivityOpen(true);
  }, [activityVisible, closeActivity, setWorkspaceOpen]);
  const activitySources = useMemo(() => {
    const wanted = new Set(
      activityOverview.subagents.map((agent) => agent.sourceEvent.sourceEventId),
    );
    const sources = new Map<number, AgentJobEventApi | AgentThreadEventApi>();
    if (!wanted.size) return sources;
    for (const source of events) {
      if (wanted.has(source.id)) sources.set(source.id, source);
    }
    return sources;
  }, [activityOverview, events]);
  const activityStepKey = useCallback(
    (event: Extract<AgentEvent, { kind: 'tool_use' }>) => {
      const source =
        event.sourceEventId === undefined ? undefined : activitySources.get(event.sourceEventId);
      if (!source) return undefined;
      const sourceJobId =
        'job_id' in source
          ? source.job_id
          : trajectoryContext.inputs?.length === 1
            ? trajectoryContext.inputs[0].jobId
            : undefined;
      if (sourceJobId !== job.id) return undefined;
      // The adapted tool row already identifies its source call. Computing its
      // stable key does not need the full diagnostic projection during render.
      return toolStepKey({ ...source, job_id: sourceJobId }, event.id);
    },
    [activitySources, job.id, trajectoryContext],
  );
  useEffect(() => {
    if (!activityVisible) return;
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target instanceof Element ? event.target : null;
      if (
        event.key !== 'Escape' ||
        event.repeat ||
        event.defaultPrevented ||
        !target?.closest('#job-activity-window') ||
        target.closest('input, textarea, select, [contenteditable="true"], [role="dialog"]')
      )
        return;
      event.preventDefault();
      event.stopPropagation();
      closeActivity();
    };
    window.addEventListener('keydown', onKeyDown, true);
    return () => window.removeEventListener('keydown', onKeyDown, true);
  }, [activityVisible, closeActivity, setWorkspaceOpen]);
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
    active: tab === 'chat' && (wideLayout || !(workspaceOpen || inspector !== null)),
  });
  // The conversation's waiting messages. A message becomes a turn on the
  // server, when the turn in front of it ends; the page follows it there when
  // it asked for it, or when it was showing the turn the new one follows.
  const followTurn = useCallback(
    (jobId: string) => {
      // A router.push remounts the dynamic [jobId] segment even with
      // scroll:false and a stable JobDetail key. Native history updates Next's
      // usePathname without replacing the page, preserving the conversation
      // while useAgentJob loads the next turn. Reload and Back keep real URLs.
      window.history.pushState(null, '', `${basePath}/${encodeURIComponent(jobId)}`);
      // The reader asked for this turn, so following its output is what they
      // asked for — the one place besides "back to bottom" that resumes it.
      scroll.followLatest();
    },
    [scroll],
  );
  const viewedJobId = job.id;
  const queue = usePendingMessages({
    // The queue this page writes to is the one it reads.
    threadId,
    active: hasLiveWork(job),
    onSent: (jobId, how) => {
      if (jobId === viewedJobId) return;
      if (how.requested || how.after === viewedJobId) followTurn(jobId);
    },
  });
  const watchPrPrompt = watchPullRequestPrompt(job);
  // These live fields describe the entire conversation/workspace. Other
  // thread rows can retain older pending/running values after it settles.
  const publicationState =
    job.workspacePublicationState ?? latestQuestionTurn?.workspace_publication_state;
  const publicationPending = Boolean(publicationState && publicationState !== 'published');
  const conversationBusy =
    job.conversationState !== undefined
      ? isActiveConversationState(job.conversationState)
      : Boolean(latestQuestionTurn && isBusyJob(latestQuestionTurn));
  // An idle send resumes a paused queue oldest-first. This action must start
  // its own request, so wait until the queue is known to have no other work.
  const canWatchPr = Boolean(
    userId &&
    job.prUrl &&
    isSuccessful &&
    !isActive &&
    !isArchived &&
    !contextLost &&
    !submitting &&
    !rewriting &&
    !mcp.saving &&
    !publicationPending &&
    watchPrAcceptedJobId !== job.id &&
    queue.loaded &&
    queue.latestJobId &&
    ['succeeded', 'failed', 'cancelled'].includes(queue.latestJobState ?? '') &&
    queue.messages.length === 0 &&
    !queue.resuming &&
    !conversationBusy,
  );

  async function watchPr() {
    const latestJobId = queue.latestJobId;
    if (!canWatchPr || !latestJobId || watchPrRequestedRef.current || sendRequestedRef.current)
      return;
    const watchScope = currentDraftScope.current;
    watchPrRequestedRef.current = true;
    setSubmitting(true);
    setActionError(null);
    try {
      // This action sends its own message. Unsent text, uploads and composer
      // selections belong to the owner's next message and stay in place.
      // The server replays this receipt under the thread lock, including for
      // another tab viewing a different turn of the same conversation. Fresh
      // requests refuse atomically if its queue or published boundary changes.
      const result = await submitThreadMessage(
        threadId,
        { prompt: watchPrPrompt, require_idle: true, expected_latest_job_id: latestJobId },
        { requestId: watchPullRequestRequestId(threadId, latestJobId) },
      );
      if (!composerMounted.current || currentDraftScope.current !== watchScope) return;
      // Keep the completed source action closed while the next turn loads.
      if (result.job && !result.pending) setWatchPrAcceptedJobId(job.id);
      queue.apply(result);
    } catch (cause: unknown) {
      if (!composerMounted.current || currentDraftScope.current !== watchScope) return;
      if (cause instanceof APIError && cause.code === 'conversation_changed') void queue.refresh();
      setActionError(cause instanceof Error ? cause.message : t('watchPr.sendFailed'));
    } finally {
      watchPrRequestedRef.current = false;
      if (composerMounted.current) setSubmitting(false);
    }
  }

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
    !activityVisible &&
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
  // The workspace and inspector share the right-hand region.
  const panelOpen = workspaceOpen || inspector !== null;
  const content = (
    <section className="flex h-full min-h-0 flex-col bg-surface">
      <header className="agent-conversation-header relative z-20 shrink-0 border-b border-border bg-surface px-4 sm:px-6">
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
        <div className="agent-conversation-toolbar flex flex-col">
          <div className="agent-conversation-context flex min-w-0 items-center gap-3 pb-2 pt-3">
            <div className="min-w-0 flex-1">
              <h1
                title={title}
                className="truncate text-base font-semibold tracking-[-0.01em] text-foreground"
              >
                {title}
              </h1>
              <div className="mt-1 flex items-center gap-2 text-xs text-muted">
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
          </div>
          <div className="agent-conversation-controls flex flex-wrap items-center gap-x-3 gap-y-2 pb-3">
            <nav
              aria-label="Run views"
              className="flex shrink-0 items-center gap-1 rounded-xl bg-surface-muted p-1 text-xs"
            >
              {(['chat', 'trajectory'] as const).map((view) => (
                <button
                  key={view}
                  type="button"
                  onClick={() => setTab(view)}
                  aria-current={tab === view ? 'page' : undefined}
                  className={`ui-focus-ring min-h-9 rounded-lg px-3 py-1.5 capitalize transition-colors [@media(pointer:coarse)]:min-h-11 ${
                    tab === view
                      ? 'bg-surface font-semibold text-foreground shadow-sm ring-1 ring-border'
                      : 'text-muted hover:bg-surface-hover hover:text-foreground'
                  }`}
                >
                  {view}
                </button>
              ))}
            </nav>
            {tab === 'chat' ? (
              <TranscriptPresentationControl
                value={presentation}
                onChange={(mode) => {
                  scroll.holdReading();
                  setPresentation(mode);
                }}
              />
            ) : null}
            <div className="ml-auto flex max-w-full flex-wrap items-center gap-2">
              {hasActivity ? (
                <button
                  ref={activityToggleRef}
                  type="button"
                  onClick={toggleActivity}
                  aria-label={
                    activityVisible ? 'Hide tasks and subagents' : 'Show tasks and subagents'
                  }
                  aria-expanded={activityVisible}
                  aria-controls="job-activity-window"
                  title="Tasks and subagents"
                  className={`ui-focus-ring inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border transition-colors [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11 ${
                    activityVisible
                      ? 'border-gray-300 bg-gray-100 text-gray-900 dark:border-white/20 dark:bg-white/10 dark:text-gray-100'
                      : 'border-gray-200 bg-white text-gray-500 hover:bg-gray-100 dark:border-white/10 dark:bg-gray-900 dark:text-gray-400 dark:hover:bg-white/5'
                  }`}
                >
                  <ActivityIcon />
                </button>
              ) : null}
              {job.diffFiles.length ? (
                <button
                  type="button"
                  onClick={() => openWorkspace('git')}
                  className="ui-focus-ring hidden min-h-9 items-center rounded-lg px-2.5 py-1.5 text-[13px] font-medium text-gray-600 dark:text-gray-400 transition-colors hover:bg-gray-100 dark:hover:bg-white/10 hover:text-gray-900 dark:hover:text-gray-100 sm:inline-flex [@media(pointer:coarse)]:min-h-11"
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
                  className="ui-focus-ring inline-flex min-h-9 items-center gap-1.5 rounded-lg bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-[13px] font-medium text-white dark:text-gray-900 transition-colors hover:bg-gray-800 dark:hover:bg-white disabled:opacity-60 [@media(pointer:coarse)]:min-h-11"
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
              <button
                type="button"
                onClick={toggleWorkspace}
                aria-label={workspaceOpen ? 'Close workspace' : 'Open workspace'}
                aria-expanded={workspaceOpen}
                aria-controls="job-workspace-pane"
                aria-keyshortcuts="Meta+J Control+J"
                title={`${workspaceOpen ? 'Close workspace' : 'Open workspace'} (⌘J / Ctrl+J)`}
                className={`ui-focus-ring inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg border transition-colors [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11 ${
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
            </div>
          </div>
        </div>
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
            className={`min-w-0 flex-1 flex-col overflow-y-auto overscroll-y-none [overflow-anchor:none] focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-crimson/40 ${
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
                    jobs={threadJobs}
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
                        presentation={presentation}
                        onInspectEvent={events.length ? inspectTranscriptEvent : undefined}
                        results={
                          <TurnDeliverables
                            job={{ id: turn.jobId, state: turn.state }}
                            outputs={recordedOutputs.get(turn.jobId)}
                            onOpenFile={openRecordedFile}
                          />
                        }
                        showActivityDetails={false}
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
                      // Only a prompt starts a turn a rewind could replace.
                      const rewindAnchor =
                        message.role === 'user' &&
                        !message.midTurn &&
                        currentRewindAnchor !== message.jobId
                          ? currentRewindAnchor
                          : undefined;
                      return (
                        <ThreadTurn
                          key={message.id}
                          message={message}
                          goalTurn={currentTurn.goalTurn}
                          presentation={presentation}
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
                        <MessageContent
                          text={job.prompt || job.title}
                          presentation={presentation}
                        />
                        {presentation !== 'compact' || (currentRewindAnchor && canForkWorkspace) ? (
                          <MessageActions align="end">
                            {presentation !== 'compact' ? (
                              <MessageActionButton
                                label="Copy message"
                                onClick={() => void copyMessage(job.prompt || job.title)}
                              >
                                <CopyIcon />
                                Copy
                              </MessageActionButton>
                            ) : null}
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
                        ) : null}
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
                      <EventList
                        events={visibleEvents}
                        presentation={presentation}
                        completed={job.state === 'done' || job.state === 'needs_review'}
                        onInspectEvent={events.length ? inspectTranscriptEvent : undefined}
                        showActivityDetails={false}
                        jobId={job.id}
                        renderEvent={(event, index, blockKey) => (
                          <EventRow
                            event={event}
                            presentation={presentation}
                            jobId={job.id}
                            onInspectEvent={events.length ? inspectTranscriptEvent : undefined}
                            blockKey={blockKey}
                            busy={forking}
                            firstRun={firstRun}
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
                        )}
                      />
                      {job.state === 'queued' && visibleEvents.length === 0 ? (
                        <div className="flex items-center gap-2.5 py-3 text-[13px] text-gray-500 dark:text-gray-400">
                          <span className="h-2 w-2 animate-pulse rounded-full bg-gray-400 dark:bg-gray-600" />
                          Waiting to start
                        </div>
                      ) : null}
                    </div>

                    <AttemptTurnFooter job={job} attemptNo={attemptNo} />
                    {selectedAttemptIsCurrent ? (
                      <TurnDeliverables
                        job={job}
                        outputs={recordedOutputs.get(job.id)}
                        onOpenFile={openRecordedFile}
                      />
                    ) : null}

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

                    <OutcomeCard
                      job={job}
                      firstRun={firstRun}
                      onOpenDiff={() => openWorkspace('git')}
                      onReview={canReview ? openReview : undefined}
                      onWatchPr={canWatchPr ? () => void watchPr() : undefined}
                    />
                    {selectedAttemptIsCurrent ? <RunNotices job={job} /> : null}
                    {usageFooter && lastVisibleMessageIndex < 0 && currentReplyId === undefined ? (
                      <div className="mt-4">{usageFooter}</div>
                    ) : null}
                  </section>
                </div>
              )}

              {/* Keep the sticky controls opaque while the transcript scrolls behind them. */}
              <div
                ref={scroll.composerRef}
                className="sticky bottom-0 z-10 -mx-2 mt-4 bg-gray-50 px-2 pb-2 pt-3 dark:bg-gray-950"
              >
                <LiveAgentStatus job={job} />
                {/* `/goal` on its own is not an objective. It is consumed
                    rather than sent to the model, and this is where the owner
                    is told what is missing — beside the box the token is still
                    sitting in. */}
                {goalHint ? (
                  <p className="mb-2 text-[11px] text-gray-500 dark:text-gray-400" role="status">
                    {goalHint}
                  </p>
                ) : null}
                <NativeQuestionPrompt
                  threadId={threadId}
                  jobId={questionTurn.id}
                  attemptId={questionTurn.attemptId}
                  active={questionTurn.active && !isArchived}
                />
                <McpSessionPanel
                  jobId={job.id}
                  threadId={job.threadId ?? job.id}
                  active={isActive}
                  approvalsOnly
                />
                {/* Its own card above the composer rather than inside it: the
                    queue is what the conversation will send next, not part of
                    the message being written. */}
                <PendingMessageQueue queue={queue} disabled={isArchived || contextLost} />
                <div
                  className={
                    isArchived
                      ? undefined
                      : `ui-composer ${dropping ? 'border-accent/40 ring-4 ring-accent/[0.08]' : ''}`
                  }
                >
                  {job.goal ? (
                    <GoalBar
                      attached={!isArchived}
                      onCleared={() => followUpBox.current?.focus()}
                      threadId={threadId}
                      goal={job.goal}
                      limits={config?.goal_limits}
                      state={{
                        live: hasLiveWork(job),
                        repairing: job.goalTurn?.kind === 'repair' && isActive,
                      }}
                      onChanged={() => {
                        // The goal belongs to the conversation, so the page's own
                        // reload is what re-reads it. Reimplementing
                        // fetch-and-adapt here would be a second reader of the
                        // same endpoint, and the two would drift.
                        onReload?.();
                      }}
                    />
                  ) : null}
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
                      onDragOver={(event) => {
                        if (
                          contextLost ||
                          submitting ||
                          !event.dataTransfer.types.includes('Files')
                        )
                          return;
                        event.preventDefault();
                        setDropping(true);
                      }}
                      onDragLeave={(event) => {
                        if (event.currentTarget.contains(event.relatedTarget as Node | null))
                          return;
                        setDropping(false);
                      }}
                      onDrop={(event) => {
                        event.preventDefault();
                        setDropping(false);
                        if (contextLost || submitting) return;
                        setReadingFiles(true);
                        void filesFromDataTransfer(event.dataTransfer)
                          .then(attachments.add)
                          .catch((cause: unknown) => {
                            setActionError(
                              cause instanceof Error
                                ? cause.message
                                : 'Could not read dropped files',
                            );
                          })
                          .finally(() => setReadingFiles(false));
                      }}
                    >
                      <ComposerCommandMenu
                        id={goalCommand.menuId}
                        entries={goalCommand.entries}
                        activeIndex={goalCommand.activeIndex}
                        onHighlight={goalCommand.onHighlight}
                        onChoose={goalCommand.onChoose}
                      />
                      <ComposerTextarea
                        textareaRef={followUpBox}
                        rows={2}
                        value={followUp}
                        onPaste={(event) => {
                          if (contextLost || submitting) return;
                          attachments.onPaste(event);
                        }}
                        onChange={(event) => {
                          setFollowUp(event.target.value);
                          if (goalHint) setGoalHint(null);
                        }}
                        // The menu owns the keys while it is showing: Enter
                        // confirms the highlighted command rather than sending
                        // half of one to the model.
                        onKeyDown={(event) => {
                          goalCommand.onKeyDown(event);
                          if (event.defaultPrevented) return;
                          if (queue.canSendNow && isSteerChord(event)) {
                            event.preventDefault();
                            void steerNow();
                          }
                        }}
                        // The list is this box's own popup, and the highlighted
                        // row is named to it — the caret never leaves here.
                        // `aria-expanded` is deliberately not used: the textbox
                        // role does not support it, and `aria-controls` pointing
                        // at a list that is only in the DOM while it is open says
                        // the same thing without lying.
                        aria-autocomplete="list"
                        aria-controls={goalCommand.open ? goalCommand.menuId : undefined}
                        aria-activedescendant={goalCommand.activeId}
                        disabled={contextLost}
                        placeholder={
                          contextLost
                            ? 'Unable to reconnect this chat'
                            : 'Message or run a task, / commands, @ files or sessions'
                        }
                        aria-label="Add a follow-up"
                        className="block max-h-[30vh] min-h-16 w-full resize-y rounded-t-2xl border-0 bg-transparent px-4 py-3 text-sm leading-6 text-foreground placeholder:text-muted focus:outline-none focus:ring-0"
                      />
                      <AttachmentStrip
                        entries={attachments.entries}
                        onRemove={attachments.remove}
                        disabled={contextLost || submitting}
                      />
                      <div className="flex flex-wrap items-center gap-1 border-t border-border px-3 py-2">
                        <ComposerPlusMenu
                          onFiles={attachments.add}
                          onOpenSkills={() => setSkillsOpen(true)}
                          skillSummary={String(selectedSkills.length)}
                          goalSummary={t('commands.goalHint')}
                          onOpenGoal={insertGoalCommand}
                          mcp={mcp}
                          onOpen={() => {
                            goalCommand.dismiss();
                            mcp.reload();
                          }}
                          mcpBlockedReason={mcpBlockedReason}
                          disabled={contextLost || submitting}
                        />
                        <span aria-hidden="true" className="mx-1 h-5 w-px bg-border" />
                        {modelOptions.length > 0 ? (
                          <>
                            {/* The harness stays the thread's; the model is offered
                          again. A model change restarts this member's CLI and
                          native-resumes it in the same retained workspace; it
                          does not replace or clone the sandbox. */}
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
                          </>
                        ) : null}
                        <PromptRewrite
                          key={threadId}
                          compact
                          inline
                          value={followUp}
                          model={model}
                          disabled={
                            contextLost || submitting || goalCommand.busy || /^\s*\//.test(followUp)
                          }
                          onActiveChange={setRewriting}
                          onApply={(prompt) => {
                            setFollowUp(prompt);
                            followUpBox.current?.focus();
                          }}
                        />
                        <ComposerMessageActions
                          label={t(isActive ? 'composer.queue' : 'composer.send')}
                          title={t(isActive ? 'composer.queueHint' : 'composer.sendHint')}
                          steerLabel={t('composer.steer')}
                          steerTitle={t('composer.steerHint')}
                          onSteer={
                            queue.canSendNow && attachments.entries.length === 0
                              ? () => void steerNow()
                              : undefined
                          }
                          disabled={
                            contextLost ||
                            !followUp.trim() ||
                            submitting ||
                            rewriting ||
                            attachments.uploading ||
                            readingFiles ||
                            mcp.saving ||
                            goalCommand.busy
                          }
                          busy={submitting || goalCommand.busy}
                        />
                      </div>
                      <p className="px-4 pb-3 text-[11px] leading-4 text-muted">
                        {contextLost
                          ? `Start a new chat · ${job.runtime}`
                          : job.state === 'cancelled'
                            ? `Restores any saved changes · ${job.runtime}`
                            : `Inherits ${job.runtime}`}
                        {modelOptions.length === 0 ? ` · ${job.model}` : ''}
                        {isActive ? ` · ${t('queue.afterThisTurn')}` : ''}
                        {queue.canSendNow ? ` · ${t('queue.steerHint')}` : ''}
                      </p>
                    </form>
                  )}
                </div>
                {skillsOpen ? (
                  <SkillSelectionDialog
                    limits={config?.skill_limits}
                    selected={selectedSkills}
                    onClose={() => setSkillsOpen(false)}
                    onApply={(skills) => {
                      const originalIds = new Set(selectedSkills.map((skill) => skill.id));
                      if (
                        skills.length !== originalIds.size ||
                        skills.some((skill) => !originalIds.has(skill.id))
                      ) {
                        setChosenSkills(skills);
                      }
                      setSkillsOpen(false);
                    }}
                  />
                ) : null}
                <ThreadStatsBar job={job} />
                {/* A goal write's refusal is drawn inside the editor that made
                    it, beside the words it refused; this line is the composer's
                    own failures, including a command's. */}
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

          {activityVisible ? (
            <div
              className="pointer-events-none absolute inset-x-0 top-0 z-10 flex justify-end px-4 pt-4 sm:px-6"
              style={{ bottom: composerHeight + 64 }}
            >
              <aside
                id="job-activity-window"
                role="region"
                aria-label="Tasks and subagents"
                className="pointer-events-auto flex max-h-full w-full max-w-sm flex-col overflow-hidden rounded-xl bg-white/95 shadow-xl ring-1 ring-gray-200/90 backdrop-blur-sm dark:bg-gray-900/95 dark:ring-white/10"
              >
                <ActivityWindow
                  overview={activityOverview}
                  turnNo={currentTurn.displayNo}
                  attemptNo={attemptNo}
                  onClose={closeActivity}
                  stepKeyForEvent={activityStepKey}
                  onSelectStep={(stepKey) => {
                    setTab('trajectory');
                    selectStep(stepKey);
                  }}
                />
              </aside>
            </div>
          ) : null}

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
          key={workspaceScope}
          id="job-workspace-pane"
          role="region"
          aria-label="Job workspace"
          hidden={!workspaceOpen}
          style={{ '--job-workspace-width': `${workspacePane.width}px` } as CSSProperties}
          className={`${
            workspaceOpen ? 'flex' : 'hidden'
          } h-full min-h-0 min-w-0 flex-col overflow-hidden bg-white dark:bg-gray-900 lg:w-[var(--job-workspace-width)] lg:shrink-0`}
        >
          {workspaceInitialized ? (
            <>
              <WorkspaceTabBar
                key={`${userId}:${job.threadId ?? job.id}`}
                selected={workspaceTab}
                chats={workspaceTabs.chats}
                diffCount={job.diffFiles.length}
                onSelect={openWorkspace}
                onAdd={workspaceTabs.add}
                onClose={workspaceTabs.close}
                onBack={toggleWorkspace}
              />
              <div
                id="workspace-panel-git"
                role="tabpanel"
                aria-labelledby="workspace-tab-git"
                hidden={workspaceTab !== 'git'}
                className="min-h-0 flex-1 overflow-y-auto p-5"
              >
                {recordedFile ? (
                  <RecordedDiffPanel
                    selection={recordedFile}
                    turnNo={
                      threadJobs.find((turn) => turn.id === recordedFile.jobId)?.turn_no ??
                      job.turnNo ??
                      1
                    }
                    onClose={() => setRecordedFile(null)}
                  />
                ) : (
                  <GitPanel
                    job={job}
                    active={workspaceOpen && workspaceTab === 'git'}
                    onReview={canReview ? openReview : undefined}
                    onWatchPr={canWatchPr ? () => void watchPr() : undefined}
                  />
                )}
              </div>
              <div
                id="workspace-panel-terminal"
                role="tabpanel"
                aria-labelledby="workspace-tab-terminal"
                hidden={workspaceTab !== 'terminal'}
                className="min-h-0 flex-1 p-3"
              >
                {terminalInitialized ? (
                  isArchived ? (
                    <p className="p-4 text-sm text-gray-500 dark:text-gray-400">
                      {t('sessionArchive.terminal')}
                    </p>
                  ) : (
                    // Keyed by the chat, not the turn: every turn shares the one
                    // workspace, and a new turn must not tear its terminals down.
                    <TerminalWorkspace
                      key={workspaceScope}
                      jobId={job.id}
                      workspaceKey={workspaceScope}
                      active={workspaceOpen && workspaceTab === 'terminal'}
                      ready={terminalWorkspaceReady}
                    />
                  )
                ) : null}
              </div>
              <div
                id="workspace-panel-files"
                role="tabpanel"
                aria-labelledby="workspace-tab-files"
                hidden={workspaceTab !== 'files'}
                className="min-h-0 flex-1 overflow-y-auto p-5"
              >
                {filesInitialized ? (
                  <WorkspaceFiles
                    key={workspaceScope}
                    job={job}
                    active={workspaceOpen && workspaceTab === 'files'}
                    requestedPath={
                      requestedFile?.scope === workspaceScope ? requestedFile.path : undefined
                    }
                    requestToken={
                      requestedFile?.scope === workspaceScope ? requestedFile.token : undefined
                    }
                  />
                ) : null}
              </div>
              {workspaceTabs.chats.map((chat) => (
                <div
                  key={chat.id}
                  id={`workspace-panel-${chat.id}`}
                  data-side-chat-id={chat.id}
                  role="tabpanel"
                  aria-labelledby={`workspace-tab-${chat.id}`}
                  hidden={workspaceTab !== chat.id}
                  className="min-h-0 flex-1"
                >
                  <SideChat
                    tab={chat}
                    sourceJob={job}
                    sourceJobs={threadJobs}
                    userId={userId}
                    config={config}
                    active={workspaceOpen && workspaceTab === chat.id}
                  />
                </div>
              ))}
            </>
          ) : null}
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
  return <WorkspaceLinkProvider onOpenFile={openWorkspaceFile}>{content}</WorkspaceLinkProvider>;
}

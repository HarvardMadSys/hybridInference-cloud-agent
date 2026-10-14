'use client';

import { isTemporaryWorkspace } from '@/lib/temporaryWorkspace';
import { WorkspaceDownloadButton } from '../TemporaryWorkspace';
import { useT } from '@/components/providers';
import Link from 'next/link';
import { siteMisconfiguredReason } from '../adapt';
import { hasLostContext, ownerRunNote } from '../conversationJob';
import type { AgentJob } from '../types';

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

export function ReviewButton({ onReview }: { onReview?: () => void }) {
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

export function WatchPullRequestButton({ onWatchPr }: { onWatchPr?: () => void }) {
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
      {/* A temporary workspace publishes nothing: its files leave as a download. */}
      {isTemporaryWorkspace(job.repo) ? <WorkspaceDownloadButton jobId={job.id} /> : null}
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

export function RunNotices({ job }: { job: AgentJob }) {
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

export function OutcomeCard({
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
    const hasActions =
      job.diffFiles.length > 0 || Boolean(job.prUrl) || isTemporaryWorkspace(job.repo);
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

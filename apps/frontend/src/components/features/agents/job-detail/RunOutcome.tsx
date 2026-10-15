'use client';

import { isTemporaryWorkspace } from '@/lib/temporaryWorkspace';
import { WorkspaceDownloadButton } from '../TemporaryWorkspace';
import { useT } from '@/components/providers';
import Link from 'next/link';
import { siteMisconfiguredReason } from '../adapt';
import { hasLostContext, ownerRunNote } from '../conversationJob';
import type { AgentJob } from '../types';
import { isActiveWorkspacePublicationState } from '../useAgentJobs';

/** The pull request to show for this turn: its own, or the workspace's latest. */
export function turnPullRequestUrl(job: AgentJob): string | undefined {
  return job.prUrl ?? job.workspacePrUrl;
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

/**
 * Create PR, then Update PR: the platform publishes the workspace only when its
 * owner asks. Shown, disabled, while a publication is on its way.
 */
export function PublishButton({ job, onPublish }: { job: AgentJob; onPublish?: () => void }) {
  const t = useT();
  const update = Boolean(job.workspacePrUrl);
  const publishing = isActiveWorkspacePublicationState(job.workspacePublicationState);
  return (
    <button
      type="button"
      onClick={onPublish}
      disabled={!onPublish || publishing}
      title={t(
        publishing
          ? 'publish.inProgress'
          : !onPublish
            ? 'publish.unavailable'
            : update
              ? 'publish.updateHint'
              : 'publish.createHint',
      )}
      className={
        update
          ? 'rounded-md bg-white dark:bg-gray-900 px-3 py-1.5 text-[13px] font-medium text-gray-700 dark:text-gray-300 shadow-sm ring-1 ring-inset ring-gray-200 dark:ring-white/10 hover:bg-gray-50 dark:hover:bg-white/5 disabled:opacity-40'
          : 'rounded-md bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-[13px] font-medium text-white dark:text-gray-900 hover:bg-gray-800 dark:hover:bg-white disabled:opacity-40'
      }
    >
      {t(update ? 'publish.update' : 'publish.create')}
    </button>
  );
}

/** Whether this turn offers the publish control: its owner can, or one is on its way. */
function offersPublication(job: AgentJob, onPublish?: () => void): boolean {
  return (
    Boolean(job.repo) &&
    !isTemporaryWorkspace(job.repo) &&
    (Boolean(onPublish) || isActiveWorkspacePublicationState(job.workspacePublicationState))
  );
}

function RunActions({
  job,
  onOpenDiff,
  onReview,
  onWatchPr,
  onPublish,
  showChanges = job.diffFiles.length > 0,
}: {
  job: AgentJob;
  onOpenDiff: () => void;
  onReview?: () => void;
  onWatchPr?: () => void;
  onPublish?: () => void;
  showChanges?: boolean;
}) {
  const prUrl = turnPullRequestUrl(job);
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
      {offersPublication(job, onPublish) ? <PublishButton job={job} onPublish={onPublish} /> : null}
      {prUrl ? (
        <>
          <a
            href={prUrl}
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

/** Where the workspace's publication stands; nothing is published until its owner asks. */
export function RunNotices({ job }: { job: AgentJob }) {
  const t = useT();
  const state = job.workspacePublicationState;
  const publicationNote = isActiveWorkspacePublicationState(state)
    ? t('publish.inProgress')
    : state === 'failed'
      ? t('publish.failed')
      : job.workspaceUnpublishedChanges
        ? t('publish.unpublished')
        : state === 'published' && job.workspacePublicationUnchanged
          ? t('publish.unchanged')
          : null;
  return publicationNote ? (
    <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">{publicationNote}</p>
  ) : null;
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
  onPublish,
}: {
  job: AgentJob;
  onOpenDiff: () => void;
  onReview?: () => void;
  onWatchPr?: () => void;
  onPublish?: () => void;
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
          <RunActions
            job={job}
            onOpenDiff={onOpenDiff}
            onReview={onReview}
            onWatchPr={onWatchPr}
            onPublish={onPublish}
          />
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
  onPublish,
  firstRun = false,
}: {
  job: AgentJob;
  onOpenDiff: () => void;
  onReview?: () => void;
  onWatchPr?: () => void;
  onPublish?: () => void;
  firstRun?: boolean;
}) {
  if (job.state === 'running' || job.state === 'queued') return null;
  const successful = job.state === 'done' || job.state === 'needs_review';
  if (successful) {
    const hasActions =
      job.diffFiles.length > 0 ||
      Boolean(turnPullRequestUrl(job)) ||
      isTemporaryWorkspace(job.repo) ||
      offersPublication(job, onPublish);
    if (!hasActions && !hasLostContext(job)) return null;
    if (firstRun && job.prUrl)
      return (
        <FirstRunOutcome
          job={job}
          onOpenDiff={onOpenDiff}
          onReview={onReview}
          onWatchPr={onWatchPr}
          onPublish={onPublish}
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
              onPublish={onPublish}
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
        <RunActions job={job} onOpenDiff={onOpenDiff} onPublish={onPublish} />
      </div>
    </div>
  );
}

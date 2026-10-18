'use client';

import { useT } from '@/components/providers';
import Link from 'next/link';
import type { RefObject } from 'react';
import { ActivityIcon } from '../ActivityWindow';
import { ThreadActions } from '../ThreadActions';
import { isTemporaryWorkspace } from '@/lib/temporaryWorkspace';
import {
  TranscriptPresentationControl,
  type TranscriptPresentation,
} from '../transcriptPresentation';
import type { AgentJob } from '../types';
import type { JobDetailPanels } from './useJobDetailPanels';
import { shortcutAriaLabel, shortcutLabel } from '@/lib/keyboardShortcuts';
import { useApplePlatform } from '@/components/features/settings/keyboardShortcutPreferences';

export function JobDetailHeader({
  panels,
  job,
  title,
  canForkCurrent,
  forking,
  onFork,
  onRename,
  presentation,
  onPresentationChange,
  hasActivity,
  activityVisible,
  activityToggleRef,
  toggleActivity,
  stopping,
  onStop,
}: {
  panels: JobDetailPanels;
  job: AgentJob;
  title: string;
  canForkCurrent: boolean;
  forking: boolean;
  onFork: () => void;
  onRename: (title: string) => void;
  presentation: TranscriptPresentation;
  onPresentationChange: (value: TranscriptPresentation) => void;
  hasActivity: boolean;
  activityVisible: boolean;
  activityToggleRef: RefObject<HTMLButtonElement>;
  toggleActivity: () => void;
  stopping: boolean;
  onStop: () => void;
}) {
  const t = useT();
  const apple = useApplePlatform();
  const isArchived = Boolean(job.archivedAt);
  const isActive = job.state === 'running' || job.state === 'queued';
  const { tab, setTab, openWorkspace, workspaceOpen, toggleWorkspace, workspaceShortcut } = panels;
  return (
    <header className="agent-conversation-header relative z-20 shrink-0 border-b border-border bg-surface px-3 sm:px-4">
      {isArchived ? (
        <div className="flex items-center gap-2 pt-2 text-xs text-gray-500 dark:text-gray-400">
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
        <div className="agent-conversation-context flex min-w-0 items-center gap-2 py-1.5">
          <div className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2">
            <h1
              title={title}
              className="max-w-full truncate text-sm font-semibold tracking-tight text-foreground"
            >
              {title}
            </h1>
            {!isTemporaryWorkspace(job.repo) && (
              <span className="min-w-0 truncate text-xs text-muted" title={job.repo}>
                {job.repo}
              </span>
            )}
          </div>
          <ThreadActions
            key={job.threadId ?? job.id}
            job={{ ...job, title }}
            canFork={canForkCurrent}
            forking={forking}
            onFork={onFork}
            onRename={onRename}
            onEnvironment={() => openWorkspace('environment')}
          />
        </div>
        <div className="agent-conversation-controls flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 pb-1.5">
          <nav
            aria-label="Run views"
            className="flex shrink-0 items-center gap-0.5 rounded-md bg-surface-muted p-0.5 text-xs"
          >
            {(['chat', 'trajectory'] as const).map((view) => (
              <button
                key={view}
                type="button"
                onClick={() => setTab(view)}
                aria-current={tab === view ? 'page' : undefined}
                className={`ui-focus-ring min-h-7 rounded-md px-2.5 py-1 capitalize transition-colors [@media(pointer:coarse)]:min-h-11 ${
                  tab === view
                    ? 'bg-surface font-semibold text-foreground shadow-sm ring-1 ring-border'
                    : 'text-muted hover:bg-surface-hover hover:text-foreground'
                }`}
              >
                {view}
              </button>
            ))}
          </nav>
          <div className="ml-auto flex max-w-full flex-wrap items-center gap-2">
            {tab === 'chat' ? (
              <TranscriptPresentationControl value={presentation} onChange={onPresentationChange} />
            ) : null}
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
                onClick={onStop}
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
              aria-keyshortcuts={shortcutAriaLabel(workspaceShortcut, apple)}
              title={`${workspaceOpen ? 'Close workspace' : 'Open workspace'}${workspaceShortcut ? ` (${shortcutLabel(workspaceShortcut, apple)})` : ''}`}
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
  );
}

'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import {
  listAgentSessions,
  type AgentSessionApi,
  type AgentSessionListOptions,
} from '@/lib/api/agents';
import { ArchiveAutoLoad } from './ArchiveAutoLoad';
import { toDisplayState } from './adapt';
import { projectLabel, projectTitle } from './conversations';
import { CONVERSATION_STATE_KEYS, toConversationState } from './conversationState';
import { ARCHIVE_FOCUS, ArchiveIcon, ArchiveProjectMenu } from './ArchiveControls';
import { SessionReviewBadge } from './SessionReviewBadge';
import { useSessionReview } from './useSessionReview';

const ROWS_PER_PAGE = 5;

/** One archived project facet, keyed the way the sidebar keys projects. */
export interface ArchiveProject {
  /** A repository, or `TEMPORARY_WORKSPACE` for the conversations without one. */
  repo: string;
  count: number;
}
const TEXT_BUTTON = `rounded px-1 py-1 text-xs font-medium text-crimson hover:underline disabled:opacity-50 dark:text-rose-300 ${ARCHIVE_FOCUS}`;

function ArchiveSessionReviewBadge({ session }: { session: AgentSessionApi }) {
  const review = useSessionReview({
    id: session.job.id,
    state: toDisplayState(session.job),
    threadId: session.thread_id,
    currentAttemptId: session.job.current_attempt_id,
    cancelRequested: session.job.cancel_requested,
  });
  return <SessionReviewBadge review={review} />;
}

export function ArchiveProjectSection({
  project,
  filters,
  revision,
  removed,
  disabled,
  busy,
  onUnarchive,
  onDelete,
  onUnarchiveGroup,
  onDeleteGroup,
  onShownCount,
  onNeedsRefresh,
}: {
  project: ArchiveProject;
  filters: AgentSessionListOptions;
  revision: number;
  removed: Set<string>;
  disabled: boolean;
  busy: string | null;
  onUnarchive: (session: AgentSessionApi) => void;
  onDelete: (session: AgentSessionApi) => void;
  onUnarchiveGroup: () => void;
  onDeleteGroup: () => void;
  onShownCount: (repo: string, count: number) => void;
  onNeedsRefresh: (repo: string, needed: boolean) => void;
}) {
  const { t, locale } = useLocale();
  const [sessions, setSessions] = useState<AgentSessionApi[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  const activeRequest = useRef<AbortController | null>(null);
  const revealedDepth = useRef(ROWS_PER_PAGE);
  const pageId = `archive-project-${encodeURIComponent(project.repo)}`;
  const visible = sessions.filter((session) => !removed.has(session.thread_id));
  const needsRefresh =
    !collapsed &&
    visible.some((session) => {
      const state =
        session.job.conversation_state ?? toConversationState(toDisplayState(session.job));
      return state === 'running' || state === 'queued' || !session.can_delete;
    });
  const formatter = useMemo(
    () =>
      new Intl.DateTimeFormat(locale, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      }),
    [locale],
  );

  useEffect(() => {
    activeRequest.current?.abort();
    const controller = new AbortController();
    activeRequest.current = controller;
    setLoading(true);
    setError(false);
    async function refreshRevealedRows() {
      const rows = new Map<string, AgentSessionApi>();
      const seenCursors = new Set<string>();
      let nextCursor: string | null = null;
      do {
        const result = await listAgentSessions(
          {
            ...filters,
            repo: project.repo,
            includeSummary: false,
            limit: Math.min(200, revealedDepth.current - rows.size),
            ...(nextCursor ? { cursor: nextCursor } : {}),
          },
          controller.signal,
        );
        if (controller.signal.aborted) return;
        for (const session of result.sessions) rows.set(session.thread_id, session);
        nextCursor = result.next_cursor;
        if (nextCursor && seenCursors.has(nextCursor)) throw new Error('Repeated archive cursor');
        if (nextCursor) seenCursors.add(nextCursor);
      } while (nextCursor && rows.size < revealedDepth.current);
      return { sessions: [...rows.values()], next_cursor: nextCursor };
    }
    void refreshRevealedRows()
      .then((result) => {
        if (!result) return;
        if (controller.signal.aborted) return;
        setSessions(result.sessions);
        setCursor(result.next_cursor);
        setLoading(false);
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setError(true);
        setLoading(false);
      });
    return () => activeRequest.current?.abort();
  }, [filters, project.repo, revision, retry]);

  useEffect(() => {
    onShownCount(project.repo, collapsed ? 0 : visible.length);
  }, [project.repo, collapsed, visible.length, onShownCount]);
  useEffect(() => () => onShownCount(project.repo, 0), [project.repo, onShownCount]);
  useEffect(() => {
    onNeedsRefresh(project.repo, needsRefresh);
  }, [project.repo, needsRefresh, onNeedsRefresh]);
  useEffect(() => () => onNeedsRefresh(project.repo, false), [project.repo, onNeedsRefresh]);

  const loadMore = useCallback(async () => {
    if (!cursor || loading) return;
    const controller = new AbortController();
    activeRequest.current?.abort();
    activeRequest.current = controller;
    setLoading(true);
    setError(false);
    try {
      const page = await listAgentSessions(
        { ...filters, repo: project.repo, limit: ROWS_PER_PAGE, cursor, includeSummary: false },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setSessions((current) => [
        ...new Map(
          [...current, ...page.sessions].map((session) => [session.thread_id, session]),
        ).values(),
      ]);
      revealedDepth.current += page.sessions.length;
      setCursor(page.next_cursor);
    } catch {
      if (!controller.signal.aborted) setError(true);
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [cursor, loading, filters, project.repo]);

  return (
    <section aria-label={projectTitle(project.repo, t)}>
      <div className="mb-2 flex min-w-0 items-center gap-2">
        <h2 className="min-w-0">
          <button
            type="button"
            aria-expanded={!collapsed}
            aria-controls={pageId}
            title={projectTitle(project.repo, t)}
            onClick={() => setCollapsed(!collapsed)}
            className={`flex w-full min-w-0 items-center gap-2 rounded py-1 text-left text-xs font-medium text-gray-700 dark:text-gray-200 [@media(pointer:coarse)]:min-h-11 ${ARCHIVE_FOCUS}`}
          >
            <ArchiveIcon
              name="chevron"
              className={`h-3 w-3 shrink-0 text-gray-400 transition-transform dark:text-gray-500 ${collapsed ? '' : 'rotate-90'}`}
            />
            <ArchiveIcon
              name="folder"
              className="h-4 w-4 shrink-0 text-gray-500 dark:text-gray-400"
            />
            <span className="truncate">{projectLabel(project.repo, t)}</span>
          </button>
        </h2>
        <span className="ml-auto shrink-0 text-[11px] text-gray-500 dark:text-gray-400">
          {t(project.count === 1 ? 'archive.countOne' : 'archive.count', { count: project.count })}
        </span>
        <ArchiveProjectMenu
          project={projectTitle(project.repo, t)}
          count={project.count}
          disabled={disabled || loading}
          onUnarchive={onUnarchiveGroup}
          onDelete={onDeleteGroup}
        />
      </div>
      <div
        id={pageId}
        hidden={collapsed}
        className="divide-y divide-gray-100 rounded-xl border border-gray-200 bg-white px-3 sm:px-4 dark:divide-white/5 dark:border-white/10 dark:bg-gray-900"
      >
        {visible.map((session) => {
          const displayState =
            session.job.conversation_state ?? toConversationState(toDisplayState(session.job));
          const active = displayState === 'running' || displayState === 'queued';
          const date = session.archived_at ? new Date(session.archived_at) : null;
          const validDate = date !== null && !Number.isNaN(date.getTime());
          return (
            <div
              key={session.thread_id}
              className="flex flex-wrap items-center gap-x-3 gap-y-2 py-3 sm:flex-nowrap"
            >
              <Link
                href={`/${session.job.id}`}
                className={`group min-w-0 basis-full rounded sm:flex-1 sm:basis-auto ${ARCHIVE_FOCUS}`}
                title={session.title}
              >
                <h3 className="break-words text-xs font-normal text-gray-800 group-hover:text-crimson sm:truncate dark:text-gray-100 dark:group-hover:text-rose-300">
                  {session.title}
                </h3>
                <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-gray-500 dark:text-gray-400">
                  <time dateTime={validDate ? session.archived_at! : undefined}>
                    {validDate
                      ? t('archive.archivedOn', { date: formatter.format(date) })
                      : t('archive.dateUnknown')}
                  </time>
                  {active ? (
                    <span
                      className="inline-flex items-center gap-1 text-sky-700 dark:text-sky-300"
                      title={t('archive.runningHint')}
                    >
                      <span className="h-1 w-1 rounded-full bg-current" />
                      {t(CONVERSATION_STATE_KEYS[displayState])}
                    </span>
                  ) : (
                    <span className="sr-only">{t(CONVERSATION_STATE_KEYS[displayState])}</span>
                  )}
                  <ArchiveSessionReviewBadge session={session} />
                </div>
              </Link>
              <div className="ml-auto flex shrink-0 items-center gap-1.5">
                <button
                  type="button"
                  aria-label={t('archive.deleteSession', { title: session.title })}
                  title={
                    session.can_delete
                      ? t('archive.deleteSession', { title: session.title })
                      : t('archive.deleteBusy')
                  }
                  disabled={disabled || loading || !session.can_delete}
                  onClick={() => onDelete(session)}
                  className={`rounded-md p-2 text-gray-400 hover:bg-red-50 hover:text-red-600 disabled:cursor-default disabled:opacity-30 dark:text-gray-500 dark:hover:bg-red-500/10 dark:hover:text-red-300 [@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11 ${ARCHIVE_FOCUS}`}
                >
                  <ArchiveIcon name="delete" />
                </button>
                <button
                  type="button"
                  aria-label={t('archive.unarchiveSession', { title: session.title })}
                  disabled={disabled || loading}
                  onClick={() => onUnarchive(session)}
                  className={`inline-flex shrink-0 items-center justify-center rounded-lg bg-gray-100 px-3 py-2 text-xs font-medium text-gray-700 hover:bg-gray-200 disabled:cursor-wait disabled:opacity-50 dark:bg-white/5 dark:text-gray-200 dark:hover:bg-white/10 [@media(pointer:coarse)]:min-h-11 ${ARCHIVE_FOCUS}`}
                >
                  {busy === session.thread_id ? t('archive.unarchiving') : t('archive.unarchive')}
                </button>
              </div>
            </div>
          );
        })}
        {loading ? (
          <p role="status" className="py-4 text-center text-xs text-gray-500 dark:text-gray-400">
            {t('archive.loading')}
          </p>
        ) : null}
        {error ? (
          <div role="alert" className="py-4 text-center">
            <p className="text-xs text-red-600 dark:text-red-300">{t('archive.loadFailed')}</p>
            <button
              type="button"
              onClick={() => (cursor ? void loadMore() : setRetry((count) => count + 1))}
              className={`mt-2 ${TEXT_BUTTON}`}
            >
              {t('archive.retry')}
            </button>
          </div>
        ) : null}
        {!collapsed && !disabled && !loading && !error && cursor ? (
          <ArchiveAutoLoad onLoad={loadMore} />
        ) : null}
        {!loading && !error && !cursor && !visible.length ? (
          <p className="py-4 text-center text-xs text-gray-500 dark:text-gray-400">
            {t('archive.groupEmpty')}
          </p>
        ) : null}
      </div>
    </section>
  );
}

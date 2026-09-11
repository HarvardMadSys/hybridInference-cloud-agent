'use client';

import Link from 'next/link';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import {
  AGENT_SESSION_SELECTION_LIMIT as BULK_LIMIT,
  deleteAgentSessions,
  listAgentSessions,
  setAgentSessionsArchived,
  type AgentSessionApi,
  type AgentSessionListOptions,
  type AgentSessionProjectApi,
} from '@/lib/api/agents';
import { onAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { APIError } from '@/lib/utils/errors';
import { ARCHIVE_FOCUS, ArchiveDeleteDialog, ArchiveIcon } from './ArchiveControls';
import { ArchiveAutoLoad } from './ArchiveAutoLoad';
import { ArchiveProjectSection } from './ArchiveProjectSection';
import { CONVERSATION_STATE_KEYS, type ConversationState } from './conversationState';

const GROUPS_PER_PAGE = 6;
const BULK_PAGE_SIZE = 100;
const SESSION_ERROR_KEYS = {
  session_active: 'archive.sessionActive',
  fork_in_progress: 'archive.forkInProgress',
  publication_pending: 'archive.publicationPending',
  not_archived: 'archive.sessionNotArchived',
  not_found: 'archive.sessionNotFound',
} as const;
const SOFT_BUTTON = `inline-flex items-center justify-center gap-1.5 rounded-lg bg-gray-100 px-3 py-2 text-xs font-medium text-gray-700 hover:bg-gray-200 disabled:opacity-50 dark:bg-white/5 dark:text-gray-200 dark:hover:bg-white/10 ${ARCHIVE_FOCUS}`;
const TEXT_BUTTON = `rounded px-1 py-1 text-xs font-medium text-crimson hover:underline disabled:opacity-50 dark:text-rose-300 ${ARCHIVE_FOCUS}`;

interface DeleteSnapshot {
  sessions: AgentSessionApi[];
  skipped: number;
  scope: string;
}

type Notice =
  | { kind: 'unarchived'; ids: string[]; href: string }
  | { kind: 'deleted'; count: number }
  | { kind: 'undone' };

/** Grouped archive management. Bulk actions use a complete, explicit ID snapshot. */
export function ArchivedTasksView() {
  const { t } = useLocale();
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [repo, setRepo] = useState('');
  const [state, setState] = useState<ConversationState | ''>('');
  const [projects, setProjects] = useState<AgentSessionProjectApi[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(false);
  const [revision, setRevision] = useState(0);
  const [shownGroups, setShownGroups] = useState(GROUPS_PER_PAGE);
  const [shownCounts, setShownCounts] = useState<Record<string, number>>({});
  const [liveGroups, setLiveGroups] = useState<Record<string, boolean>>({});
  const [removed, setRemoved] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [pendingDelete, setPendingDelete] = useState<DeleteSnapshot | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const actionRequest = useRef<AbortController | null>(null);
  const loadedFilters = useRef<AgentSessionListOptions | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(search.trim()), 250);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(
    () =>
      onAgentSessionsChanged((change) => {
        setRemoved((current) => {
          const next = new Set(current);
          for (const id of change.threadIds) {
            if (change.deleted || change.archived === false) next.add(id);
            else if (change.archived === true) next.delete(id);
          }
          return next;
        });
        setRevision((current) => current + 1);
      }),
    [],
  );

  useEffect(() => () => actionRequest.current?.abort(), []);

  const filters = useMemo<AgentSessionListOptions>(
    () => ({
      archived: true,
      ...(query ? { search: query } : {}),
      ...(repo ? { repo } : {}),
      ...(state ? { state } : {}),
    }),
    [query, repo, state],
  );

  useEffect(() => {
    setShownGroups(GROUPS_PER_PAGE);
    setShownCounts({});
    setLiveGroups({});
  }, [filters]);

  const hasLiveSessions = Object.values(liveGroups).some(Boolean);
  useEffect(() => {
    if (!hasLiveSessions || busy || pendingDelete) return;
    const timer = window.setInterval(() => setRevision((current) => current + 1), 10_000);
    return () => window.clearInterval(timer);
  }, [hasLiveSessions, busy, pendingDelete]);

  useEffect(() => {
    const controller = new AbortController();
    if (loadedFilters.current !== filters) setLoading(true);
    setRefreshing(true);
    setError(false);
    // Facets cover all matching history. Each project pages its own rows, so
    // one large repository cannot push every smaller project off the page.
    void listAgentSessions({ ...filters, limit: 1 }, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;
        if (result.projects === null || result.total === null)
          throw new Error('Archive summary is missing');
        setProjects(result.projects);
        setTotal(result.total);
        loadedFilters.current = filters;
        setLoading(false);
        setRefreshing(false);
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setError(true);
        setLoading(false);
        setRefreshing(false);
      });
    return () => controller.abort();
  }, [filters, revision]);

  const filtered = Boolean(query || repo || state);
  const hasCurrentIndex = loadedFilters.current === filters;
  const filterPending = search.trim() !== query;
  const controlsDisabled = Boolean(busy || pendingDelete || refreshing);
  const matchingProjects = projects.filter((project) => !repo || project.repo === repo);
  const displayedProjects = matchingProjects.slice(0, shownGroups);
  const showNextProjects = useCallback(() => {
    setShownGroups((count) => count + GROUPS_PER_PAGE);
  }, []);
  const shownTotal = displayedProjects.reduce(
    (sum, project) => sum + (shownCounts[project.repo] ?? 0),
    0,
  );
  const setShownCount = useCallback((project: string, count: number) => {
    setShownCounts((current) =>
      current[project] === count ? current : { ...current, [project]: count },
    );
  }, []);
  const setNeedsRefresh = useCallback((project: string, value: boolean) => {
    setLiveGroups((current) =>
      current[project] === value ? current : { ...current, [project]: value },
    );
  }, []);
  let noticeText = '';
  if (notice?.kind === 'unarchived') {
    noticeText = t(notice.ids.length === 1 ? 'archive.unarchivedOne' : 'archive.unarchived', {
      count: notice.ids.length,
    });
  } else if (notice?.kind === 'deleted') {
    noticeText = t(notice.count === 1 ? 'archive.deletedOne' : 'archive.deleted', {
      count: notice.count,
    });
  } else if (notice?.kind === 'undone') {
    noticeText = t('archive.undone');
  }

  function clearFilters() {
    setSearch('');
    setQuery('');
    setRepo('');
    setState('');
  }

  function actionFailure(cause: unknown, fallback: string): string {
    if (!(cause instanceof APIError)) return fallback;
    if (cause.code === 'session_selection_too_large')
      return t('archive.bulkLimit', { count: BULK_LIMIT });
    const key = SESSION_ERROR_KEYS[cause.code as keyof typeof SESSION_ERROR_KEYS];
    return key ? t(key) : cause.message;
  }

  function refreshAfterConflict(cause: unknown): boolean {
    if (!(cause instanceof APIError) || ![404, 409].includes(cause.statusCode ?? 0)) return false;
    setRevision((current) => current + 1);
    return true;
  }

  async function collectSessions(project?: string): Promise<AgentSessionApi[]> {
    actionRequest.current?.abort();
    const controller = new AbortController();
    actionRequest.current = controller;
    const byId = new Map<string, AgentSessionApi>();
    const cursors = new Set<string>();
    let cursor: string | undefined;
    let pages = 0;
    do {
      // Bound requests too: concurrent changes must not keep collection paging forever.
      if (++pages > Math.ceil(BULK_LIMIT / BULK_PAGE_SIZE) + 1)
        throw new APIError('session_selection_too_large', 'Selection exceeds the bulk limit');
      const page = await listAgentSessions(
        {
          ...filters,
          ...(project !== undefined ? { repo: project } : {}),
          limit: BULK_PAGE_SIZE,
          includeSummary: !cursor,
          ...(cursor ? { cursor } : {}),
        },
        controller.signal,
      );
      if (!cursor && page.total === null) throw new Error('Archive summary is missing');
      if (page.total !== null && page.total > BULK_LIMIT)
        throw new APIError('session_selection_too_large', 'Selection exceeds the bulk limit');
      for (const session of page.sessions) byId.set(session.thread_id, session);
      if (byId.size > BULK_LIMIT)
        throw new APIError('session_selection_too_large', 'Selection exceeds the bulk limit');
      const next = page.next_cursor;
      if (next && cursors.has(next)) throw new Error('Repeated archive cursor');
      if (next) cursors.add(next);
      cursor = next ?? undefined;
    } while (cursor);
    return [...byId.values()];
  }

  async function unarchive(session?: AgentSessionApi, project?: string) {
    setBusy(session?.thread_id ?? project ?? 'unarchive');
    setActionError(null);
    try {
      const sessions = session ? [session] : await collectSessions(project);
      if (!sessions.length) return;
      if (sessions.length > BULK_LIMIT) {
        setActionError(t('archive.bulkLimit', { count: BULK_LIMIT }));
        return;
      }
      const result = await setAgentSessionsArchived(
        sessions.map((item) => item.thread_id),
        false,
      );
      const ids = result.sessions.map((item) => item.thread_id);
      const restored = sessions.filter((item) => ids.includes(item.thread_id));
      setRemoved((current) => new Set([...current, ...ids]));
      setNotice({
        kind: 'unarchived',
        ids,
        href: restored.length === 1 ? `/${restored[0].job.id}` : '/',
      });
    } catch (cause) {
      refreshAfterConflict(cause);
      setActionError(actionFailure(cause, t('archive.unarchiveFailed')));
    } finally {
      setBusy(null);
    }
  }

  async function prepareDelete(session?: AgentSessionApi, project?: string) {
    setBusy('prepare-delete');
    setActionError(null);
    setDeleteError(null);
    const scope = session
      ? t('archive.scopeSession', { title: session.title })
      : project !== undefined
        ? t(filtered ? 'archive.scopeProjectFiltered' : 'archive.scopeProject', { project })
        : t(filtered ? 'archive.scopeFiltered' : 'archive.scopeAll');
    try {
      const sessions = session ? [session] : await collectSessions(project);
      const deletable = sessions.filter((item) => item.can_delete);
      setPendingDelete({ sessions: deletable, skipped: sessions.length - deletable.length, scope });
    } catch (cause) {
      refreshAfterConflict(cause);
      setActionError(actionFailure(cause, t('archive.prepareFailed')));
    } finally {
      setBusy(null);
    }
  }

  async function confirmDelete() {
    if (
      !pendingDelete ||
      !pendingDelete.sessions.length ||
      pendingDelete.sessions.length > BULK_LIMIT
    )
      return;
    setBusy('delete');
    setDeleteError(null);
    try {
      const result = await deleteAgentSessions(
        pendingDelete.sessions.map((session) => session.thread_id),
      );
      setRemoved((current) => new Set([...current, ...result.thread_ids]));
      setNotice({ kind: 'deleted', count: result.thread_ids.length });
      setPendingDelete(null);
    } catch (cause) {
      const message = actionFailure(cause, t('archive.deleteFailed'));
      if (refreshAfterConflict(cause)) {
        setPendingDelete(null);
        setActionError(`${message} ${t('archive.selectionChanged')}`);
      } else {
        setDeleteError(message);
      }
    } finally {
      setBusy(null);
    }
  }

  async function undo() {
    if (notice?.kind !== 'unarchived') return;
    setBusy('undo');
    setActionError(null);
    try {
      const result = await setAgentSessionsArchived(notice.ids, true);
      setRemoved((current) => {
        const next = new Set(current);
        for (const session of result.sessions) next.delete(session.thread_id);
        return next;
      });
      setNotice({ kind: 'undone' });
    } catch (cause) {
      refreshAfterConflict(cause);
      if (cause instanceof APIError && cause.statusCode === 404) setNotice(null);
      setActionError(actionFailure(cause, t('archive.undoFailed')));
    } finally {
      setBusy(null);
    }
  }

  return (
    <section
      className="mx-auto w-full max-w-5xl px-4 py-8 sm:px-7 lg:px-10 lg:py-10"
      aria-labelledby="archive-page-title"
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1
            id="archive-page-title"
            className="text-2xl font-medium tracking-tight text-gray-900 dark:text-gray-100"
          >
            {t('archive.title')}
          </h1>
          <p className="mt-2 text-xs leading-relaxed text-gray-500 dark:text-gray-400">
            {t('archive.subtitle')}
          </p>
        </div>
        <button
          type="button"
          disabled={controlsDisabled || loading || error || filterPending || total === 0}
          onClick={() => void prepareDelete()}
          className={`ml-auto inline-flex items-center gap-1.5 rounded-lg bg-red-50 px-3 py-2 text-xs font-medium text-red-600 hover:bg-red-100 disabled:opacity-40 dark:bg-red-500/10 dark:text-red-300 dark:hover:bg-red-500/20 ${ARCHIVE_FOCUS}`}
        >
          <ArchiveIcon name="delete" />
          {t(filtered ? 'archive.deleteFiltered' : 'archive.deleteAll')}
        </button>
      </div>

      {notice ? (
        <div className="mt-5 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2.5 dark:border-white/10 dark:bg-white/5">
          <ArchiveIcon
            name="check"
            className="h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-300"
          />
          <p role="status" className="min-w-0 flex-1 text-xs text-gray-700 dark:text-gray-200">
            {noticeText}
          </p>
          {notice.kind === 'unarchived' ? (
            <>
              <Link href={notice.href} className={TEXT_BUTTON}>
                {t('archive.view')}
              </Link>
              <button
                type="button"
                disabled={controlsDisabled}
                onClick={() => void undo()}
                className={TEXT_BUTTON}
              >
                {t('archive.undo')}
              </button>
            </>
          ) : null}
          <button
            type="button"
            aria-label={t('archive.dismiss')}
            disabled={controlsDisabled}
            onClick={() => setNotice(null)}
            className={`rounded-md p-1.5 text-gray-400 hover:bg-gray-200 dark:text-gray-500 dark:hover:bg-white/10 ${ARCHIVE_FOCUS}`}
          >
            <ArchiveIcon name="close" />
          </button>
        </div>
      ) : null}
      {actionError ? (
        <p role="alert" className="mt-4 text-xs text-red-600 dark:text-red-300">
          {actionError}
        </p>
      ) : null}
      {error && hasCurrentIndex ? (
        <div
          role="alert"
          className="mt-4 flex flex-wrap items-center gap-2 text-xs text-red-600 dark:text-red-300"
        >
          <p>{t('archive.refreshFailed')}</p>
          <button
            type="button"
            onClick={() => setRevision((current) => current + 1)}
            className={TEXT_BUTTON}
          >
            {t('archive.retry')}
          </button>
        </div>
      ) : null}
      {busy === 'prepare-delete' ? (
        <p role="status" className="mt-4 text-xs text-gray-500 dark:text-gray-400">
          {t('archive.preparing')}
        </p>
      ) : null}

      <div className="my-7 grid grid-cols-2 gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(130px,auto)_minmax(140px,auto)]">
        <label className="col-span-2 flex min-w-0 items-center gap-2 rounded-lg border border-gray-200 bg-white px-3 text-gray-400 focus-within:border-crimson/40 focus-within:ring-2 focus-within:ring-crimson/10 sm:col-span-1 dark:border-white/10 dark:bg-gray-900 dark:text-gray-500">
          <ArchiveIcon name="search" className="h-4 w-4 shrink-0" />
          <span className="sr-only">{t('archive.search')}</span>
          <input
            type="search"
            value={search}
            disabled={controlsDisabled}
            onChange={(event) => setSearch(event.target.value)}
            placeholder={t('archive.search')}
            className="min-w-0 flex-1 bg-transparent py-2.5 text-xs text-gray-900 outline-none placeholder:text-gray-400 disabled:opacity-50 dark:text-gray-100 dark:placeholder:text-gray-500"
          />
        </label>
        <label className="relative flex min-w-0 items-center gap-2 rounded-lg border border-gray-200 bg-white pl-3 text-gray-400 focus-within:border-crimson/40 focus-within:ring-2 focus-within:ring-crimson/10 dark:border-white/10 dark:bg-gray-900 dark:text-gray-500">
          <ArchiveIcon name="filter" className="h-4 w-4 shrink-0" />
          <span className="sr-only">{t('archive.statusFilter')}</span>
          <select
            value={state}
            disabled={controlsDisabled}
            onChange={(event) => setState(event.target.value as ConversationState | '')}
            className="w-full min-w-0 cursor-pointer appearance-none bg-transparent py-2.5 pr-8 text-xs text-gray-700 outline-none disabled:opacity-50 dark:text-gray-200 dark:[color-scheme:dark]"
          >
            <option value="">{t('archive.allStates')}</option>
            {Object.entries(CONVERSATION_STATE_KEYS).map(([value, key]) => (
              <option key={value} value={value}>
                {t(key)}
              </option>
            ))}
          </select>
          <ArchiveIcon
            name="chevron"
            className="pointer-events-none absolute right-3 h-3 w-3 rotate-90"
          />
        </label>
        <label className="relative flex min-w-0 items-center gap-2 rounded-lg border border-gray-200 bg-white pl-3 text-gray-400 focus-within:border-crimson/40 focus-within:ring-2 focus-within:ring-crimson/10 dark:border-white/10 dark:bg-gray-900 dark:text-gray-500">
          <ArchiveIcon name="folder" className="h-4 w-4 shrink-0" />
          <span className="sr-only">{t('archive.projectFilter')}</span>
          <select
            value={repo}
            disabled={controlsDisabled}
            onChange={(event) => setRepo(event.target.value)}
            className="w-full min-w-0 cursor-pointer appearance-none truncate bg-transparent py-2.5 pr-8 text-xs text-gray-700 outline-none disabled:opacity-50 dark:text-gray-200 dark:[color-scheme:dark]"
          >
            <option value="">{t('archive.allProjects')}</option>
            {repo && !projects.some((project) => project.repo === repo) ? (
              <option value={repo}>{repo}</option>
            ) : null}
            {projects.map((project) => (
              <option key={project.repo} value={project.repo}>
                {project.repo}
              </option>
            ))}
          </select>
          <ArchiveIcon
            name="chevron"
            className="pointer-events-none absolute right-3 h-3 w-3 rotate-90"
          />
        </label>
      </div>

      {loading ? (
        <p role="status" className="py-8 text-center text-xs text-gray-500 dark:text-gray-400">
          {t('archive.loading')}
        </p>
      ) : error && !hasCurrentIndex ? (
        <div
          role="alert"
          className="rounded-xl border border-red-100 p-6 text-center dark:border-red-500/20"
        >
          <p className="text-xs text-red-600 dark:text-red-300">{t('archive.loadFailed')}</p>
          <button
            type="button"
            onClick={() => setRevision((current) => current + 1)}
            className={`mt-3 ${SOFT_BUTTON}`}
          >
            {t('archive.retry')}
          </button>
        </div>
      ) : total === 0 ? (
        <div className="rounded-xl border border-dashed border-gray-200 px-5 py-16 text-center dark:border-white/10">
          <ArchiveIcon
            name={filtered ? 'search' : 'archive'}
            className="mx-auto mb-4 h-7 w-7 text-gray-300 dark:text-gray-600"
          />
          <h2 className="text-sm font-medium text-gray-700 dark:text-gray-200">
            {t(filtered ? 'archive.noResults' : 'archive.empty')}
          </h2>
          <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            {t(filtered ? 'archive.noResultsHint' : 'archive.emptyHint')}
          </p>
          {filtered ? (
            <button type="button" onClick={clearFilters} className={`mt-3 ${TEXT_BUTTON}`}>
              {t('archive.clearFilters')}
            </button>
          ) : null}
        </div>
      ) : (
        <>
          <div className="space-y-5">
            {displayedProjects.map((project) => (
              <ArchiveProjectSection
                key={`${query}\u0000${state}\u0000${project.repo}`}
                project={project}
                filters={filters}
                revision={revision}
                removed={removed}
                disabled={controlsDisabled || filterPending}
                busy={busy}
                onUnarchive={(session) => void unarchive(session)}
                onDelete={(session) => void prepareDelete(session)}
                onUnarchiveGroup={() => void unarchive(undefined, project.repo)}
                onDeleteGroup={() => void prepareDelete(undefined, project.repo)}
                onShownCount={setShownCount}
                onNeedsRefresh={setNeedsRefresh}
              />
            ))}
          </div>
          {matchingProjects.length > shownGroups ? (
            <ArchiveAutoLoad key={shownGroups} onLoad={showNextProjects} />
          ) : null}
          <div className="mt-6 flex flex-wrap justify-between gap-2 text-[11px] text-gray-500 dark:text-gray-400">
            <span>{t('archive.showing', { count: shownTotal, total })}</span>
            <span>{t('archive.order')}</span>
          </div>
        </>
      )}
      {pendingDelete ? (
        <ArchiveDeleteDialog
          count={pendingDelete.sessions.length}
          skipped={pendingDelete.skipped}
          scope={pendingDelete.scope}
          titles={pendingDelete.sessions.slice(0, 3).map((session) => session.title)}
          busy={busy === 'delete'}
          limitExceeded={pendingDelete.sessions.length > BULK_LIMIT}
          error={deleteError}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => void confirmDelete()}
        />
      ) : null}
    </section>
  );
}

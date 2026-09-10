'use client';
import Link from 'next/link';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useAuth, useT } from '@/components/providers';
import { SettingsLauncher } from '@/components/features/settings/SettingsLauncher';
import {
  archiveAgentJob,
  pinAgentJob,
  unpinAgentJob,
  getAgentJob,
  getAgentJobThread,
} from '@/lib/api/agents';
import { CONVERSATION_RESTORED_EVENT, type ConversationRestoredDetail } from './conversationEvents';
import { onAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { PaneResizer } from './PaneResizer';
import { useAgentProjects } from './useAgentJobs';
import { announcePaneResize, useResizablePane } from './useResizablePane';
import { useSidebarPreferences, manualOrder, moveBefore } from './sidebarPreferences';
import { SidebarViewMenu } from './SidebarViewMenu';
import { SearchControls } from './SearchControls';
import {
  useConversationList,
  conversationRow,
  type SidebarConversation,
} from './useConversationList';
import {
  TaskRow,
  ProjectFolder,
  ListStatus,
  type RowActions,
  type SidebarReorderProps,
} from './SidebarRows';
export { groupJobsByConversation, groupJobsByProject } from './conversations';
export const SIDEBAR_WIDTH_STORAGE_KEY = 'agents.sidebar.width';
export function AgentsSidebar({ collapsed = false }: { collapsed?: boolean }) {
  const pathname = usePathname() ?? '';
  const params = useSearchParams();
  const router = useRouter();
  const t = useT();
  const { state } = useAuth();
  const userId = state.user?.id;
  const [preferences, updatePreferences] = useSidebarPreferences(userId);
  const knownRows = useMemo(
    () => ({ userId, rows: new Map<string, SidebarConversation>() }),
    [userId],
  ).rows;
  const [refresh, setRefresh] = useState(0);
  const reload = useCallback(() => setRefresh((v) => v + 1), []);
  const {
    projects,
    loading: projectsLoading,
    error: projectsError,
    reload: reloadProjects,
  } = useAgentProjects();
  const list = useConversationList({ userId, refresh, enabled: preferences.group === 'flat' });
  const [selected, setSelected] = useState<{
    path: string;
    userId?: string;
    row: SidebarConversation;
  } | null>(null);
  const sessionGeneration = useRef(0);
  const [sessionRefresh, setSessionRefresh] = useState(0);
  const [hiddenThreads, setHiddenThreads] = useState<Set<string>>(new Set());
  const [archiving, setArchiving] = useState<string | null>(null);
  const [pinning, setPinning] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [scope, setScope] = useState('all');
  const [includeArchived, setIncludeArchived] = useState(false);
  const searchInput = useRef<HTMLInputElement>(null);
  const searchButton = useRef<HTMLButtonElement>(null);
  const resultList = useRef<HTMLDivElement>(null);
  const pane = useResizablePane({
    storageKey: SIDEBAR_WIDTH_STORAGE_KEY,
    defaultWidth: 288,
    minWidth: 180,
    maxWidth: 520,
    side: 'start',
    maxViewportFraction: 0.4,
  });
  const current =
    selected?.path === pathname && selected.userId === userId
      ? selected.row
      : (list.rows.find((row) => row.jobIds.some((id) => pathname === `/${id}`)) ??
        [...knownRows.values()].find((row) => row.jobIds.some((id) => pathname === `/${id}`)));
  const activeKey = current?.key ?? null;
  const activeRepo = pathname === '/' ? params.get('repo') : (current?.job.repo ?? null);
  const searching = searchOpen && (query.trim() !== '' || scope !== 'all' || includeArchived);
  const search = useConversationList({
    q: debounced,
    repo: scope === 'current' ? (activeRepo ?? undefined) : undefined,
    includeArchived,
    enabled: searching && debounced === query.trim(),
    userId,
    refresh,
  });
  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query.trim()), 250);
    return () => clearTimeout(timer);
  }, [query]);
  useEffect(() => {
    if (!activeRepo) setScope('all');
  }, [activeRepo]);
  useEffect(() => {
    announcePaneResize();
  }, [collapsed]);
  useEffect(() => {
    setHiddenThreads(new Set());
  }, [userId]);
  useEffect(
    () =>
      onAgentSessionsChanged((change) => {
        sessionGeneration.current += 1;
        setSessionRefresh(sessionGeneration.current);
        for (const id of change.threadIds) knownRows.delete(id);
        setHiddenThreads((current) => {
          const next = new Set(current);
          for (const id of change.threadIds) {
            if (change.archived === false && !change.deleted) next.delete(id);
            else next.add(id);
          }
          return next;
        });
      }),
    [knownRows],
  );
  useEffect(() => {
    const restored = (event: Event) => {
      const detail = (event as CustomEvent<ConversationRestoredDetail>).detail;
      if (!detail || detail.userId !== userId || typeof detail.threadId !== 'string') return;
      setHiddenThreads((old) => {
        const next = new Set(old);
        next.delete(detail.threadId);
        return next;
      });
      setSelected((old) =>
        old?.row.key === detail.threadId ? { ...old, row: { ...old.row, archived: false } } : old,
      );
      reload();
      reloadProjects();
    };
    window.addEventListener(CONVERSATION_RESTORED_EVENT, restored);
    return () => window.removeEventListener(CONVERSATION_RESTORED_EVENT, restored);
  }, [userId, reload, reloadProjects]);
  useEffect(() => {
    let cancelled = false;
    const generation = sessionGeneration.current;
    setSelected(null);
    const id = /^\/([^/]+)$/.exec(pathname)?.[1];
    if (!id || ['archived', 'integrations', 'admin', 'login'].includes(id)) return;
    Promise.all([getAgentJob(id), getAgentJobThread(id)])
      .then(([job, thread]) => {
        if (cancelled || generation !== sessionGeneration.current) return;
        const latest =
          thread?.jobs.reduce((a, b) => ((b.turn_no ?? 1) > (a.turn_no ?? 1) ? b : a), job) ?? job;
        const row = conversationRow({
          job: latest,
          title: thread?.title ?? job.task_prompt.split('\n')[0],
          archived: Boolean(thread?.archived_at),
          last_activity_at: thread?.updated_at ?? latest.created_at ?? null,
        });
        row.jobIds = [...new Set([id, ...(thread?.jobs.map((turn) => turn.id) ?? [])])];
        knownRows.set(row.key, row);
        setSelected({ path: pathname, userId, row });
        updatePreferences((old) => ({
          ...old,
          expanded: { ...old.expanded, [row.job.repo]: true },
        }));
      })
      .catch(() => {
        /* Detail page owns errors; never guess a project. */
      });
    return () => {
      cancelled = true;
    };
  }, [pathname, userId, updatePreferences, knownRows, sessionRefresh]);
  const mountPath = useRef(pathname);
  useEffect(() => {
    if (mountPath.current === pathname) return;
    mountPath.current = pathname;
    reload();
    reloadProjects();
  }, [pathname, reload, reloadProjects]);
  useEffect(() => {
    if (!projects.some((project) => project.active_count > 0)) return;
    const timer = setInterval(() => {
      reload();
      reloadProjects();
    }, 10000);
    return () => clearInterval(timer);
  }, [projects, reload, reloadProjects]);
  const closeSearch = useCallback(() => {
    setSearchOpen(false);
    setQuery('');
    setDebounced('');
    setScope('all');
    setIncludeArchived(false);
    searchButton.current?.focus();
  }, []);
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && !collapsed) {
        event.preventDefault();
        setSearchOpen(true);
        requestAnimationFrame(() => searchInput.current?.focus());
      }
    };
    document.addEventListener('keydown', keydown);
    return () => document.removeEventListener('keydown', keydown);
  }, [collapsed]);
  const registerRows = useCallback(
    (rows: SidebarConversation[]) => {
      if (!rows.length) return;
      rows.forEach((row) => {
        const previous = knownRows.get(row.key);
        knownRows.set(row.key, {
          ...row,
          jobIds: [...new Set([...(previous?.jobIds ?? []), ...row.jobIds])],
        });
      });
      updatePreferences((old) => {
        if (!old.manualInitialized && old.order !== 'manual') return old;
        const tasks = { ...old.tasks };
        let changed = false;
        for (const row of rows)
          for (const group of ['flat', `repo:${row.job.repo}`]) {
            const ids = tasks[group] ?? [];
            if (!ids.includes(row.key)) {
              tasks[group] = [...ids, row.key];
              changed = true;
            }
          }
        return changed ? { ...old, tasks } : old;
      });
    },
    [updatePreferences, knownRows],
  );
  useEffect(() => {
    registerRows(list.rows);
  }, [list.rows, registerRows]);
  const baseFolders = useMemo(() => {
    const repos = new Map(projects.map((project) => [project.repo, project]));
    for (const row of preferences.group === 'flat' ? list.rows : [])
      if (!repos.has(row.job.repo))
        repos.set(row.job.repo, {
          repo: row.job.repo,
          task_count: 1,
          active_count: 0,
          last_activity_at: row.lastActivityAt,
        });
    if (activeRepo && !repos.has(activeRepo))
      repos.set(activeRepo, {
        repo: activeRepo,
        task_count: 0,
        active_count: 0,
        last_activity_at: null,
      });
    return [...repos.values()].sort(
      (a, b) =>
        (Date.parse(b.pinned_at ?? '') || 0) - (Date.parse(a.pinned_at ?? '') || 0) ||
        (Date.parse(b.last_activity_at ?? '') || 0) - (Date.parse(a.last_activity_at ?? '') || 0),
    );
  }, [projects, list.rows, activeRepo, preferences.group]);
  useEffect(() => {
    updatePreferences((old) => {
      if (!old.manualInitialized && old.order !== 'manual') return old;
      const added = baseFolders.map((p) => p.repo).filter((repo) => !old.projects.includes(repo));
      return added.length ? { ...old, projects: [...old.projects, ...added] } : old;
    });
  }, [baseFolders, updatePreferences]);
  const folders =
    preferences.order === 'manual'
      ? manualOrder(baseFolders, preferences.projects, (p) => p.repo)
      : baseFolders;
  const defaultRepo = activeRepo ?? folders[0]?.repo;
  const drag = useRef<{ group: string; id: string } | null>(null);
  const [announcement, setAnnouncement] = useState('');
  const pointerAllowsDrag = useRef(true);
  const [dropTarget, setDropTarget] = useState<{
    group: string;
    id: string;
    position: 'before' | 'after';
  } | null>(null);
  function reorder(group: string, ids: string[], source: string, target: string, after = false) {
    let next = moveBefore(ids, source, target);
    if (after && source !== target && ids.includes(source) && ids.includes(target)) {
      next = ids.filter((id) => id !== source);
      next.splice(next.indexOf(target) + 1, 0, source);
    }
    updatePreferences((old) => {
      if (group === 'projects') return { ...old, projects: next };
      const saved = old.tasks[group] ?? ids;
      const peers = new Set(ids);
      let index = 0;
      const merged = saved.map((id) => (peers.has(id) ? next[index++] : id));
      merged.push(...next.slice(index));
      return { ...old, tasks: { ...old.tasks, [group]: merged } };
    });
    setAnnouncement(t('sidebar.moved'));
  }
  function reorderProps(group: string, ids: string[], id: string): SidebarReorderProps {
    if (preferences.order !== 'manual' || searching) return {};
    const canDrop = () =>
      drag.current?.group === group && drag.current.id !== id && ids.includes(drag.current.id);
    const positionAt = (element: HTMLElement, y: number): 'before' | 'after' => {
      const rect = element.getBoundingClientRect();
      return y > rect.top + rect.height / 2 ? 'after' : 'before';
    };
    return {
      draggable: true,
      title: t('sidebar.reorderHint'),
      'data-drop-position':
        dropTarget?.group === group && dropTarget.id === id ? dropTarget.position : undefined,
      onPointerDownCapture: (event) => {
        pointerAllowsDrag.current = !(event.target as Element).closest('button, [data-row-action]');
      },
      onDragStart: (event) => {
        if (
          !pointerAllowsDrag.current ||
          (event.target as Element).closest('button, [data-row-action]')
        ) {
          event.preventDefault();
          event.stopPropagation();
          return;
        }
        drag.current = { group, id };
        setDropTarget(null);
        event.dataTransfer.effectAllowed = 'move';
        event.dataTransfer.clearData?.();
        event.dataTransfer.setData('text/plain', id);
        event.stopPropagation();
      },
      onDragEnd: () => {
        drag.current = null;
        setDropTarget(null);
        pointerAllowsDrag.current = true;
      },
      onDragOver: (event) => {
        if (!canDrop()) {
          setDropTarget(null);
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
        const position = positionAt(event.currentTarget, event.clientY);
        setDropTarget((old) =>
          old?.group === group && old.id === id && old.position === position
            ? old
            : { group, id, position },
        );
      },
      onDragLeave: (event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
          setDropTarget((old) => (old?.group === group && old.id === id ? null : old));
        }
      },
      onDrop: (event) => {
        event.preventDefault();
        event.stopPropagation();
        if (canDrop())
          reorder(
            group,
            ids,
            drag.current!.id,
            id,
            positionAt(event.currentTarget, event.clientY) === 'after',
          );
        drag.current = null;
        setDropTarget(null);
        pointerAllowsDrag.current = true;
      },
      onKeyDown: (event) => {
        if (!event.altKey || !['ArrowUp', 'ArrowDown'].includes(event.key)) return;
        event.preventDefault();
        event.stopPropagation();
        const index = ids.indexOf(id),
          target = ids[index + (event.key === 'ArrowDown' ? 1 : -1)];
        if (target) reorder(group, ids, id, target, event.key === 'ArrowDown');
      },
    };
  }
  function orderedRows(rows: SidebarConversation[], flat = false) {
    if (preferences.order !== 'manual') return rows;
    const result = manualOrder(
      rows,
      preferences.tasks[flat ? 'flat' : `repo:${rows[0]?.job.repo}`] ?? [],
      (row) => row.key,
    );
    return result.sort((a, b) => Number(Boolean(b.job.pinnedAt)) - Number(Boolean(a.job.pinnedAt)));
  }
  function taskReorder(rows: SidebarConversation[], row: SidebarConversation, flat = false) {
    // Flat ordering changes position only. Pins stay in their own partition.
    const peers = rows.filter((peer) => Boolean(peer.job.pinnedAt) === Boolean(row.job.pinnedAt));
    return reorderProps(
      flat ? 'flat' : `repo:${row.job.repo}`,
      peers.map((peer) => peer.key),
      row.key,
    );
  }
  async function archiveConversation(key: string, jobId: string) {
    setArchiving(key);
    setActionError(null);
    try {
      await archiveAgentJob(jobId);
      setHiddenThreads((old) => new Set(old).add(key));
      reload();
      reloadProjects();
      if (activeKey === key) router.replace('/');
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : t('sidebar.archiveFailed'));
    } finally {
      setArchiving(null);
    }
  }
  async function setConversationPinned(key: string, jobId: string, _repo: string, pinned: boolean) {
    setPinning(key);
    setActionError(null);
    try {
      await (pinned ? pinAgentJob(jobId) : unpinAgentJob(jobId));
      reload();
      reloadProjects();
    } catch (cause) {
      setActionError(cause instanceof Error ? cause.message : t('sidebar.pinFailed'));
    } finally {
      setPinning(null);
    }
  }
  const actions: RowActions = {
    activeKey,
    archiving,
    pinning,
    onArchive: archiveConversation,
    onSetPinned: setConversationPinned,
  };
  useEffect(() => {
    setHiddenThreads((old) => {
      const next = new Set([...old].filter((key) => !list.rows.some((row) => row.key === key)));
      return next.size === old.size ? old : next;
    });
  }, [list.rows]);
  const visible = (rows: SidebarConversation[]) =>
    rows.filter((row) => row.archived || !hiddenThreads.has(row.key));
  const flatSource =
    current && !current.archived && !list.rows.some((row) => row.key === current.key)
      ? [...list.rows, current]
      : list.rows;
  const flatRows = orderedRows(
    visible(flatSource).sort(
      (a, b) =>
        (Date.parse(b.job.pinnedAt ?? '') || 0) - (Date.parse(a.job.pinnedAt ?? '') || 0) ||
        (Date.parse(b.lastActivityAt ?? '') || 0) - (Date.parse(a.lastActivityAt ?? '') || 0),
    ),
    true,
  );
  if (collapsed) return null;
  return (
    <>
      <aside
        id="agents-sidebar"
        style={{ width: pane.width }}
        className="flex shrink-0 flex-col border-r border-gray-200/70 bg-gray-50/80 dark:border-white/10 dark:bg-gray-900/60"
      >
        <div className="flex-1 overflow-y-auto px-3.5 py-4">
          <Link
            href="/"
            className={`flex w-full items-center gap-2 rounded-lg border border-transparent px-2.5 py-2 text-[13px] font-medium transition-colors ${
              pathname === '/'
                ? 'border-crimson/10 bg-crimson/[0.06] text-crimson-dark dark:border-crimson/25 dark:bg-crimson/15 dark:text-crimson-light'
                : 'text-gray-700 hover:bg-white/80 hover:text-gray-900 dark:text-gray-300 dark:hover:bg-white/5 dark:hover:text-gray-100'
            }`}
          >
            <svg
              className={`h-4 w-4 ${pathname === '/' ? 'text-crimson dark:text-crimson-light' : 'text-gray-500 dark:text-gray-400'}`}
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M16.86 4.49a1.5 1.5 0 0 1 2.12 0l.53.53a1.5 1.5 0 0 1 0 2.12L8.53 18.12 4 19.5l1.38-4.53L16.86 4.49Z"
              />
            </svg>
            {t('sidebar.newTask')}
          </Link>

          <div className="mb-2 mt-5 flex items-center justify-between px-2 text-[13px] text-gray-500 dark:text-gray-400">
            <span>{t('sidebar.projects')}</span>
            <div className="flex items-center gap-1">
              <button
                ref={searchButton}
                type="button"
                aria-label={t('sidebar.search')}
                title={`${t('sidebar.search')} (⌘K / Ctrl K)`}
                aria-expanded={searchOpen}
                className="rounded-md p-1.5 hover:bg-gray-200/60 dark:hover:bg-white/10"
                onClick={() => {
                  if (searchOpen) closeSearch();
                  else {
                    setSearchOpen(true);
                    requestAnimationFrame(() => searchInput.current?.focus());
                  }
                }}
              >
                <svg
                  width="18"
                  height="18"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.8"
                  aria-hidden="true"
                >
                  <circle cx="10" cy="10" r="6.5" />
                  <path d="m15 15 5 5" />
                </svg>
              </button>
              <SidebarViewMenu
                value={preferences}
                onChange={(field, value) =>
                  updatePreferences((old) => {
                    if (field !== 'order' || value !== 'manual' || old.manualInitialized)
                      return { ...old, [field]: value };
                    const rows = [...knownRows.values()].sort(
                      (a, b) =>
                        (Date.parse(b.job.pinnedAt ?? '') || 0) -
                          (Date.parse(a.job.pinnedAt ?? '') || 0) ||
                        (Date.parse(b.lastActivityAt ?? '') || 0) -
                          (Date.parse(a.lastActivityAt ?? '') || 0),
                    );
                    const tasks: Record<string, string[]> = { flat: rows.map((row) => row.key) };
                    for (const row of rows) (tasks[`repo:${row.job.repo}`] ??= []).push(row.key);
                    return {
                      ...old,
                      order: 'manual',
                      manualInitialized: true,
                      projects: baseFolders.map((p) => p.repo),
                      tasks,
                    };
                  })
                }
              />
            </div>
          </div>
          {searchOpen && (
            <SearchControls
              inputRef={searchInput}
              query={query}
              onQueryChange={setQuery}
              scope={scope}
              onScopeChange={setScope}
              activeRepo={activeRepo}
              includeArchived={includeArchived}
              onIncludeArchivedChange={setIncludeArchived}
              onClose={closeSearch}
              onInputKeyDown={(event) => {
                if (['ArrowDown', 'Enter'].includes(event.key)) {
                  event.preventDefault();
                  const first =
                    resultList.current?.querySelector<HTMLAnchorElement>('[data-search-result]');
                  if (event.key === 'Enter') first?.click();
                  else first?.focus();
                }
              }}
            />
          )}
          {actionError && (
            <p role="alert" className="px-2 text-[12px] text-red-500">
              {actionError}
            </p>
          )}
          {projectsError && (
            <p role="alert" className="px-2 text-[12px] text-red-500">
              {projectsError}
              <button onClick={reloadProjects}>{t('sidebar.retry')}</button>
            </p>
          )}
          {searching ? (
            <div
              ref={resultList}
              onKeyDown={(event) => {
                if (event.key === 'Escape') {
                  closeSearch();
                  return;
                }
                if (!['ArrowDown', 'ArrowUp'].includes(event.key)) return;
                event.preventDefault();
                const items = [
                  ...resultList.current!.querySelectorAll<HTMLAnchorElement>(
                    '[data-search-result]',
                  ),
                ];
                const index = items.indexOf(document.activeElement as HTMLAnchorElement);
                const next = items[index + (event.key === 'ArrowDown' ? 1 : -1)];
                if (next) next.focus();
                else if (event.key === 'ArrowUp') searchInput.current?.focus();
              }}
            >
              {debounced === query.trim() &&
                visible(search.rows).map((row) => (
                  <TaskRow key={row.key} row={row} flat search query={debounced} {...actions} />
                ))}
              <ListStatus
                loading={search.loading || debounced !== query.trim()}
                error={search.error}
                empty={!search.rows.length}
                more={Boolean(search.cursor)}
                onMore={search.loadMore}
                onRetry={search.reload}
                search
              />
            </div>
          ) : preferences.group === 'flat' ? (
            <div className="space-y-0.5">
              {flatRows.map((row) => (
                <TaskRow
                  key={row.key}
                  row={row}
                  flat
                  {...actions}
                  reorderProps={taskReorder(flatRows, row, true)}
                />
              ))}
              <ListStatus
                loading={list.loading}
                error={list.error}
                empty={!flatRows.length}
                more={Boolean(list.cursor)}
                onMore={list.loadMore}
                onRetry={list.reload}
              />
            </div>
          ) : (
            <>
              {folders.map((folder) => (
                <ProjectFolder
                  key={folder.repo}
                  repo={folder.repo}
                  active={activeRepo === folder.repo}
                  activeCount={folder.active_count}
                  expanded={preferences.expanded[folder.repo] ?? folder.repo === defaultRepo}
                  onToggle={() =>
                    updatePreferences((old) => ({
                      ...old,
                      expanded: {
                        ...old.expanded,
                        [folder.repo]: !(old.expanded[folder.repo] ?? folder.repo === defaultRepo),
                      },
                    }))
                  }
                  initial={[]}
                  selected={current?.job.repo === folder.repo ? current : undefined}
                  userId={userId}
                  refresh={refresh}
                  {...actions}
                  hiddenThreads={hiddenThreads}
                  registerRows={registerRows}
                  orderRows={orderedRows}
                  taskReorder={taskReorder}
                  reorderProps={reorderProps(
                    'projects',
                    folders.map((p) => p.repo),
                    folder.repo,
                  )}
                />
              ))}
              {!folders.length && (
                <ListStatus
                  loading={list.loading || projectsLoading}
                  error={list.error}
                  empty
                  more={false}
                  onMore={list.loadMore}
                  onRetry={list.reload}
                />
              )}
            </>
          )}
          <span role="status" className="sr-only">
            {announcement}
          </span>
          <Link
            href="/archived"
            className={`mt-4 flex items-center gap-2 rounded-lg border border-transparent px-2.5 py-2 text-[13px] font-medium transition-colors ${
              pathname === '/archived'
                ? 'border-crimson/10 bg-crimson/[0.06] text-crimson-dark dark:border-crimson/25 dark:bg-crimson/15 dark:text-crimson-light'
                : 'text-gray-600 hover:bg-white/80 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-gray-100'
            }`}
          >
            <svg
              className="h-4 w-4"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.8}
              aria-hidden="true"
            >
              <path
                d="M4 7.5h16m-14 0V20h12V7.5M5 4h14l1 3.5H4L5 4Zm5 7h4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            {t('sidebar.archived')}
          </Link>

          <Link
            href="/integrations"
            className={`mt-0.5 flex items-center gap-2 rounded-lg border border-transparent px-2.5 py-2 text-[13px] font-medium transition-colors ${
              pathname === '/integrations'
                ? 'border-crimson/10 bg-crimson/[0.06] text-crimson-dark dark:border-crimson/25 dark:bg-crimson/15 dark:text-crimson-light'
                : 'text-gray-600 hover:bg-white/80 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-gray-100'
            }`}
          >
            <svg
              className="h-4 w-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M8 5v3m8-3v3M6.5 8h11v2.5a5.5 5.5 0 0 1-11 0V8ZM12 16v3"
              />
            </svg>
            {t('sidebar.integrations')}
          </Link>
        </div>

        <SettingsLauncher />
      </aside>
      <PaneResizer pane={pane} label={t('sidebar.resize')} controls="agents-sidebar" />
    </>
  );
}

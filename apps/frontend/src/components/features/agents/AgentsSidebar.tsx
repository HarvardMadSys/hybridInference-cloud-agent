'use client';
import Link from 'next/link';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useAuth, useT } from '@/components/providers';
import {
  useApplePlatform,
  useKeyboardShortcuts,
} from '@/components/features/settings/keyboardShortcutPreferences';
import { matchesShortcut, shortcutAriaLabel, shortcutLabel } from '@/lib/keyboardShortcuts';
import { SettingsSidebar } from '@/components/features/settings/SettingsSidebar';
import { useSidebarScroll } from './useSidebarScroll';
import { AccountMenu } from './AccountMenu';
import { archiveAgentJob, deleteAgentSessions, pinAgentJob, unpinAgentJob } from '@/lib/api/agents';
import { APIError } from '@/lib/utils/errors';
import { useAgentReads } from './AgentReadProvider';
import { ArchiveDeleteDialog, SESSION_ERROR_KEYS } from './ArchiveControls';
import { CONVERSATION_RESTORED_EVENT, type ConversationRestoredDetail } from './conversationEvents';
import { latestJob } from './conversationJob';
import { onAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { onAgentRecordsChanged, PUSH_FALLBACK_POLL_MS, useAgentPushLive } from './agentPush';
import { changesSidebarRows, SIDEBAR_ACTIVITY_REFRESH_MS } from './sidebarRecordChanges';
import { GettingStarted } from './GettingStarted';
import { PaneResizer } from './PaneResizer';
import { useAgentProjects } from './useAgentJobs';
import { announcePaneResize, useResizablePane } from './useResizablePane';
import { useSidebarPreferences, manualOrder, moveBefore } from './sidebarPreferences';
import { SidebarViewMenu } from './SidebarViewMenu';
import { SearchControls } from './SearchControls';
import { AddWorkspace } from './AddWorkspace';
import { useConversationReadState } from './useConversationReadState';
import {
  useConversationList,
  conversationRow,
  type SidebarConversation,
} from './useConversationList';
import {
  TaskRow,
  ProjectFolder,
  ListStatus,
  reconcileSidebarPins,
  type SidebarPinUpdate,
  type RowActions,
  type SidebarReorderProps,
} from './SidebarRows';
export const SIDEBAR_WIDTH_STORAGE_KEY = 'agents.sidebar.width';
export function AgentsSidebar({
  collapsed = false,
  headerAction,
  settingsNavigation,
}: {
  collapsed?: boolean;
  headerAction?: React.ReactNode;
  settingsNavigation?: { backHref: string };
}) {
  const route = usePathname() ?? '';
  const routeParams = useSearchParams();
  const active = !settingsNavigation;
  const lastLocation = useRef({ pathname: route, search: routeParams.toString() });
  if (active) lastLocation.current = { pathname: route, search: routeParams.toString() };
  const { pathname, search: queryString } = lastLocation.current;
  const params = new URLSearchParams(queryString);
  const router = useRouter();
  const t = useT();
  const { state } = useAuth();
  const userId = state.user?.id;
  const [shortcuts] = useKeyboardShortcuts(userId);
  const apple = useApplePlatform();
  const scrollRef = useSidebarScroll(active && !collapsed, userId);
  const reads = useAgentReads();
  const [preferences, updatePreferences] = useSidebarPreferences(userId);
  const { isUnread, markRead } = useConversationReadState(userId);
  const knownRows = useMemo(
    () => ({ userId, rows: new Map<string, SidebarConversation>() }),
    [userId],
  ).rows;
  const pinUpdates = useMemo(
    () => ({ userId, updates: new Map<string, SidebarPinUpdate>() }),
    [userId],
  ).updates;
  const pushedRecords = useMemo(
    () => ({ userId, shown: new Map<string, string>() }),
    [userId],
  ).shown;
  const [refresh, setRefresh] = useState(0);
  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;
  const reload = useCallback(() => {
    refreshRef.current += 1;
    setRefresh(refreshRef.current);
  }, []);
  const {
    projects,
    loading: projectsLoading,
    error: projectsError,
    reload: reloadProjects,
  } = useAgentProjects(false, active);
  const backgroundRefreshPending = useRef(false);
  const refreshInBackground = useCallback(() => {
    if (!active || document.visibilityState === 'hidden') {
      backgroundRefreshPending.current = true;
      return;
    }
    backgroundRefreshPending.current = false;
    reload();
    reloadProjects();
  }, [active, reload, reloadProjects]);

  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== 'hidden' && backgroundRefreshPending.current)
        refreshInBackground();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, [refreshInBackground]);

  const [selected, setSelected] = useState<{
    path: string;
    userId?: string;
    row: SidebarConversation;
    refresh: number;
  } | null>(null);
  const sessionGeneration = useRef(0);
  const [sessionRefresh, setSessionRefresh] = useState(0);
  const [hiddenThreads, setHiddenThreads] = useState<Set<string>>(new Set());
  const [archiving, setArchiving] = useState<string | null>(null);
  const [pinning, setPinning] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<SidebarConversation | null>(null);
  const [deletingBusy, setDeletingBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [debounced, setDebounced] = useState('');
  const [scope, setScope] = useState('all');
  const [includeArchived, setIncludeArchived] = useState(false);
  const searching = searchOpen && (query.trim() !== '' || scope !== 'all' || includeArchived);
  const list = useConversationList({
    userId,
    refresh,
    pinned: false,
    enabled: active && !searching && preferences.group === 'flat',
  });
  const pinnedList = useConversationList({
    userId,
    refresh,
    pinned: true,
    enabled: active && !searching,
  });
  const [pinnedShown, setPinnedShown] = useState(15);
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
  const selectedRow =
    selected?.path === pathname && selected.userId === userId
      ? reconcileSidebarPins([selected.row], pinUpdates, selected.refresh)[0]
      : undefined;
  const loadedCurrent =
    [...pinnedList.rows, ...list.rows].find((row) =>
      row.jobIds.some((id) => pathname === `/${id}`),
    ) ?? [...knownRows.values()].find((row) => row.jobIds.some((id) => pathname === `/${id}`));
  const current = selectedRow ?? loadedCurrent;
  const activeKey = current?.key ?? null;
  const activeRepo = pathname === '/' ? params.get('repo') : (current?.job.repo ?? null);
  const search = useConversationList({
    q: debounced,
    repo: scope === 'current' ? (activeRepo ?? undefined) : undefined,
    includeArchived,
    enabled: active && searching && debounced === query.trim(),
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
    setPinnedShown(15);
  }, [userId]);
  useEffect(
    () =>
      onAgentSessionsChanged((change) => {
        sessionGeneration.current += 1;
        setSessionRefresh(sessionGeneration.current);
        for (const id of change.threadIds) {
          knownRows.delete(id);
          pinUpdates.delete(id);
        }
        reload();
        setHiddenThreads((current) => {
          const next = new Set(current);
          for (const id of change.threadIds) {
            if (change.archived === false && !change.deleted) next.delete(id);
            else if (change.archived || change.deleted) next.add(id);
          }
          return next;
        });
      }),
    [knownRows, pinUpdates, reload],
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
    const detailRefresh = refreshRef.current;
    const generation = sessionGeneration.current;
    if (!active) return;
    setSelected(null);
    const id = /^\/([^/]+)$/.exec(pathname)?.[1];
    if (
      !id ||
      [
        'archived',
        'integrations',
        'settings',
        'connected',
        'approvals',
        'admin',
        'login',
        'setup',
      ].includes(id)
    )
      return;
    Promise.all([reads.getAgentJob(id), reads.getAgentJobThread(id)])
      .then(([job, thread]) => {
        if (cancelled || generation !== sessionGeneration.current) return;
        const latest = latestJob([job, ...(thread?.jobs ?? [])]) ?? job;
        const row = conversationRow({
          job: latest,
          title: thread?.title ?? job.title ?? job.task_prompt.split('\n')[0],
          archived: Boolean(thread?.archived_at),
          last_activity_at: thread?.updated_at ?? latest.created_at ?? null,
        });
        row.jobIds = [...new Set([id, ...(thread?.jobs.map((turn) => turn.id) ?? [])])];
        knownRows.set(row.key, row);
        setSelected({ path: pathname, userId, row, refresh: detailRefresh });
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
  }, [active, pathname, userId, updatePreferences, knownRows, sessionRefresh, reads]);
  const mountPath = useRef(pathname);
  useEffect(() => {
    if (!active || mountPath.current === pathname) return;
    mountPath.current = pathname;
    reload();
    reloadProjects();
  }, [active, pathname, reload, reloadProjects]);
  // The owner's change stream says when a conversation or turn changed; the
  // clock below is the backstop for when it is not flowing.
  const activityRefresh = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!active) return;
    const unsubscribe = onAgentRecordsChanged((change) => {
      // Tool approvals, usage, queues and publication hints belong to detail
      // panes. Only changed records or a stream gap can affect these lists.
      if (!change.resync && change.jobs.length === 0 && change.threads.length === 0) return;
      if (changesSidebarRows(change, pushedRecords)) {
        if (activityRefresh.current) clearTimeout(activityRefresh.current);
        activityRefresh.current = null;
        refreshInBackground();
        return;
      }
      // Only activity moved, which can only reorder rows: read it at most
      // once per window, however many replies land in it.
      activityRefresh.current ??= setTimeout(() => {
        activityRefresh.current = null;
        refreshInBackground();
      }, SIDEBAR_ACTIVITY_REFRESH_MS);
    });
    return () => {
      unsubscribe();
      if (activityRefresh.current) clearTimeout(activityRefresh.current);
      activityRefresh.current = null;
    };
  }, [active, pushedRecords, refreshInBackground]);
  const pushLive = useAgentPushLive();
  const hasActiveProjects = projects.some((project) => project.active_count > 0);
  useEffect(() => {
    if (!active || !hasActiveProjects) return;
    const timer = setInterval(refreshInBackground, pushLive ? PUSH_FALLBACK_POLL_MS : 10000);
    return () => clearInterval(timer);
  }, [active, hasActiveProjects, pushLive, refreshInBackground]);
  const closeSearch = useCallback(() => {
    setSearchOpen(false);
    setQuery('');
    setDebounced('');
    setScope('all');
    setIncludeArchived(false);
    searchButton.current?.focus();
  }, []);
  useEffect(() => {
    if (!active) return;
    const keydown = (event: KeyboardEvent) => {
      if (matchesShortcut(event, shortcuts.search) && !collapsed) {
        event.preventDefault();
        if (event.repeat) return;
        // Search must not pull focus out of a modal, including dialogs that
        // render through a portal outside the sidebar.
        if (
          [...document.querySelectorAll('[aria-modal="true"], dialog[open]')].some(
            (dialog) => !dialog.closest('[hidden], [aria-hidden="true"]'),
          )
        ) {
          return;
        }
        setSearchOpen(true);
        requestAnimationFrame(() => searchInput.current?.focus());
      }
    };
    document.addEventListener('keydown', keydown);
    return () => document.removeEventListener('keydown', keydown);
  }, [active, collapsed, shortcuts.search]);
  const registerRows = useCallback(
    (sourceRows: SidebarConversation[], loadedRefresh: number | null) => {
      const rows = reconcileSidebarPins(sourceRows, pinUpdates, loadedRefresh);
      if (!rows.length) return;
      rows.forEach((row) => {
        const previous = knownRows.get(row.key);
        knownRows.set(row.key, {
          ...row,
          jobIds: [...new Set([...(previous?.jobIds ?? []), ...row.jobIds])],
        });
      });
      setSelected((old) => {
        if (!old || loadedRefresh === null || loadedRefresh < old.refresh) return old;
        const row = rows.find((item) => item.key === old.row.key);
        if (!row) return old;
        return {
          ...old,
          row: { ...row, jobIds: [...new Set([...old.row.jobIds, ...row.jobIds])] },
          refresh: loadedRefresh,
        };
      });
      updatePreferences((old) => {
        if (!old.manualInitialized && old.order !== 'manual') return old;
        const tasks = { ...old.tasks };
        let changed = false;
        for (const row of rows)
          for (const group of row.job.pinnedAt ? ['pinned'] : ['flat', `repo:${row.job.repo}`]) {
            const ids = tasks[group] ?? [];
            if (!ids.includes(row.key)) {
              tasks[group] = [...ids, row.key];
              changed = true;
            }
          }
        return changed ? { ...old, tasks } : old;
      });
    },
    [updatePreferences, knownRows, pinUpdates],
  );
  useEffect(() => {
    registerRows(list.rows, list.loadedRefresh);
  }, [list.rows, list.loadedRefresh, registerRows]);
  useEffect(() => {
    registerRows(pinnedList.rows, pinnedList.loadedRefresh);
  }, [pinnedList.rows, pinnedList.loadedRefresh, registerRows]);
  const baseFolders = useMemo(() => {
    const repos = new Map(projects.map((project) => [project.repo, project]));
    for (const repo of preferences.addedProjects)
      if (!repos.has(repo))
        repos.set(repo, { repo, task_count: 0, active_count: 0, last_activity_at: null });
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
  }, [projects, list.rows, activeRepo, preferences.group, preferences.addedProjects]);
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
        pointerAllowsDrag.current = !(event.target as Element).closest(
          'button:not([data-reorder-project]), [data-row-action]',
        );
      },
      onDragStart: (event) => {
        if (
          !pointerAllowsDrag.current ||
          (event.target as Element).closest('button:not([data-reorder-project]), [data-row-action]')
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
  function orderedRows(rows: SidebarConversation[], group = `repo:${rows[0]?.job.repo}`) {
    if (preferences.order !== 'manual') return rows;
    const result = manualOrder(rows, preferences.tasks[group] ?? [], (row) => row.key);
    return result;
  }
  function taskReorder(
    rows: SidebarConversation[],
    row: SidebarConversation,
    group = `repo:${row.job.repo}`,
  ) {
    return reorderProps(
      group,
      rows.map((peer) => peer.key),
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
  function requestDelete(row: SidebarConversation) {
    setDeleteError(null);
    setPendingDelete(row);
  }
  async function confirmDelete() {
    if (!pendingDelete) return;
    const { key } = pendingDelete;
    setDeletingBusy(true);
    setDeleteError(null);
    try {
      await deleteAgentSessions([key]);
      setHiddenThreads((old) => new Set(old).add(key));
      setPendingDelete(null);
      reload();
      reloadProjects();
      if (activeKey === key) router.replace('/');
    } catch (cause) {
      const code = cause instanceof APIError ? cause.code : undefined;
      const reason = SESSION_ERROR_KEYS[code as keyof typeof SESSION_ERROR_KEYS];
      setDeleteError(
        reason ? t(reason) : cause instanceof Error ? cause.message : t('archive.deleteFailed'),
      );
    } finally {
      setDeletingBusy(false);
    }
  }
  async function setConversationPinned(key: string, jobId: string, _repo: string, pinned: boolean) {
    setPinning(key);
    setActionError(null);
    try {
      const result = await (pinned ? pinAgentJob(jobId) : unpinAgentJob(jobId));
      const previous = knownRows.get(key) ?? search.rows.find((row) => row.key === key) ?? current;
      if (previous?.key === key) {
        const row = {
          ...previous,
          job: {
            ...previous.job,
            pinnedAt: pinned ? (result?.pinned_at ?? new Date().toISOString()) : undefined,
          },
        };
        const revision = refreshRef.current + 1;
        pinUpdates.set(key, { row, refresh: revision });
        knownRows.set(key, row);
        setSelected((old) => (old?.row.key === key ? { ...old, row, refresh: revision } : old));
      }
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
    isUnread,
    onRead: markRead,
    archiving,
    pinning,
    deleting: pendingDelete?.key ?? null,
    onArchive: archiveConversation,
    onSetPinned: setConversationPinned,
    onDelete: requestDelete,
  };
  const visible = (rows: SidebarConversation[]) =>
    rows.filter((row) => row.archived || !hiddenThreads.has(row.key));
  const flatSource = reconcileSidebarPins(list.rows, pinUpdates, list.loadedRefresh, true);
  if (current && !current.archived && !flatSource.some((row) => row.key === current.key))
    flatSource.push(current);
  const flatRows = orderedRows(
    visible(flatSource)
      .filter((row) => !row.job.pinnedAt && !row.archived)
      .sort(
        (a, b) =>
          (Date.parse(b.lastActivityAt ?? '') || 0) - (Date.parse(a.lastActivityAt ?? '') || 0),
      ),
    'flat',
  );
  const pinnedSource = reconcileSidebarPins(
    pinnedList.rows,
    pinUpdates,
    pinnedList.loadedRefresh,
    true,
  );
  if (
    current?.job.pinnedAt &&
    !current.archived &&
    !pinnedSource.some((row) => row.key === current.key)
  )
    pinnedSource.push(current);
  const pinnedRows = orderedRows(
    visible(pinnedSource)
      .filter((row) => row.job.pinnedAt && !row.archived)
      .sort(
        (a, b) =>
          (Date.parse(b.job.pinnedAt ?? '') || 0) - (Date.parse(a.job.pinnedAt ?? '') || 0) ||
          (Date.parse(b.lastActivityAt ?? '') || 0) - (Date.parse(a.lastActivityAt ?? '') || 0),
      ),
    'pinned',
  );
  const pinnedSelectedIndex = pinnedRows.findIndex((row) => row.key === activeKey);
  const visiblePinned = pinnedRows.slice(0, Math.max(pinnedShown, pinnedSelectedIndex + 1));
  if (collapsed) return null;
  return (
    <>
      {settingsNavigation && (
        <SettingsSidebar
          collapsed={collapsed}
          headerAction={headerAction}
          pane={pane}
          backHref={settingsNavigation.backHref}
        />
      )}
      <aside
        id={active ? 'agents-sidebar' : undefined}
        hidden={!active}
        style={{ width: pane.width }}
        className={`${active ? 'ui-enter flex' : 'hidden'} min-h-0 shrink-0 flex-col border-r border-border/70 bg-surface-muted`}
      >
        <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
          <div className="flex items-center gap-1.5">
            <Link
              href="/"
              aria-current={pathname === '/' ? 'page' : undefined}
              className={`ui-focus-ring flex min-h-8 min-w-0 flex-1 items-center gap-2 rounded-lg border px-2.5 py-1.5 text-[13px] font-semibold transition-colors [@media(pointer:coarse)]:min-h-11 ${
                pathname === '/'
                  ? 'border-border bg-surface text-foreground shadow-subtle'
                  : 'border-border/70 bg-surface text-foreground hover:border-border-strong/50 hover:bg-surface-hover'
              }`}
            >
              <svg
                aria-hidden="true"
                className="h-4 w-4 shrink-0 text-accent"
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
            {active && headerAction}
          </div>

          {!searching && (pinnedRows.length > 0 || pinnedList.error) && (
            <section aria-labelledby="sidebar-pinned-heading" className="mt-4">
              <h2
                id="sidebar-pinned-heading"
                className="mb-1 px-2 text-[11px] font-medium text-muted"
              >
                {t('sidebar.pinned')}
              </h2>
              <div className="space-y-1">
                {(active ? visiblePinned : []).map((row) => (
                  <TaskRow
                    key={row.key}
                    row={row}
                    flat
                    {...actions}
                    reorderProps={taskReorder(pinnedRows, row, 'pinned')}
                  />
                ))}
                <ListStatus
                  loading={pinnedList.showLoading}
                  error={pinnedList.error}
                  empty={false}
                  more={pinnedRows.length > visiblePinned.length || Boolean(pinnedList.cursor)}
                  onMore={() => {
                    setPinnedShown((value) => value + 15);
                    if (pinnedRows.length <= visiblePinned.length + 15) pinnedList.loadMore();
                  }}
                  onRetry={pinnedList.reload}
                />
              </div>
            </section>
          )}

          <div className="mb-1 mt-4 flex flex-wrap items-center justify-between gap-x-2 gap-y-1 pl-2 pr-0.5">
            <span className="text-[11px] font-medium text-muted">{t('sidebar.projects')}</span>
            <div className="ml-auto flex items-center gap-0.5">
              <button
                ref={searchButton}
                type="button"
                aria-label={t('sidebar.search')}
                title={`${t('sidebar.search')}${shortcuts.search ? ` (${shortcutLabel(shortcuts.search, apple)})` : ''}`}
                aria-keyshortcuts={shortcutAriaLabel(shortcuts.search, apple)}
                aria-expanded={searchOpen}
                className="ui-focus-ring flex h-8 w-8 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-hover hover:text-foreground [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11"
                onClick={() => {
                  if (searchOpen) closeSearch();
                  else {
                    setSearchOpen(true);
                    requestAnimationFrame(() => searchInput.current?.focus());
                  }
                }}
              >
                <svg
                  width="16"
                  height="16"
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
                    const tasks: Record<string, string[]> = {
                      flat: rows.filter((row) => !row.job.pinnedAt).map((row) => row.key),
                      pinned: rows.filter((row) => row.job.pinnedAt).map((row) => row.key),
                    };
                    for (const row of rows)
                      if (!row.job.pinnedAt) (tasks[`repo:${row.job.repo}`] ??= []).push(row.key);
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
              <AddWorkspace
                userId={userId}
                existing={baseFolders.map((folder) => folder.repo)}
                onSelect={(repo) => {
                  closeSearch();
                  updatePreferences((old) => ({
                    ...old,
                    group: 'project',
                    addedProjects: [...new Set([...old.addedProjects, repo])],
                    expanded: { ...old.expanded, [repo]: true },
                  }));
                }}
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
            <p role="alert" className="px-2 text-[12px] text-status-error">
              {actionError}
            </p>
          )}
          {projectsError && (
            <p role="alert" className="px-2 text-[12px] text-status-error">
              {projectsError}
              <button onClick={reloadProjects}>{t('sidebar.retry')}</button>
            </p>
          )}
          {searching ? (
            <div
              ref={resultList}
              onKeyDown={(event) => {
                if (event.key === 'Escape') {
                  event.preventDefault();
                  event.stopPropagation();
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
              {active &&
                debounced === query.trim() &&
                visible(reconcileSidebarPins(search.rows, pinUpdates, search.loadedRefresh)).map(
                  (row) => (
                    <TaskRow key={row.key} row={row} flat search query={debounced} {...actions} />
                  ),
                )}
              <ListStatus
                loading={search.showLoading || debounced !== query.trim()}
                error={search.error}
                empty={!search.rows.length}
                more={Boolean(search.cursor)}
                onMore={search.loadMore}
                onRetry={search.reload}
                search
              />
            </div>
          ) : preferences.group === 'flat' ? (
            <div className="space-y-1">
              {(active ? flatRows : []).map((row) => (
                <TaskRow
                  key={row.key}
                  row={row}
                  flat
                  {...actions}
                  reorderProps={taskReorder(flatRows, row, 'flat')}
                />
              ))}
              <ListStatus
                loading={list.showLoading}
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
                  enabled={active}
                  selected={current?.job.repo === folder.repo ? current : undefined}
                  userId={userId}
                  refresh={refresh}
                  {...actions}
                  hiddenThreads={hiddenThreads}
                  pinUpdates={pinUpdates}
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
                  loading={list.showLoading || projectsLoading}
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
        </div>

        {active && <GettingStarted />}

        <div className="shrink-0 space-y-0.5 border-t border-border/70 px-2 py-2">
          <Link
            href="/archived"
            aria-current={pathname === '/archived' ? 'page' : undefined}
            className={`ui-focus-ring flex min-h-8 items-center gap-2 rounded-lg px-2.5 py-1.5 text-[13px] font-medium transition-colors [@media(pointer:coarse)]:min-h-11 ${
              pathname === '/archived'
                ? 'bg-surface-hover text-foreground'
                : 'text-muted hover:bg-surface-hover hover:text-foreground'
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

          <div className="mt-1 border-t border-border/70 pt-1.5">
            {active && <AccountMenu variant="sidebar" />}
          </div>
        </div>
      </aside>
      {active && <PaneResizer pane={pane} label={t('sidebar.resize')} controls="agents-sidebar" />}
      {active && pendingDelete ? (
        <ArchiveDeleteDialog
          count={1}
          skipped={0}
          scope={t('archive.scopeSession', { title: pendingDelete.job.title })}
          titles={[]}
          busy={deletingBusy}
          limitExceeded={false}
          error={deleteError}
          onCancel={() => setPendingDelete(null)}
          onConfirm={() => void confirmDelete()}
        />
      ) : null}
    </>
  );
}

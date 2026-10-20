'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { onAgentRecordsChanged, PUSH_FALLBACK_POLL_MS, useAgentPushLive } from './agentPush';
import { changesSidebarRows, SIDEBAR_ACTIVITY_REFRESH_MS } from './sidebarRecordChanges';
import { useAgentProjects } from './useAgentJobs';

/**
 * When the sidebar reads its conversation lists and projects again.
 *
 * `refresh` counts the reads: the lists load again whenever it changes, and a
 * row read on its own remembers the count it was read at, so an older list
 * load cannot replace it. `reload()` reads now.
 *
 * The rest happens in the background: when the owner's change stream reports a
 * record the lists show, at most once a window when only activity moved, and on
 * a timer while a project has a turn running. A background read waits while the
 * tab is hidden or the sidebar is not the active one, and runs once the tab is
 * visible again.
 */
export function useSidebarRefresh(userId: string | undefined, active: boolean) {
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
  return { refresh, refreshRef, reload, projects, projectsLoading, projectsError, reloadProjects };
}

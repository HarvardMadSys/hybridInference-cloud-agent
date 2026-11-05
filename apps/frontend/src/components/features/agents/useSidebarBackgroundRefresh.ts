import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * The sidebar's refreshes that nobody asked for: after a pushed change, on the
 * fallback poll, and once a hidden page is visible again.
 *
 * A hidden or inactive sidebar only notes that it owes a refresh, and pays it
 * in full once visible. `reorderInBackground` is for a change that only moved
 * activity, which can only lift rows to the top: `activityMoved` counts those,
 * and a list given it reads its first page instead of every loaded one.
 */
export function useSidebarBackgroundRefresh(
  active: boolean,
  reload: () => void,
  reloadProjects: () => void,
) {
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
  const [activityMoved, setActivityMoved] = useState(0);
  const reorderInBackground = useCallback(() => {
    if (!active || document.visibilityState === 'hidden') {
      backgroundRefreshPending.current = true;
      return;
    }
    setActivityMoved((value) => value + 1);
    reloadProjects();
  }, [active, reloadProjects]);

  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== 'hidden' && backgroundRefreshPending.current)
        refreshInBackground();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, [refreshInBackground]);
  return { refreshInBackground, reorderInBackground, activityMoved };
}

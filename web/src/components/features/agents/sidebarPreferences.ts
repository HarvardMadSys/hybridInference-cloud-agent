import { useCallback, useEffect, useState } from 'react';

export interface SidebarPreferences {
  group: 'project' | 'flat';
  order: 'manual' | 'updated';
  manualInitialized: boolean;
  projects: string[];
  tasks: Record<string, string[]>;
  expanded: Record<string, boolean>;
}
export const DEFAULT_PREFERENCES: SidebarPreferences = {
  group: 'project',
  order: 'updated',
  manualInitialized: false,
  projects: [],
  tasks: {},
  expanded: {},
};
export const sidebarPreferencesKey = (userId: string) =>
  `agents.sidebar.navigation.${encodeURIComponent(userId)}`;
const strings = (value: unknown): string[] =>
  Array.isArray(value) ? [...new Set(value.filter((v): v is string => typeof v === 'string'))] : [];
export function readSidebarPreferences(userId: string): SidebarPreferences {
  try {
    const value = JSON.parse(localStorage.getItem(sidebarPreferencesKey(userId)) ?? 'null');
    if (!value || typeof value !== 'object' || Array.isArray(value)) return DEFAULT_PREFERENCES;
    return {
      group: value.group === 'flat' ? 'flat' : 'project',
      order: value.order === 'manual' ? 'manual' : 'updated',
      manualInitialized: value.manualInitialized === true || value.order === 'manual',
      projects: strings(value.projects),
      tasks: Object.fromEntries(
        Object.entries(value.tasks ?? {}).map(([key, list]) => [key, strings(list)]),
      ),
      expanded: Object.fromEntries(
        Object.entries(value.expanded ?? {}).filter(([, v]) => typeof v === 'boolean'),
      ) as Record<string, boolean>,
    };
  } catch {
    return DEFAULT_PREFERENCES;
  }
}
export function useSidebarPreferences(userId: string | undefined) {
  const [saved, setSaved] = useState<{ userId?: string; value: SidebarPreferences }>({
    value: DEFAULT_PREFERENCES,
  });
  useEffect(() => {
    setSaved({ userId, value: userId ? readSidebarPreferences(userId) : DEFAULT_PREFERENCES });
  }, [userId]);
  const value = saved.userId === userId ? saved.value : DEFAULT_PREFERENCES;
  const update = useCallback(
    (change: (old: SidebarPreferences) => SidebarPreferences) => {
      setSaved((current) => {
        const next = change(current.userId === userId ? current.value : DEFAULT_PREFERENCES);
        if (current.userId === userId && next === current.value) return current;
        if (userId) {
          try {
            localStorage.setItem(sidebarPreferencesKey(userId), JSON.stringify(next));
          } catch {
            /* Storage is optional. */
          }
        }
        return { userId, value: next };
      });
    },
    [userId],
  );
  return [value, update] as const;
}
/** Keep remembered order stable as new rows appear; unknown rows append. */
export function manualOrder<T>(items: T[], saved: string[], key: (item: T) => string): T[] {
  const ranks = new Map(saved.map((id, index) => [id, index]));
  return [...items].sort(
    (a, b) => (ranks.get(key(a)) ?? Infinity) - (ranks.get(key(b)) ?? Infinity),
  );
}
/** Move within the supplied peer list; callers partition tasks by repo and pin. */
export function moveBefore(ids: string[], source: string, target: string): string[] {
  if (source === target || !ids.includes(source) || !ids.includes(target)) return ids;
  const next = ids.filter((id) => id !== source);
  next.splice(next.indexOf(target), 0, source);
  return next;
}

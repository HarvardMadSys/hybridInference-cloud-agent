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
    const manualInitialized = value.manualInitialized === true || value.order === 'manual';
    const tasks = Object.fromEntries(
      Object.entries(manualInitialized ? (value.tasks ?? {}) : {}).map(([key, list]) => [
        key,
        strings(list),
      ]),
    );
    return {
      group: value.group === 'flat' ? 'flat' : 'project',
      order: value.order === 'manual' ? 'manual' : 'updated',
      manualInitialized,
      projects: manualInitialized ? strings(value.projects) : [],
      tasks,
      expanded: Object.fromEntries(
        Object.entries(value.expanded ?? {}).filter(([, v]) => typeof v === 'boolean'),
      ) as Record<string, boolean>,
    };
  } catch {
    return DEFAULT_PREFERENCES;
  }
}
function serializedPreferences(value: SidebarPreferences): string {
  if (value.manualInitialized || value.order === 'manual') return JSON.stringify(value);
  const { projects: _projects, tasks: _tasks, ...display } = value;
  return JSON.stringify(display);
}
export function useSidebarPreferences(userId: string | undefined) {
  const [saved, setSaved] = useState<{ userId?: string; value: SidebarPreferences }>({
    value: DEFAULT_PREFERENCES,
  });
  useEffect(() => {
    const value = userId ? readSidebarPreferences(userId) : DEFAULT_PREFERENCES;
    try {
      // The former shared key has no account owner, so it must never be migrated.
      localStorage.removeItem('agents.sidebar.projects');
      if (userId && localStorage.getItem(sidebarPreferencesKey(userId)) !== null) {
        // Remove any history IDs previously recorded before manual order was enabled.
        localStorage.setItem(sidebarPreferencesKey(userId), serializedPreferences(value));
      }
    } catch {
      /* Blocked storage must not prevent navigation. */
    }
    setSaved({ userId, value });
  }, [userId]);
  const value = saved.userId === userId ? saved.value : DEFAULT_PREFERENCES;
  const update = useCallback(
    (change: (old: SidebarPreferences) => SidebarPreferences) => {
      setSaved((current) => {
        const next = change(current.userId === userId ? current.value : DEFAULT_PREFERENCES);
        if (current.userId === userId && next === current.value) return current;
        if (userId) {
          try {
            localStorage.setItem(sidebarPreferencesKey(userId), serializedPreferences(next));
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

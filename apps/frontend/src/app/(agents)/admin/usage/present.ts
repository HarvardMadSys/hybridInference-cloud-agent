// Which turns the Admin Usage page reads, and where its links lead: the
// window, the selection a URL carries, and the Activity lists a number opens.
// How the numbers themselves read is in `dashboard.ts`.
//
// Separate from `page.tsx` because Next type-checks a page's exports, and
// tested directly rather than only through the rendered page.

import type { AdminUsageFilters } from '@/lib/api/adminUsage';

/** The longest identity an exact filter accepts; longer keys cannot be linked. */
export const MAX_FILTER_LENGTH = 256;

export interface UsagePreset {
  key: string;
  label: string;
  days: number;
}

export const PRESETS: UsagePreset[] = [
  { key: '7', label: 'Last 7 days', days: 7 },
  { key: '30', label: 'Last 30 days', days: 30 },
  { key: '90', label: 'Last 90 days', days: 90 },
];

const DAY_MS = 86_400_000;
//: The identity filters this page offers, in form order.
export const IDENTITY_KEYS = ['user_id', 'model', 'runtime', 'repo'] as const;
const WINDOW_KEYS = ['since', 'until'] as const;
const QUERY_KEYS = [...WINDOW_KEYS, ...IDENTITY_KEYS] as const;

/**
 * Resolve a window from an instant.
 *
 * The result is absolute: later reads reuse the same two instants instead of
 * drifting with the clock, so a copied link and a refresh agree.
 */
export function windowFor(days: number, now: Date = new Date()): AdminUsageFilters {
  return {
    since: new Date(now.getTime() - days * DAY_MS).toISOString(),
    until: now.toISOString(),
  };
}

export interface QuerySelection {
  /** The selection to read, or null when the URL asked for nothing usable. */
  filters: AdminUsageFilters | null;
  /** Why the URL could not be used, when it carried something this page cannot. */
  error: string | null;
  /** True when the URL named a selection, usable or not. */
  linked: boolean;
}

/**
 * Read a selection out of a URL.
 *
 * A link carrying an unusable window is an error, not an empty selection:
 * falling back to a default window would answer for a different cohort than the
 * one that was linked, and dropping an unknown parameter would silently widen
 * it. Only a URL with no selection at all falls back.
 */
export function selectionFromQuery(query: string): QuerySelection {
  const params = new URLSearchParams(query);
  const unknown = [...params.keys()].filter(
    (key) => !(QUERY_KEYS as readonly string[]).includes(key),
  );
  if (unknown.length) {
    return { filters: null, error: `Unsupported filter: ${unknown[0]}`, linked: true };
  }
  const filters: AdminUsageFilters = {};
  for (const key of QUERY_KEYS) {
    const value = params.get(key) ?? undefined;
    if (value) filters[key] = value;
  }
  const named = Object.keys(filters).length > 0;
  if (!named) return { filters: null, error: null, linked: false };
  if (!filters.since || !filters.until) {
    return { filters: null, error: 'This link is missing one end of its window.', linked: true };
  }
  if (!(Date.parse(filters.since) < Date.parse(filters.until))) {
    return { filters: null, error: 'This link has an empty or inverted window.', linked: true };
  }
  return { filters, error: null, linked: true };
}

//: How stale a window's end may be and still count as the preset it looks like.
const PRESET_TOLERANCE_MS = 5 * 60_000;

/**
 * Which preset, if any, the applied window is.
 *
 * The window is the authority, not the option the admin last clicked. A linked
 * one-day window must not read "Last 7 days", because changing another filter
 * would then re-anchor it to seven days without anyone asking. A window that
 * merely has the same length as a preset is still custom: a week ending last
 * Tuesday is not the last seven days, and re-anchoring it would silently move
 * the selection.
 */
export function presetKey(filters: AdminUsageFilters, now: Date = new Date()): string {
  const since = filters.since;
  const until = filters.until;
  if (!since || !until) return 'custom';
  const span = Date.parse(until) - Date.parse(since);
  for (const preset of PRESETS) {
    if (Math.abs(span - preset.days * DAY_MS) >= 1000) continue;
    if (Math.abs(now.getTime() - Date.parse(until)) < PRESET_TOLERANCE_MS) return preset.key;
  }
  return 'custom';
}

export interface RunsTarget {
  dimension: 'user_id' | 'model' | 'day';
  value?: string | null;
  since?: string;
  until?: string;
}

/** A selection as query parameters, leaving out every empty filter. */
function selectionParams(filters: AdminUsageFilters): URLSearchParams {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value);
  }
  return params;
}

/** Where this page writes its own selection back into the address bar. */
export function urlFor(pathname: string, filters: AdminUsageFilters): string {
  // The current pathname, not a hardcoded one: a deployment mounted under a
  // base path must keep its prefix, which native history does not add.
  return `${pathname}?${selectionParams(filters)}`;
}

/** The Activity list for this whole selection: its window and every identity. */
export function activityHref(filters: AdminUsageFilters): string {
  return `/admin/activity?${selectionParams(filters)}`;
}

/**
 * Link to the Activity list for one day, user or model.
 *
 * Every applied filter is kept, so the linked list is the same cohort the
 * number was aggregated from. A key the Activity filter cannot express returns
 * null rather than a link that would answer with a different, wider set.
 */
export function runsHref(filters: AdminUsageFilters, row: RunsTarget): string | null {
  const params = selectionParams(filters);
  if (row.dimension === 'day') {
    if (!row.since || !row.until) return null;
    params.set('since', row.since);
    params.set('until', row.until);
  } else {
    const value = row.value;
    if (!value || value.length > MAX_FILTER_LENGTH) return null;
    params.set(row.dimension, value);
  }
  return `/admin/activity?${params}`;
}

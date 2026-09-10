// Pure presentation for the Admin Usage page.
//
// Separate from `page.tsx` because Next type-checks a page's exports, and
// tested directly rather than only through the rendered table.

import type {
  AdminUsageAggregate,
  AdminUsageFilters,
  AdminUsageGroup,
  AdminUsageUnknownRows,
} from '@/lib/api/adminUsage';

import { money, number } from '../runs/shared';

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

/**
 * Link to the runs list for one breakdown row.
 *
 * Every applied filter is kept, so the linked list is the same cohort the row
 * was aggregated from. A key the runs filter cannot express returns null rather
 * than a link that would answer with a different, wider set.
 */
/** The identity filters an apply should carry, read from the submitted form. */
export function identityFromForm(
  read: (key: (typeof IDENTITY_KEYS)[number]) => string,
): AdminUsageFilters {
  const filters: AdminUsageFilters = {};
  for (const key of IDENTITY_KEYS) {
    // A cleared field is absent from the next request, not the previous value.
    const value = read(key).trim();
    if (value) filters[key] = value;
  }
  return filters;
}

/** Where this page writes its own selection back into the address bar. */
export function urlFor(pathname: string, filters: AdminUsageFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value);
  }
  // The current pathname, not a hardcoded one: a deployment mounted under a
  // base path must keep its prefix, which native history does not add.
  return `${pathname}?${params}`;
}

export function runsHref(filters: AdminUsageFilters, row: RunsTarget): string | null {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) params.set(key, value);
  }
  if (row.dimension === 'day') {
    if (!row.since || !row.until) return null;
    params.set('since', row.since);
    params.set('until', row.until);
  } else {
    const value = row.value;
    if (!value || value.length > MAX_FILTER_LENGTH) return null;
    params.set(row.dimension, value);
  }
  return `/admin/runs?${params}`;
}

export interface UsageMetric {
  label: string;
  value: string;
  detail: string | null;
  exact: string | null;
}

/** The four headline numbers, each carrying its own missing-data note. */
export function headline(aggregate: AdminUsageAggregate): UsageMetric[] {
  const totals = aggregate.totals;
  const unknown = aggregate.unknown_rows;
  const cost = money(totals?.spent_usd);
  return [
    {
      label: 'Recorded cost',
      value: cost.display,
      detail: unknown?.spent_usd
        ? `${number(unknown.spent_usd)} requests without a recorded cost`
        : null,
      exact: cost.exact,
    },
    {
      label: 'Model calls',
      value: totals ? number(totals.calls) : 'Unavailable',
      // A call count is known even when one of its metrics is not, so the
      // missing values are stated here rather than hidden behind the number.
      detail: callDetail(aggregate, unknown),
      exact: null,
    },
    {
      label: 'Input tokens',
      value: number(totals?.tokens_in),
      detail: unknown?.tokens_in ? `${number(unknown.tokens_in)} requests unknown` : null,
      exact: null,
    },
    {
      label: 'Output tokens',
      value: number(totals?.tokens_out),
      detail: unknown?.tokens_out ? `${number(unknown.tokens_out)} requests unknown` : null,
      exact: null,
    },
  ];
}

function callDetail(
  aggregate: AdminUsageAggregate,
  unknown: AdminUsageUnknownRows | null,
): string | null {
  const missing = unknown ? Object.values(unknown).reduce((total, count) => total + count, 0) : 0;
  const notes: string[] = [];
  if (aggregate.estimated_calls) notes.push(`${number(aggregate.estimated_calls)} estimated`);
  if (missing) notes.push(`${number(missing)} recordings with an unreported value`);
  return notes.length ? notes.join(' · ') : null;
}

/** The one sentence a coverage number must never be read without. */
export const COVERAGE_BASIS =
  'Runs with at least one valid snapshot. A zero-call snapshot counts, and a run with some attempts missing still counts. This is not a completeness rate for the ledger.';

export const WINDOW_BASIS =
  'Usage recorded for runs created in this period. Includes their full cached usage. Dates use UTC.';

export const CACHE_NOTICE = 'Cached usage; active runs may not be counted yet.';

export function coverageSummary(aggregate: AdminUsageAggregate): string {
  if (aggregate.run_count === 0) return 'No runs in this period';
  return `${number(aggregate.runs_with_snapshot)} of ${number(
    aggregate.run_count,
  )} runs have a usage snapshot`;
}

/** Status lines worth showing, with the runs that ended in each state. */
export function statusLines(aggregate: AdminUsageAggregate): { label: string; count: number }[] {
  const labels: Record<string, string> = {
    partial: 'Partial snapshot',
    stale: 'Last refresh failed',
    pending: 'No snapshot yet',
    unavailable: 'No snapshot',
  };
  return Object.entries(labels)
    .map(([status, label]) => ({ label, count: aggregate.status_counts[status as never] ?? 0 }))
    .filter((line) => line.count > 0);
}

/** The breakdown a group dimension belongs to. */
export function breakdownKey(dimension: 'user_id' | 'model'): 'by_user' | 'by_model' {
  return dimension === 'user_id' ? 'by_user' : 'by_model';
}

/** A group's display label, never inventing an identity for an unknown one. */
export function groupLabel(dimension: 'user_id' | 'model' | 'day', group: AdminUsageGroup): string {
  if (group.key === null) return 'Unknown configuration';
  if (dimension === 'day') return group.key;
  return group.key;
}

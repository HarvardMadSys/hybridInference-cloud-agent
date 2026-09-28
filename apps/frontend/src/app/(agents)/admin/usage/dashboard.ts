// How the Admin Usage page's numbers read: shortened for a headline, compared
// with the window before, and qualified when they are not final. Which turns
// they count and where their links lead is in `present.ts`.
//
// Separate from `page.tsx` because Next type-checks a page's exports, and
// tested directly rather than only through the rendered page.

import type { AdminUsageAggregate, AdminUsageDay, AdminUsageFilters } from '@/lib/api/adminUsage';

import { money } from '../runs/shared';

const DAY_MS = 86_400_000;

export type UsageMetric = 'cost' | 'turns' | 'calls' | 'tokens_in' | 'tokens_out';

/** How the toolbar names each identity a selection can be narrowed to. */
export const IDENTITY_LABELS = {
  user_id: 'User',
  model: 'Model',
  runtime: 'Agent',
  repo: 'Repository',
} as const;

/**
 * One metric of an aggregate as a number.
 *
 * Null means the metric was not recorded, never zero: a turn without a usage
 * snapshot has no known cost, and adding it as $0 would understate the total.
 */
export function metricValue(aggregate: AdminUsageAggregate, metric: UsageMetric): number | null {
  if (metric === 'turns') return aggregate.run_count;
  const totals = aggregate.totals;
  if (!totals) return null;
  if (metric === 'calls') return totals.calls;
  if (metric === 'cost') {
    const amount = totals.spent_usd == null ? Number.NaN : Number(totals.spent_usd);
    return Number.isFinite(amount) ? amount : null;
  }
  return totals[metric] ?? null;
}

const COMPACT = new Intl.NumberFormat('en-US', {
  notation: 'compact',
  maximumSignificantDigits: 3,
});

/** A count short enough for a headline: 9,907 stays whole, 397,212,004 reads 397M. */
export function compactCount(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return Math.abs(value) < 10_000 ? value.toLocaleString('en-US') : COMPACT.format(value);
}

/** A cost to the cent. The exact recorded amount is kept for a tooltip. */
export function costDisplay(value: string | null | undefined): {
  display: string;
  exact: string | null;
} {
  const { exact } = money(value);
  if (exact === null) return { display: '—', exact: null };
  const amount = Number(exact);
  if (amount === 0) return { display: '$0', exact };
  if (amount < 0.01) return { display: '<$0.01', exact };
  return {
    display: `$${amount.toLocaleString('en-US', {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`,
    exact,
  };
}

/** A metric's value as the headline shows it, with the exact figure when it was shortened. */
export function metricDisplay(
  aggregate: AdminUsageAggregate,
  metric: UsageMetric,
): { display: string; exact: string | null } {
  if (metric === 'cost') return costDisplay(aggregate.totals?.spent_usd);
  const value = metricValue(aggregate, metric);
  const display = compactCount(value);
  const whole = value === null ? null : value.toLocaleString('en-US');
  return { display, exact: whole !== display ? whole : null };
}

/** A chart value: a cost to the cent, a count as the headline shortens it. */
export function formatMetric(metric: UsageMetric, value: number | null): string {
  if (value === null) return '—';
  if (metric !== 'cost') return compactCount(value);
  if (value === 0) return '$0';
  if (value < 0.01) return '<$0.01';
  return `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/**
 * The same selection one window earlier: equally long, ending where this one
 * starts, with the same identities. Null for a window that cannot be read.
 */
export function previousWindow(filters: AdminUsageFilters): AdminUsageFilters | null {
  const since = Date.parse(filters.since ?? '');
  const until = Date.parse(filters.until ?? '');
  if (!Number.isFinite(since) || !Number.isFinite(until) || !(since < until)) return null;
  return {
    ...filters,
    since: new Date(since - (until - since)).toISOString(),
    until: new Date(since).toISOString(),
  };
}

/** How a tooltip names the previous window: `previous 7 days`, or `previous window`. */
export function previousLabel(filters: AdminUsageFilters): string {
  const span = Date.parse(filters.until ?? '') - Date.parse(filters.since ?? '');
  const days = span / DAY_MS;
  if (!Number.isInteger(days) || days < 1) return 'previous window';
  return days === 1 ? 'previous day' : `previous ${days} days`;
}

/**
 * The change from the previous window, as `+18%` or `−5%`.
 *
 * Null when there is nothing to measure against: an unrecorded value on either
 * side, or a previous value of zero, which no percentage describes.
 */
export function change(current: number | null, previous: number | null): string | null {
  if (current === null || previous === null || previous <= 0) return null;
  const percent = Math.round(((current - previous) / previous) * 100);
  if (percent === 0) return '0%';
  return `${percent > 0 ? '+' : '−'}${Math.abs(percent).toLocaleString('en-US')}%`;
}

/**
 * The share of input tokens read from the cache.
 *
 * Only a ratio both sides fully report is shown: a request that did not report
 * one of the two would make it a guess, and a cache read larger than the input
 * means the two were not counted the same way.
 */
export function cachedShare(aggregate: AdminUsageAggregate): number | null {
  const totals = aggregate.totals;
  const unknown = aggregate.unknown_rows;
  if (!totals || totals.tokens_in == null || totals.cache_read == null) return null;
  if (unknown && (unknown.tokens_in > 0 || unknown.cache_read > 0)) return null;
  if (totals.tokens_in <= 0 || totals.cache_read > totals.tokens_in) return null;
  return totals.cache_read / totals.tokens_in;
}

function count(value: number, noun: string): string {
  return `${value.toLocaleString('en-US')} ${noun}${value === 1 ? '' : 's'}`;
}

/**
 * What keeps these numbers from being final.
 *
 * `clauses` is the one line the page shows, and only when it is not empty:
 * turns still running, turns not fully counted, and calls whose cost the
 * cost total is missing. `detail` is the breakdown behind it, for a tooltip.
 */
export function notFinal(aggregate: AdminUsageAggregate): { clauses: string[]; detail: string[] } {
  const statuses = aggregate.status_counts;
  const clauses: string[] = [];
  if (aggregate.active_runs) clauses.push(`${count(aggregate.active_runs, 'turn')} still running`);
  const incomplete = aggregate.run_count - (statuses.available ?? 0);
  if (incomplete > 0) clauses.push(`${count(incomplete, 'turn')} not fully counted`);
  const uncosted = aggregate.unknown_rows?.spent_usd ?? 0;
  if (uncosted) clauses.push(`${count(uncosted, 'model call')} without a recorded cost`);

  const detail: string[] = [];
  if (statuses.pending) detail.push(`${count(statuses.pending, 'turn')} without usage yet`);
  if (statuses.unavailable) {
    detail.push(`${count(statuses.unavailable, 'turn')} whose usage could not be read`);
  }
  if (statuses.partial) detail.push(`${count(statuses.partial, 'turn')} partly counted`);
  if (statuses.stale) {
    detail.push(`${count(statuses.stale, 'turn')} not updated since a failed read`);
  }
  if (aggregate.estimated_calls) {
    detail.push(`${count(aggregate.estimated_calls, 'model call')} with estimated tokens`);
  }
  return { clauses, detail };
}

/**
 * A group's share of the selection, for the bar beside it.
 *
 * Measured by cost when the selection has one, else by model calls, else by
 * turns, so a selection whose cost was never recorded still ranks by use.
 */
export function share(group: AdminUsageAggregate, whole: AdminUsageAggregate): number {
  for (const metric of ['cost', 'calls', 'turns'] as const) {
    const total = metricValue(whole, metric);
    if (total !== null && total > 0) {
      return Math.min(1, Math.max(0, (metricValue(group, metric) ?? 0) / total));
    }
  }
  return 0;
}

/** `Sep 23`, for a UTC calendar date or an instant. */
export function shortDay(value: string | null | undefined): string {
  const time = Date.parse(value ?? '');
  if (!Number.isFinite(time)) return '—';
  return new Date(time).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

/** True when the window covers only part of this UTC day, as its first and last days can. */
export function isPartialDay(day: AdminUsageDay): boolean {
  return Date.parse(day.until) - Date.parse(day.since) < DAY_MS;
}

/** Which days get a label under the chart: every one up to eight, otherwise five spread evenly. */
export function tickIndexes(days: number): number[] {
  if (days <= 8) return Array.from({ length: days }, (_, index) => index);
  const ticks = 5;
  const step = (days - 1) / (ticks - 1);
  return Array.from({ length: ticks }, (_, index) => Math.round(index * step));
}

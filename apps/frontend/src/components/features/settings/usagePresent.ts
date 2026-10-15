// How the Usage settings section reads its numbers: containers and their
// states, the period's totals, and one value per day for the chart. Separate
// from the component so the arithmetic is tested directly.

import type { AdminUsageAggregate, AdminUsageDay } from '@/lib/api/adminUsage';
import type { ContainerState, OwnerResourceDay, OwnerResourceTotals } from '@/lib/api/ownerUsage';

export const USAGE_PERIODS = [7, 30] as const;
export type UsagePeriod = (typeof USAGE_PERIODS)[number];

export const CHART_METRICS = ['cost', 'tokens', 'running', 'cpu', 'memory'] as const;
export type ChartMetric = (typeof CHART_METRICS)[number];

/** States in the order the summary names them; a zero count is left out. */
export const STATE_ORDER: ContainerState[] = [
  'running',
  'starting',
  'stopping',
  'stopped',
  'stored',
  'deleting',
  'unavailable',
];

const HOUR = 3600;
const GIB = 1024 ** 3;

function finite(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

/** A number with as many decimals as its size needs: 0.05, 1.3, 14. */
export function decimal(value: number): string {
  const digits = value === 0 || value >= 10 ? 0 : value >= 1 ? 1 : 2;
  return value.toLocaleString('en-US', { maximumFractionDigits: digits });
}

/** Docker's binary units, named as people say them: 4 GB is 4 GiB. */
export function formatMemory(bytes: number | null | undefined): string {
  const value = finite(bytes);
  if (value === null) return '—';
  if (value >= GIB) return `${decimal(value / GIB)} GB`;
  return `${Math.round(value / 1024 ** 2).toLocaleString('en-US')} MB`;
}

const COMPACT = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });

/** 9,907 stays whole; 1,234,567 reads 1.2M. */
export function compactCount(value: number | null | undefined): string {
  const amount = finite(value);
  if (amount === null) return '—';
  return amount < 10_000 ? amount.toLocaleString('en-US') : COMPACT.format(amount);
}

/** A recorded USD amount to the cent; an unknown amount is a dash, never $0. */
export function formatCost(value: string | null | undefined): string {
  if (value == null || !/^\d+(\.\d+)?$/.test(value)) return '—';
  const amount = Number(value);
  if (amount === 0) return '$0';
  if (amount < 0.01) return '<$0.01';
  return `$${amount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** Hours of running time, or minutes when under one hour. */
export function duration(seconds: number): { unit: 'hours' | 'minutes'; value: string } {
  if (seconds < HOUR) return { unit: 'minutes', value: String(Math.round(seconds / 60)) };
  return { unit: 'hours', value: decimal(seconds / HOUR) };
}

export function coreHours(totals: Pick<OwnerResourceTotals, 'cpu_core_seconds'>): number {
  return totals.cpu_core_seconds / HOUR;
}

export function gibHours(totals: Pick<OwnerResourceTotals, 'memory_byte_seconds'>): number {
  return totals.memory_byte_seconds / HOUR / GIB;
}

/**
 * Whether CPU and memory describe noticeably less than the running time.
 *
 * Measurement began with this feature and skips gaps in host reports, so the
 * history can cover only part of the time containers ran. Small differences
 * are sampling, not missing data.
 */
export function partlyMeasured(totals: OwnerResourceTotals): boolean {
  return totals.running_seconds > 60 && totals.measured_seconds < totals.running_seconds * 0.9;
}

/** Input and output tokens together; null when either is unknown. */
export function tokenTotal(usage: AdminUsageAggregate): number | null {
  const tokensIn = finite(usage.totals?.tokens_in);
  const tokensOut = finite(usage.totals?.tokens_out);
  return tokensIn === null || tokensOut === null ? null : tokensIn + tokensOut;
}

/** Turns whose usage the Gateway has not reported yet. */
export function unrecordedTurns(usage: AdminUsageAggregate): number {
  return Math.max(0, usage.run_count - usage.runs_with_snapshot);
}

export interface ChartDay {
  date: string;
  /** False for today, which has not ended. */
  whole: boolean;
  tokens: AdminUsageDay | null;
  resources: OwnerResourceDay | null;
}

/** Join the two histories by UTC date, in the resources' order. */
export function chartDays(
  tokens: AdminUsageDay[] | undefined,
  resources: OwnerResourceDay[] | undefined,
): ChartDay[] {
  const tokensByDate = new Map((tokens ?? []).map((day) => [day.date, day]));
  const resourcesByDate = new Map((resources ?? []).map((day) => [day.date, day]));
  const dates = resources?.length ? resources : (tokens ?? []);
  return dates.map((day) => ({
    date: day.date,
    whole: Date.parse(day.until) - Date.parse(day.since) >= 86_400_000,
    tokens: tokensByDate.get(day.date) ?? null,
    resources: resourcesByDate.get(day.date) ?? null,
  }));
}

/**
 * One day's value of a metric, in the unit its tiles use.
 *
 * Null is unknown, not zero: a day with turns but no recorded cost, or with
 * running time but no measurement, is marked rather than drawn as empty.
 */
export function dayValue(day: ChartDay, metric: ChartMetric): number | null {
  if (metric === 'cost' || metric === 'tokens') {
    if (!day.tokens) return null;
    if (day.tokens.usage.run_count === 0) return 0;
    if (metric === 'tokens') return tokenTotal(day.tokens.usage);
    const cost = day.tokens.usage.totals?.spent_usd;
    return cost != null && /^\d+(\.\d+)?$/.test(cost) ? Number(cost) : null;
  }
  if (!day.resources) return null;
  if (metric === 'running') return day.resources.running_seconds / HOUR;
  if (day.resources.measured_seconds === 0) return day.resources.running_seconds > 0 ? null : 0;
  return metric === 'cpu' ? coreHours(day.resources) : gibHours(day.resources);
}

/** A chart value in the metric's own words, without the unit. */
export function formatDayValue(metric: ChartMetric, value: number | null): string {
  if (value === null) return '—';
  if (metric === 'cost') {
    if (value === 0) return '$0';
    if (value < 0.01) return '<$0.01';
    return `$${value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  return metric === 'tokens' ? compactCount(value) : decimal(value);
}

/** `Oct 9`, for a UTC calendar date. */
export function shortDay(date: string, locale: string): string {
  const time = Date.parse(`${date}T00:00:00Z`);
  if (!Number.isFinite(time)) return date;
  return new Date(time).toLocaleDateString(locale, {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

/**
 * Which days get a label under the chart: every one up to ``most``, otherwise
 * at most five spread evenly. A phone fits four.
 */
export function tickIndexes(days: number, most = 8): number[] {
  if (days <= most) return Array.from({ length: days }, (_, index) => index);
  const count = Math.min(5, most);
  const step = (days - 1) / (count - 1);
  return Array.from({ length: count }, (_, index) => Math.round(index * step));
}

/** `in 18 minutes`, `3 hours ago`, in the reader's language. */
export function relativeTime(iso: string, nowMs: number, locale: string): string {
  const seconds = (Date.parse(iso) - nowMs) / 1000;
  if (!Number.isFinite(seconds)) return '—';
  const format = new Intl.RelativeTimeFormat(locale, { numeric: 'always' });
  const size = Math.abs(seconds);
  if (size < HOUR) return format.format(Math.round(seconds / 60), 'minute');
  if (size < 86_400) return format.format(Math.round(seconds / HOUR), 'hour');
  return format.format(Math.round(seconds / 86_400), 'day');
}

// How the Usage settings section reads its numbers: containers and their
// states, the period's totals, and one value per day for the chart. Separate
// from the component so the arithmetic is tested directly.

import type { AdminUsageAggregate, AdminUsageDay } from '@/lib/api/adminUsage';
import type {
  ContainerState,
  HistoryQuery,
  OwnerHistoryPeriod,
  OwnerResourceDay,
  OwnerResourceTotals,
} from '@/lib/api/ownerUsage';
import type { TranslationKey, TranslationVars } from '@/lib/i18n/translate';
import { APIError } from '@/lib/utils/errors';

// Named, not counted: "today" is not "1 days", and all time is no number of days.
export const USAGE_PERIODS = ['today', '7d', '30d', 'all'] as const;
export type UsagePeriod = (typeof USAGE_PERIODS)[number];

const PERIOD_QUERY: Record<UsagePeriod, HistoryQuery> = {
  today: { days: 1 },
  '7d': { days: 7 },
  '30d': { days: 30 },
  all: { all: true },
};

/** What a period asks the server for. */
export function periodQuery(period: UsagePeriod): HistoryQuery {
  return PERIOD_QUERY[period];
}

export type ChartMetric = 'cost' | 'tokens' | 'running' | 'cpu' | 'memory';
/** What the chart offers: not cost, which each owner's provider keeps. */
export const CHART_METRICS: readonly ChartMetric[] = ['tokens', 'running', 'cpu', 'memory'];

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

/**
 * The share of input tokens read from the cache, as the Admin Usage page's
 * `cachedShare` reads it.
 *
 * Only a ratio both sides fully report is shown: a call that did not report
 * one of the two would make it a guess, and a cache read larger than the input
 * means the two were not counted the same way. Input already includes cached
 * input, so the cache read is a part of it and is never added to it.
 */
export function cachedShare(usage: AdminUsageAggregate): number | null {
  const totals = usage.totals;
  const unknown = usage.unknown_rows;
  if (!totals || totals.tokens_in == null || totals.cache_read == null) return null;
  if (unknown && (unknown.tokens_in > 0 || unknown.cache_read > 0)) return null;
  if (totals.tokens_in <= 0 || totals.cache_read > totals.tokens_in) return null;
  return totals.cache_read / totals.tokens_in;
}

/** `42%`; a share above zero that rounds to none reads `<1%`, and an unknown one a dash. */
export function formatShare(share: number | null): string {
  if (share === null || !Number.isFinite(share)) return '—';
  const percent = Math.round(share * 100);
  return share > 0 && percent === 0 ? '<1%' : `${percent}%`;
}

/**
 * Whether a known reasoning total leaves out calls that did not report one.
 * An unknown total already reads as a dash and needs no such note.
 */
export function reasoningPartly(usage: AdminUsageAggregate): boolean {
  return usage.totals?.reasoning != null && (usage.unknown_rows?.reasoning ?? 0) > 0;
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

/** `Oct 9`, or `Oct 9, 2025` with ``withYear``, for a UTC calendar date. */
export function shortDay(date: string, locale: string, withYear = false): string {
  const time = Date.parse(`${date}T00:00:00Z`);
  if (!Number.isFinite(time)) return date;
  return new Date(time).toLocaleDateString(locale, {
    year: withYear ? 'numeric' : undefined,
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

/**
 * Past this many days the chart draws one bar per week. Thirty days still fit
 * a phone as bars wide enough to point at; ninety, or a year, do not.
 */
export const MAX_DAILY_BARS = 31;
const DAY_MS = 86_400_000;

/** One bar of the chart: a UTC day, or the days of one week inside the period. */
export interface ChartBar {
  /** The first and last UTC date the bar sums; the same date for a day. */
  first: string;
  last: string;
  days: number;
  /** False when the bar covers only part of its day or week. */
  whole: boolean;
  /** Null is unknown: a week with one unknown day is unknown as a whole. */
  value: number | null;
}

/** The Monday (UTC) that starts ``date``'s week, as ISO weeks do. */
function weekOf(date: string): string {
  const time = Date.parse(`${date}T00:00:00Z`);
  const sinceMonday = (new Date(time).getUTCDay() + 6) % 7;
  return new Date(time - sinceMonday * DAY_MS).toISOString().slice(0, 10);
}

/**
 * A metric's bars: one per day, or one per calendar week past
 * ``MAX_DAILY_BARS`` days.
 *
 * A week sums only the days the period has. Days before the period are not
 * drawn as zero; the week the period starts in is partial instead, and so is
 * the week that holds today. Like ``tokenTotal``, a sum with one unknown day
 * in it is unknown.
 */
export function chartBars(days: ChartDay[], metric: ChartMetric): ChartBar[] {
  const bars: ChartBar[] = [];
  const weekly = days.length > MAX_DAILY_BARS;
  let week: string | null = null;
  for (const day of days) {
    const value = dayValue(day, metric);
    const start = weekly ? weekOf(day.date) : null;
    const bar = bars.at(-1);
    if (bar && start !== null && start === week) {
      bar.last = day.date;
      bar.days += 1;
      bar.whole &&= day.whole;
      bar.value = bar.value === null || value === null ? null : bar.value + value;
      continue;
    }
    week = start;
    bars.push({ first: day.date, last: day.date, days: 1, whole: day.whole, value });
  }
  return weekly ? bars.map((bar) => ({ ...bar, whole: bar.whole && bar.days === 7 })) : bars;
}

/** Whether a chart's labels need their year: the bars cross a new year. */
export function crossesYear(bars: ChartBar[]): boolean {
  return bars.length > 0 && bars[0].first.slice(0, 4) !== bars[bars.length - 1].last.slice(0, 4);
}

/** The UTC date an instant falls on, as `2026-04-12`. */
function utcDate(iso: string): string {
  const time = Date.parse(iso);
  return Number.isFinite(time) ? new Date(time).toISOString().slice(0, 10) : iso;
}

/**
 * What an all-time period covers, said as it is.
 *
 * All time is shown as all only when it starts at the owner's first run.
 * Narrowed, it names the first day it holds and why earlier days are left out.
 * A period of some number of days needs no note: its button says it.
 */
export function rangeNote(
  period: OwnerHistoryPeriod | null | undefined,
): { key: TranslationKey; vars?: TranslationVars } | null {
  if (period?.kind !== 'all') return null;
  if (period.first_activity_at === null) return { key: 'settings.usage.range.noRuns' };
  const date = utcDate(period.since);
  if (period.limited_by === 'runs') return { key: 'settings.usage.range.runs', vars: { date } };
  if (period.limited_by === 'days') {
    const span = Date.parse(`${utcDate(period.until)}T00:00:00Z`) - Date.parse(period.since);
    return { key: 'settings.usage.range.days', vars: { date, count: span / DAY_MS + 1 } };
  }
  return { key: 'settings.usage.range.first', vars: { date } };
}

const ERROR_KEY: Partial<Record<string, TranslationKey>> = {
  usage_query_too_large: 'settings.usage.error.tooLarge',
  usage_response_too_large: 'settings.usage.error.responseTooLarge',
  usage_temporarily_unavailable: 'settings.usage.error.unavailable',
};

/**
 * The words for a history read the server refused, in the reader's language.
 * The server's own message is English prose for logs and is never shown.
 */
export function historyErrorKey(cause: unknown): TranslationKey {
  if (!(cause instanceof APIError)) return 'settings.usage.loadError';
  const key = ERROR_KEY[cause.code];
  if (key) return key;
  return cause.statusCode === 503 ? 'settings.usage.error.unavailable' : 'settings.usage.loadError';
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

/**
 * The reader's clock time at the 00:00 UTC that starts ``atMs``'s day, such as
 * `8:00` in Beijing; null when the reader's clock already reads UTC there.
 */
export function utcMidnightLocal(atMs: number, locale: string, timeZone?: string): string | null {
  const at = new Date(atMs);
  if (!Number.isFinite(at.getTime())) return null;
  const midnight = Date.UTC(at.getUTCFullYear(), at.getUTCMonth(), at.getUTCDate());
  const options: Intl.DateTimeFormatOptions = { hour: 'numeric', minute: '2-digit' };
  const local = new Intl.DateTimeFormat(locale, { ...options, timeZone }).format(midnight);
  const utc = new Intl.DateTimeFormat(locale, { ...options, timeZone: 'UTC' }).format(midnight);
  return local === utc ? null : local;
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

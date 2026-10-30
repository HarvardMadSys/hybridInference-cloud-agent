import { describe, expect, it } from 'vitest';

import type { AdminUsageAggregate, AdminUsageDay } from '@/lib/api/adminUsage';
import type { OwnerHistoryPeriod, OwnerResourceDay } from '@/lib/api/ownerUsage';
import { translate } from '@/lib/i18n/translate';
import { APIError } from '@/lib/utils/errors';

import {
  MAX_DAILY_BARS,
  USAGE_PERIODS,
  cachedShare,
  chartBars,
  chartDays,
  crossesYear,
  compactCount,
  coreHours,
  dayValue,
  decimal,
  duration,
  formatCost,
  formatDayValue,
  formatMemory,
  formatShare,
  gibHours,
  historyErrorKey,
  partlyMeasured,
  periodQuery,
  rangeNote,
  reasoningPartly,
  relativeTime,
  shortDay,
  tickIndexes,
  tokenTotal,
  utcMidnightLocal,
} from './usagePresent';

const GIB = 1024 ** 3;

function aggregate(overrides: Partial<AdminUsageAggregate> = {}): AdminUsageAggregate {
  return {
    run_count: 2,
    attempt_count: 2,
    totals: {
      calls: 4,
      tokens_in: 1000,
      tokens_out: 250,
      cache_read: 0,
      cache_write: 0,
      reasoning: null,
      spent_usd: '1.2345',
    },
    unknown_rows: null,
    estimated_calls: 0,
    oldest_queried_at: null,
    runs_with_snapshot: 2,
    missing_attempts: 0,
    status_counts: { available: 2, partial: 0, stale: 0, pending: 0, unavailable: 0 },
    active_runs: 0,
    refreshing_runs: 0,
    ...overrides,
  };
}

function tokenDay(date: string, usage = aggregate()): AdminUsageDay {
  return { date, since: `${date}T00:00:00Z`, until: `${date}T23:59:59.999Z`, usage };
}

function resourceDay(date: string, overrides: Partial<OwnerResourceDay> = {}): OwnerResourceDay {
  return {
    date,
    since: `${date}T00:00:00Z`,
    until: new Date(Date.parse(`${date}T00:00:00Z`) + 86_400_000).toISOString(),
    running_seconds: 7200,
    measured_seconds: 3600,
    cpu_core_seconds: 1800,
    memory_byte_seconds: 3600 * 2 * GIB,
    ...overrides,
  };
}

describe('numbers', () => {
  it('keep as many decimals as their size needs', () => {
    expect(decimal(0)).toBe('0');
    expect(decimal(0.054)).toBe('0.05');
    expect(decimal(1.34)).toBe('1.3');
    expect(decimal(14.6)).toBe('15');
  });

  it('read memory in the binary units Docker limits use', () => {
    expect(formatMemory(4 * GIB)).toBe('4 GB');
    expect(formatMemory(1.5 * GIB)).toBe('1.5 GB');
    expect(formatMemory(512 * 1024 ** 2)).toBe('512 MB');
    expect(formatMemory(null)).toBe('—');
  });

  it('shorten counts only past ten thousand', () => {
    expect(compactCount(9907)).toBe('9,907');
    expect(compactCount(1_234_567)).toBe('1.2M');
    expect(compactCount(null)).toBe('—');
  });

  it('show a cost to the cent and an unknown cost as a dash', () => {
    expect(formatCost('1.2345')).toBe('$1.23');
    expect(formatCost('0')).toBe('$0');
    expect(formatCost('0.0001')).toBe('<$0.01');
    expect(formatCost('1234.5')).toBe('$1,234.50');
    expect(formatCost(null)).toBe('—');
    expect(formatCost('not a number')).toBe('—');
  });

  it('read running time in minutes under an hour', () => {
    expect(duration(35 * 60)).toEqual({ unit: 'minutes', value: '35' });
    expect(duration(14.2 * 3600)).toEqual({ unit: 'hours', value: '14' });
    expect(duration(1.5 * 3600)).toEqual({ unit: 'hours', value: '1.5' });
  });

  it('convert measured seconds into core-hours and GB-hours', () => {
    expect(coreHours({ cpu_core_seconds: 7200 })).toBe(2);
    expect(gibHours({ memory_byte_seconds: 3600 * 4 * GIB })).toBe(4);
  });
});

describe('coverage', () => {
  it('flags CPU and memory that cover much less than the running time', () => {
    const base = { cpu_core_seconds: 0, memory_byte_seconds: 0 };
    expect(partlyMeasured({ ...base, running_seconds: 7200, measured_seconds: 3600 })).toBe(true);
    expect(partlyMeasured({ ...base, running_seconds: 7200, measured_seconds: 7000 })).toBe(false);
    expect(partlyMeasured({ ...base, running_seconds: 30, measured_seconds: 0 })).toBe(false);
  });

  it('adds input and output tokens only when both are known', () => {
    expect(tokenTotal(aggregate())).toBe(1250);
    const unknown = aggregate();
    unknown.totals = { ...unknown.totals!, tokens_out: null };
    expect(tokenTotal(unknown)).toBeNull();
  });

  it('never adds reasoning or cached input again: output and input hold them', () => {
    const usage = aggregate();
    usage.totals = { ...usage.totals!, cache_read: 400, cache_write: 100, reasoning: 90 };
    expect(tokenTotal(usage)).toBe(1250);
  });
});

describe('cache hit rate', () => {
  function withCache(
    cacheRead: number | null,
    tokensIn: number | null = 1000,
    unknown: Partial<NonNullable<AdminUsageAggregate['unknown_rows']>> | null = null,
  ): AdminUsageAggregate {
    const usage = aggregate();
    usage.totals = { ...usage.totals!, cache_read: cacheRead, tokens_in: tokensIn };
    if (unknown) {
      usage.unknown_rows = {
        tokens_in: 0,
        tokens_out: 0,
        cache_read: 0,
        cache_write: 0,
        reasoning: 0,
        spent_usd: 0,
        ...unknown,
      };
    }
    return usage;
  }

  it('reads the share of input taken from the cache', () => {
    expect(cachedShare(withCache(250))).toBe(0.25);
    expect(cachedShare(withCache(250, 1000, { tokens_out: 3, reasoning: 2 }))).toBe(0.25);
  });

  it('keeps a reported zero as zero', () => {
    expect(cachedShare(withCache(0))).toBe(0);
    expect(formatShare(cachedShare(withCache(0)))).toBe('0%');
  });

  it('shows no ratio for anything not fully reported', () => {
    expect(cachedShare(aggregate({ totals: null }))).toBeNull();
    expect(cachedShare(withCache(null))).toBeNull();
    expect(cachedShare(withCache(250, null))).toBeNull();
    expect(cachedShare(withCache(250, 1000, { cache_read: 1 }))).toBeNull();
    expect(cachedShare(withCache(250, 1000, { tokens_in: 1 }))).toBeNull();
  });

  it('shows no ratio for no input, or a cache read larger than the input', () => {
    expect(cachedShare(withCache(0, 0))).toBeNull();
    expect(cachedShare(withCache(1200, 1000))).toBeNull();
  });

  it('reads an unknown share as a dash, never 0%', () => {
    expect(formatShare(null)).toBe('—');
    expect(formatShare(cachedShare(withCache(null)))).toBe('—');
    expect(formatShare(0.4248)).toBe('42%');
    expect(formatShare(0.004)).toBe('<1%');
    expect(formatShare(1)).toBe('100%');
  });
});

describe('reasoning', () => {
  function withReasoning(reasoning: number | null, unreported: number | null): AdminUsageAggregate {
    const usage = aggregate();
    usage.totals = { ...usage.totals!, reasoning };
    if (unreported !== null) {
      usage.unknown_rows = {
        tokens_in: 0,
        tokens_out: 0,
        cache_read: 0,
        cache_write: 0,
        reasoning: unreported,
        spent_usd: 0,
      };
    }
    return usage;
  }

  it('notes a known total that leaves out calls not reporting it', () => {
    expect(reasoningPartly(withReasoning(90, 2))).toBe(true);
  });

  it('notes nothing for a fully reported or an unknown total', () => {
    expect(reasoningPartly(withReasoning(90, null))).toBe(false);
    expect(reasoningPartly(withReasoning(90, 0))).toBe(false);
    expect(reasoningPartly(withReasoning(null, 2))).toBe(false);
    expect(reasoningPartly(aggregate({ totals: null }))).toBe(false);
  });
});

describe('chart days', () => {
  it('join the two histories by UTC date and mark the unfinished day', () => {
    const days = chartDays(
      [tokenDay('2026-10-14'), tokenDay('2026-10-15')],
      [resourceDay('2026-10-14'), { ...resourceDay('2026-10-15'), until: '2026-10-15T12:00:00Z' }],
    );
    expect(days.map((day) => [day.date, day.whole])).toEqual([
      ['2026-10-14', true],
      ['2026-10-15', false],
    ]);
    expect(days[0].tokens?.date).toBe('2026-10-14');
    expect(days[1].resources?.date).toBe('2026-10-15');
  });

  it('give each metric its own unit, and an unknown value as null', () => {
    const [day] = chartDays([tokenDay('2026-10-14')], [resourceDay('2026-10-14')]);
    expect(dayValue(day, 'cost')).toBeCloseTo(1.2345);
    expect(dayValue(day, 'tokens')).toBe(1250);
    expect(dayValue(day, 'running')).toBe(2);
    expect(dayValue(day, 'cpu')).toBe(0.5);
    expect(dayValue(day, 'memory')).toBe(2);

    const [unmeasured] = chartDays(
      [tokenDay('2026-10-14', aggregate({ totals: null }))],
      [resourceDay('2026-10-14', { measured_seconds: 0, cpu_core_seconds: 0 })],
    );
    expect(dayValue(unmeasured, 'cost')).toBeNull();
    expect(dayValue(unmeasured, 'cpu')).toBeNull();

    const [idle] = chartDays(
      [tokenDay('2026-10-14', aggregate({ run_count: 0, totals: null }))],
      [resourceDay('2026-10-14', { running_seconds: 0, measured_seconds: 0 })],
    );
    expect(dayValue(idle, 'cost')).toBe(0);
    expect(dayValue(idle, 'cpu')).toBe(0);
  });

  it('read chart values without units', () => {
    expect(formatDayValue('cost', 3.456)).toBe('$3.46');
    expect(formatDayValue('tokens', 123_456)).toBe('123.5K');
    expect(formatDayValue('cpu', 0.25)).toBe('0.25');
    expect(formatDayValue('memory', null)).toBe('—');
  });

  it('label every day up to a bound, then a few spread evenly', () => {
    expect(tickIndexes(7)).toEqual([0, 1, 2, 3, 4, 5, 6]);
    expect(tickIndexes(30)).toEqual([0, 7, 15, 22, 29]);
    expect(tickIndexes(7, 4)).toEqual([0, 2, 4, 6]);
    expect(tickIndexes(30, 4)).toEqual([0, 10, 19, 29]);
  });
});

describe('relative time', () => {
  const now = Date.parse('2026-10-15T12:00:00Z');
  it('reads minutes, hours and days in the reader’s language', () => {
    expect(relativeTime('2026-10-15T12:18:00Z', now, 'en')).toBe('in 18 minutes');
    expect(relativeTime('2026-10-15T09:00:00Z', now, 'en')).toBe('3 hours ago');
    expect(relativeTime('2026-10-13T12:00:00Z', now, 'en')).toBe('2 days ago');
    expect(relativeTime('2026-10-15T09:00:00Z', now, 'zh-CN')).toBe('3小时前');
    expect(relativeTime('not a time', now, 'en')).toBe('—');
  });
});

describe('the start of a UTC day on the reader’s clock', () => {
  const at = Date.parse('2026-10-15T12:00:00Z');
  it('reads 00:00 UTC in the reader’s time zone, or nothing on UTC', () => {
    expect(utcMidnightLocal(at, 'zh-CN', 'Asia/Shanghai')).toBe('8:00');
    expect(utcMidnightLocal(at, 'en-US', 'America/New_York')).toBe('8:00 PM');
    expect(utcMidnightLocal(at, 'en-US', 'Asia/Kolkata')).toBe('5:30 AM');
    expect(utcMidnightLocal(at, 'en-US', 'UTC')).toBeNull();
    expect(utcMidnightLocal(Number.NaN, 'en-US', 'Asia/Shanghai')).toBeNull();
  });
});

describe('periods', () => {
  it('ask for a number of days, or for all time', () => {
    expect(USAGE_PERIODS.map(periodQuery)).toEqual([
      { days: 1 },
      { days: 7 },
      { days: 30 },
      { all: true },
    ]);
  });
});

/** ``count`` whole UTC days from ``first``, the last of them today so far. */
function history(
  first: string,
  count: number,
  cost = (index: number) =>
    aggregate({ totals: { ...aggregate().totals!, spent_usd: String(index + 1) } }),
) {
  const start = Date.parse(`${first}T00:00:00Z`);
  const days = Array.from({ length: count }, (_, index) => {
    const date = new Date(start + index * 86_400_000).toISOString().slice(0, 10);
    const day = resourceDay(date);
    return index === count - 1 ? { ...day, until: `${date}T12:00:00.000Z` } : day;
  });
  const tokens = days.map((day, index) => ({
    ...tokenDay(day.date, cost(index)),
    until: day.until,
  }));
  return chartDays(tokens, days);
}

describe('chart bars', () => {
  it('stay one per day up to the bound', () => {
    const bars = chartBars(history('2026-10-01', MAX_DAILY_BARS), 'cost');
    expect(bars).toHaveLength(MAX_DAILY_BARS);
    expect(bars[0]).toEqual({
      first: '2026-10-01',
      last: '2026-10-01',
      days: 1,
      whole: true,
      value: 1,
    });
    expect(bars.at(-1)!.whole).toBe(false);
  });

  it('sum calendar weeks past it, from the first real day and with no padding', () => {
    // 2026-09-03 is a Thursday: its week holds only the four days the period has.
    const bars = chartBars(history('2026-09-03', 40), 'cost');
    expect(bars[0]).toEqual({
      first: '2026-09-03',
      last: '2026-09-06',
      days: 4,
      whole: false,
      value: 1 + 2 + 3 + 4,
    });
    expect(bars[1]).toMatchObject({
      first: '2026-09-07',
      last: '2026-09-13',
      days: 7,
      whole: true,
    });
    expect(bars[1].value).toBe(5 + 6 + 7 + 8 + 9 + 10 + 11);
    // The week that holds today has not ended either.
    expect(bars.at(-1)).toMatchObject({ last: '2026-10-12', whole: false });
    expect(bars.reduce((days, bar) => days + bar.days, 0)).toBe(40);
  });

  it('call a week unknown when any one of its days is', () => {
    const unknownOn = '2026-09-09';
    const days = history('2026-09-07', 40).map((day) =>
      day.date === unknownOn
        ? { ...day, tokens: tokenDay(day.date, aggregate({ totals: null })) }
        : day,
    );
    const bars = chartBars(days, 'cost');
    expect(bars[0]).toMatchObject({ first: '2026-09-07', value: null });
    expect(bars[1].value).not.toBeNull();
    // A week with no turns at all is zero, not unknown.
    const quiet = history('2026-09-07', 40, () => aggregate({ run_count: 0, totals: null }));
    expect(chartBars(quiet, 'cost')[0].value).toBe(0);
  });

  it('name the year only when the bars cross one', () => {
    expect(crossesYear(chartBars(history('2026-09-03', 40), 'cost'))).toBe(false);
    expect(crossesYear(chartBars(history('2025-12-20', 40), 'cost'))).toBe(true);
    expect(shortDay('2025-12-22', 'en-US', true)).toBe('Dec 22, 2025');
    expect(shortDay('2025-12-22', 'en-US')).toBe('Dec 22');
  });
});

function allTime(overrides: Partial<OwnerHistoryPeriod> = {}): OwnerHistoryPeriod {
  return {
    kind: 'all',
    since: '2026-04-12T00:00:00Z',
    until: '2026-10-15T12:00:00Z',
    first_activity_at: '2026-04-12T08:30:00Z',
    limited_by: null,
    ...overrides,
  };
}

describe('the range note', () => {
  const say = (period: OwnerHistoryPeriod | null, locale: 'en' | 'zh-CN' = 'en') => {
    const note = rangeNote(period);
    return note && translate(locale, note.key, note.vars);
  };

  it('says nothing about a number of days', () => {
    expect(say({ ...allTime(), kind: 'days', first_activity_at: null })).toBeNull();
    expect(say(null)).toBeNull();
  });

  it('calls all time all only from the first turn', () => {
    expect(say(allTime())).toBe('Since 2026-04-12, the day of your first turn.');
    expect(say(allTime({ first_activity_at: null, since: '2026-10-15T00:00:00Z' }))).toBe(
      'No turns yet, so this shows today only.',
    );
  });

  it('names the real start and the reason when all time is narrowed', () => {
    const runs = allTime({ first_activity_at: '2026-01-02T00:00:00Z', limited_by: 'runs' });
    expect(say(runs, 'zh-CN')).toBe('自 2026-04-12 起 · 更早的用量超出单次汇总上限，未计入。');
    expect(say(runs)).not.toMatch(/\ball\b/i);
    const days = allTime({
      since: '2025-10-15T00:00:00Z',
      first_activity_at: '2025-01-02T00:00:00Z',
      limited_by: 'days',
    });
    expect(say(days)).toBe(
      'Since 2025-10-15 · Usage from before the last 366 days is not included.',
    );
  });
});

describe('a refused history read', () => {
  it('is told in the reader’s words, never the server’s', () => {
    const refused = (code: string, status: number) => new APIError(code, 'English prose', status);
    expect(historyErrorKey(refused('usage_query_too_large', 422))).toBe(
      'settings.usage.error.tooLarge',
    );
    expect(historyErrorKey(refused('usage_response_too_large', 422))).toBe(
      'settings.usage.error.responseTooLarge',
    );
    expect(historyErrorKey(refused('usage_temporarily_unavailable', 503))).toBe(
      'settings.usage.error.unavailable',
    );
    // No database behind the service: a 503 without the usage envelope.
    expect(historyErrorKey(refused('SERVICE_UNAVAILABLE', 503))).toBe(
      'settings.usage.error.unavailable',
    );
    expect(historyErrorKey(refused('UNKNOWN_ERROR', 422))).toBe('settings.usage.loadError');
    expect(historyErrorKey(new Error('Failed to fetch'))).toBe('settings.usage.loadError');
  });
});

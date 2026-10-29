import { describe, expect, it } from 'vitest';

import type { AdminUsageAggregate, AdminUsageDay } from '@/lib/api/adminUsage';
import type { OwnerResourceDay } from '@/lib/api/ownerUsage';

import {
  cachedShare,
  chartDays,
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
  notFinal,
  partlyMeasured,
  reasoningPartly,
  relativeTime,
  tickIndexes,
  tokenTotal,
  unrecordedTurns,
  utcMidnightLocal,
  bucketLabel,
  crossesYear,
  rangeSpan,
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

  it('counts turns the Gateway has not reported', () => {
    const usage = aggregate({
      run_count: 5,
      runs_with_snapshot: 3,
      status_counts: { available: 3, partial: 0, stale: 0, pending: 1, unavailable: 1 },
    });
    expect(unrecordedTurns(usage)).toBe(2);
    expect(unrecordedTurns(aggregate())).toBe(0);
  });

  it('leaves out a turn that never called a model, and one refreshing its report', () => {
    // One turn has a report and is reading it again, one ended before its
    // agent called a model, and one is waiting for its first report.
    const usage = aggregate({
      run_count: 3,
      runs_with_snapshot: 1,
      status_counts: { available: 2, partial: 0, stale: 0, pending: 1, unavailable: 0 },
      refreshing_runs: 2,
    });
    expect(unrecordedTurns(usage)).toBe(1);
  });

  it('adds input and output tokens only when both are known', () => {
    expect(tokenTotal(aggregate())).toBe(1250);
    const unknown = aggregate();
    unknown.totals = { ...unknown.totals!, tokens_out: null };
    expect(tokenTotal(unknown)).toBeNull();
  });

  it('leaves reasoning and cached input out of the token total', () => {
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

describe('not final', () => {
  it('says nothing when every turn is counted and none is running', () => {
    expect(notFinal(aggregate({ oldest_queried_at: '2026-10-15T09:00:00Z' }))).toBeNull();
  });

  it('counts running and not fully counted turns, and uncosted and estimated calls', () => {
    const usage = aggregate({
      run_count: 6,
      active_runs: 1,
      estimated_calls: 3,
      oldest_queried_at: '2026-10-15T09:00:00Z',
      status_counts: { available: 2, partial: 1, stale: 1, pending: 1, unavailable: 1 },
      unknown_rows: {
        tokens_in: 0,
        tokens_out: 0,
        cache_read: 0,
        cache_write: 0,
        reasoning: 0,
        spent_usd: 2,
      },
    });
    expect(notFinal(usage)).toEqual({
      running: 1,
      incomplete: 4,
      uncosted: 2,
      estimated: 3,
      oldestRead: '2026-10-15T09:00:00Z',
    });
  });

  it('names each reason on its own', () => {
    expect(notFinal(aggregate({ active_runs: 1 }))?.running).toBe(1);
    expect(notFinal(aggregate({ estimated_calls: 1 }))?.estimated).toBe(1);
    const partial = notFinal(
      aggregate({
        status_counts: { available: 1, partial: 1, stale: 0, pending: 0, unavailable: 0 },
      }),
    );
    expect(partial).toMatchObject({ running: 0, incomplete: 1, uncosted: 0, estimated: 0 });
    expect(partial?.oldestRead).toBeNull();
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

describe('ranges and buckets', () => {
  it('names a bucket by its day, week start or month, with the year once bars cross one', () => {
    expect(bucketLabel('2026-10-12', 'day', 'en-US', false)).toBe('Oct 12');
    expect(bucketLabel('2026-10-12', 'week', 'en-US', true)).toBe('Oct 12, 2026');
    expect(bucketLabel('2025-12-01', 'month', 'en-US', true)).toBe('Dec 2025');
    expect(bucketLabel('2025-12-01', 'month', 'en-US', false)).toBe('Dec');
    expect(crossesYear(['2025-12-01', '2026-01-01'])).toBe(true);
    expect(crossesYear(['2026-01-01', '2026-10-01'])).toBe(false);
  });

  it('writes a range’s first and last UTC days', () => {
    const week = {
      preset: '7d' as const,
      days: null,
      since: '2026-10-09T00:00:00Z',
      until: '2026-10-15T12:00:00Z',
      day_timezone: 'UTC' as const,
      bucket: 'day' as const,
      first_record_at: null,
    };
    expect(rangeSpan(week, 'en-US')).toEqual({ since: 'Oct 9', until: 'Oct 15' });
    expect(rangeSpan({ ...week, preset: 'all', since: '2024-03-09T00:00:00Z' }, 'en-US')).toEqual({
      since: 'Mar 9, 2024',
      until: 'Oct 15, 2026',
    });
  });

  it('treats only the bucket that ends now as still going', () => {
    const day = (since: string, until: string) => ({
      date: since.slice(0, 10),
      since,
      until,
      running_seconds: 0,
      measured_seconds: 0,
      cpu_core_seconds: 0,
      memory_byte_seconds: 0,
    });
    const days = chartDays(undefined, [
      // A week clipped by the range's first day is whole.
      day('2024-03-09T00:00:00Z', '2024-03-11T00:00:00Z'),
      day('2026-10-12T00:00:00Z', '2026-10-15T12:00:00Z'),
    ]);
    expect(days.map((entry) => entry.whole)).toEqual([true, false]);
  });
});

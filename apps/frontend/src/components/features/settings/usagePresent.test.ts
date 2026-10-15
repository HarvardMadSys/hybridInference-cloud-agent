import { describe, expect, it } from 'vitest';

import type { AdminUsageAggregate, AdminUsageDay } from '@/lib/api/adminUsage';
import type { OwnerResourceDay } from '@/lib/api/ownerUsage';

import {
  chartDays,
  compactCount,
  coreHours,
  dayValue,
  decimal,
  duration,
  formatCost,
  formatDayValue,
  formatMemory,
  gibHours,
  partlyMeasured,
  relativeTime,
  tickIndexes,
  tokenTotal,
  unrecordedTurns,
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

  it('counts turns the Gateway has not reported', () => {
    expect(unrecordedTurns(aggregate({ run_count: 5, runs_with_snapshot: 3 }))).toBe(2);
    expect(unrecordedTurns(aggregate())).toBe(0);
  });

  it('adds input and output tokens only when both are known', () => {
    expect(tokenTotal(aggregate())).toBe(1250);
    const unknown = aggregate();
    unknown.totals = { ...unknown.totals!, tokens_out: null };
    expect(tokenTotal(unknown)).toBeNull();
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

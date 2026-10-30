import { describe, expect, it } from 'vitest';

import type { AdminUsageAggregate } from '@/lib/api/adminUsage';

import {
  cachedShare,
  change,
  compactCount,
  formatMetric,
  isPartialDay,
  metricDisplay,
  metricValue,
  notFinal,
  previousLabel,
  previousWindow,
  reportedTokens,
  share,
  shortDay,
  tickIndexes,
} from './dashboard';

function aggregate(overrides: Partial<AdminUsageAggregate> = {}): AdminUsageAggregate {
  return {
    run_count: 4,
    attempt_count: 4,
    totals: {
      calls: null,
      tokens_in: 40_086,
      tokens_out: 413,
      cache_read: 30_000,
      cache_write: null,
      reasoning: null,
      spent_usd: null,
    },
    unknown_rows: {
      tokens_in: 0,
      tokens_out: 0,
      cache_read: 0,
      cache_write: 2,
      reasoning: 4,
      spent_usd: 4,
    },
    estimated_calls: 0,
    oldest_queried_at: '2026-09-09T10:01:00Z',
    runs_with_snapshot: 4,
    missing_attempts: 0,
    status_counts: { available: 4, partial: 0, pending: 0, unavailable: 0 },
    active_runs: 0,
    refreshing_runs: 0,
    ...overrides,
  };
}

describe('metricValue', () => {
  it('reads every metric the agents reported', () => {
    const usage = aggregate();
    expect(metricValue(usage, 'turns')).toBe(4);
    expect(metricValue(usage, 'tokens_in')).toBe(40_086);
    expect(metricValue(usage, 'tokens_out')).toBe(413);
    expect(reportedTokens(usage)).toBe(40_086 + 413);
  });

  it('keeps an unreported metric unknown rather than zero', () => {
    const unread = aggregate({ totals: null, runs_with_snapshot: 0 });
    // Turns are counted from the database, so they are known without usage.
    expect(metricValue(unread, 'turns')).toBe(4);
    expect(metricValue(unread, 'tokens_in')).toBeNull();
    expect(reportedTokens(unread)).toBeNull();
    const partly = aggregate({ totals: { ...aggregate().totals!, tokens_out: null } });
    expect(metricValue(partly, 'tokens_out')).toBeNull();
  });
});

describe('number formats', () => {
  it('shortens only counts too long for a headline', () => {
    expect(compactCount(9_907)).toBe('9,907');
    expect(compactCount(22_512)).toBe('22.5K');
    expect(compactCount(87_297_535)).toBe('87.3M');
    expect(compactCount(1_027_441_902)).toBe('1.03B');
    expect(compactCount(null)).toBe('—');
  });

  it('offers the whole count behind a shortened headline', () => {
    expect(metricDisplay(aggregate(), 'tokens_in')).toEqual({ display: '40.1K', exact: '40,086' });
    expect(metricDisplay(aggregate(), 'tokens_out')).toEqual({ display: '413', exact: null });
    expect(metricDisplay(aggregate({ totals: null }), 'tokens_out')).toEqual({
      display: '—',
      exact: null,
    });
  });

  it('formats a chart value as the headline shortens it', () => {
    expect(formatMetric(25_712_000)).toBe('25.7M');
    expect(formatMetric(null)).toBe('—');
  });
});

describe('the previous window', () => {
  it('is equally long, ends where the selection starts and keeps its identities', () => {
    expect(
      previousWindow({
        since: '2026-09-21T09:00:00.000Z',
        until: '2026-09-28T09:00:00.000Z',
        user_id: 'owner-one',
      }),
    ).toEqual({
      since: '2026-09-14T09:00:00.000Z',
      until: '2026-09-21T09:00:00.000Z',
      user_id: 'owner-one',
    });
  });

  it('is not read for a window that is not one', () => {
    expect(previousWindow({})).toBeNull();
    expect(
      previousWindow({ since: '2026-09-28T00:00:00.000Z', until: '2026-09-21T00:00:00.000Z' }),
    ).toBeNull();
  });

  it('is named by its length when that is whole days', () => {
    const week = { since: '2026-09-21T00:00:00.000Z', until: '2026-09-28T00:00:00.000Z' };
    expect(previousLabel(week)).toBe('previous 7 days');
    expect(
      previousLabel({ since: '2026-09-27T00:00:00.000Z', until: '2026-09-28T00:00:00.000Z' }),
    ).toBe('previous day');
    expect(
      previousLabel({ since: '2026-09-27T00:00:00.000Z', until: '2026-09-28T12:00:00.000Z' }),
    ).toBe('previous window');
  });

  it('measures a change only against a recorded, non-zero value', () => {
    expect(change(110, 100)).toBe('+10%');
    expect(change(95, 100)).toBe('−5%');
    expect(change(100.2, 100)).toBe('0%');
    expect(change(5, 0)).toBeNull();
    expect(change(null, 100)).toBeNull();
    expect(change(100, null)).toBeNull();
  });
});

describe('cachedShare', () => {
  it('is the share of input tokens read from the cache', () => {
    expect(cachedShare(aggregate())).toBeCloseTo(30_000 / 40_086);
  });

  it('is not shown when either side is partly unreported or they disagree', () => {
    const unreported = aggregate({
      unknown_rows: { ...aggregate().unknown_rows!, cache_read: 1 },
    });
    expect(cachedShare(unreported)).toBeNull();
    const larger = aggregate({ totals: { ...aggregate().totals!, cache_read: 50_000 } });
    expect(cachedShare(larger)).toBeNull();
    expect(cachedShare(aggregate({ totals: null }))).toBeNull();
  });
});

describe('notFinal', () => {
  it('says nothing when every turn is counted', () => {
    expect(notFinal(aggregate())).toEqual({ clauses: [], detail: [] });
  });

  it('names running and not fully counted turns, and never an unrecorded cost', () => {
    const { clauses, detail } = notFinal(
      aggregate({
        active_runs: 1,
        runs_with_snapshot: 3,
        status_counts: { available: 1, partial: 1, pending: 1, unavailable: 1 },
      }),
    );
    // No agent reports a cost, so its absence is not a gap in the numbers.
    expect(clauses).toEqual(['1 turn still running', '3 turns not fully counted']);
    expect(detail).toEqual([
      '1 turn without usage yet',
      '1 turn whose agent reported no usage',
      '1 turn partly counted',
    ]);
  });
});

describe('share', () => {
  it('measures by reported tokens when the selection has any', () => {
    const whole = aggregate({
      totals: { ...aggregate().totals!, tokens_in: 3_000, tokens_out: 1_000 },
    });
    const group = aggregate({
      totals: { ...aggregate().totals!, tokens_in: 900, tokens_out: 100 },
    });
    expect(share(group, whole)).toBeCloseTo(0.25);
  });

  it('falls back to turns while nothing was reported', () => {
    expect(
      share(aggregate({ run_count: 1, totals: null }), aggregate({ run_count: 4, totals: null })),
    ).toBeCloseTo(0.25);
  });
});

describe('days', () => {
  it('names a UTC day briefly', () => {
    expect(shortDay('2026-09-23')).toBe('Sep 23');
    expect(shortDay('2026-09-23T23:30:00Z')).toBe('Sep 23');
    expect(shortDay('soon')).toBe('—');
  });

  it('tells a day cut by the window from a whole one', () => {
    const usage = aggregate();
    expect(
      isPartialDay({
        date: '2026-09-21',
        since: '2026-09-21T09:31:00Z',
        until: '2026-09-22T00:00:00Z',
        usage,
      }),
    ).toBe(true);
    expect(
      isPartialDay({
        date: '2026-09-22',
        since: '2026-09-22T00:00:00Z',
        until: '2026-09-23T00:00:00Z',
        usage,
      }),
    ).toBe(false);
  });

  it('labels every day of a week and five days of a longer window', () => {
    expect(tickIndexes(8)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect(tickIndexes(31)).toEqual([0, 8, 15, 23, 30]);
    expect(tickIndexes(91)).toEqual([0, 23, 45, 68, 90]);
  });
});

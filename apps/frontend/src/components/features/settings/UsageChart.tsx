'use client';

import { useState } from 'react';
import { useLocale } from '@/components/providers';
import type { UsageBucket } from '@/lib/api/ownerUsage';
import type { TranslationKey } from '@/lib/i18n/translate';
import {
  CHART_METRICS,
  bucketLabel,
  crossesYear,
  dayValue,
  formatDayValue,
  tickIndexes,
  type ChartDay,
  type ChartMetric,
} from './usagePresent';

const CHART_LABEL: Record<ChartMetric, TranslationKey> = {
  cost: 'settings.usage.chart.cost',
  tokens: 'settings.usage.chart.tokens',
  running: 'settings.usage.chart.running',
  cpu: 'settings.usage.chart.cpu',
  memory: 'settings.usage.chart.memory',
};

const CHART_TITLE: Record<UsageBucket, TranslationKey> = {
  day: 'settings.usage.chartLabel',
  week: 'usage.chart.byWeek',
  month: 'usage.chart.byMonth',
};

/**
 * One bar per bucket the server returned: a day, a week or a month. The bars
 * are drawn as given; nothing here adds buckets together, so an amount shown
 * is always one the server summed.
 */
export function UsageChart({ days, bucket }: { days: ChartDay[]; bucket: UsageBucket }) {
  const { t, locale } = useLocale();
  const [metric, setMetric] = useState<ChartMetric>('cost');
  const [pointed, setPointed] = useState<number | null>(null);
  if (days.length === 0) return null;
  const values = days.map((day) => dayValue(day, metric));
  const peak = Math.max(0, ...values.map((value) => value ?? 0));
  const ticks = new Set(tickIndexes(days.length));
  const phoneTicks = new Set(tickIndexes(days.length, 4));
  const shown = pointed !== null && pointed < days.length ? pointed : days.length - 1;
  const label = t(CHART_LABEL[metric]);
  const withYear = crossesYear(days.map((day) => day.date));
  const name = (day: ChartDay) => bucketLabel(day.date, bucket, locale, withYear);
  const reading = (index: number) => {
    const day = days[index];
    const partial = bucket === 'day' ? t('settings.usage.today') : t('usage.chart.soFar');
    const value = values[index];
    return `${name(day)}${day.whole ? '' : ` (${partial})`} · ${
      value === null ? t('settings.usage.notRecorded') : formatDayValue(metric, value)
    }`;
  };
  const title = t(CHART_TITLE[bucket], { metric: label });
  return (
    <section aria-label={title} className="mt-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div
          role="group"
          aria-label={t('settings.usage.chartMetric')}
          className="flex flex-wrap gap-1 rounded-lg border border-border bg-surface-muted p-1"
        >
          {CHART_METRICS.map((item) => (
            <button
              key={item}
              type="button"
              aria-pressed={item === metric}
              onClick={() => setMetric(item)}
              className={`ui-focus-ring min-h-8 rounded-md px-2.5 text-xs font-medium transition-colors [@media(pointer:coarse)]:min-h-11 ${
                item === metric
                  ? 'bg-surface text-foreground shadow-subtle'
                  : 'text-muted hover:bg-surface-hover hover:text-foreground'
              }`}
            >
              {t(CHART_LABEL[item])}
            </button>
          ))}
        </div>
        <p aria-hidden="true" className="text-xs tabular-nums text-muted">
          {reading(shown)}
        </p>
      </div>
      <div aria-hidden="true" className="relative mt-3 h-36 border-b border-border">
        <div className="absolute inset-x-0 top-0 border-t border-dashed border-border" />
        <span className="absolute left-0 top-1 text-[11px] tabular-nums text-muted">
          {formatDayValue(metric, peak)}
        </span>
        <div className={`flex h-full items-end pt-6 ${days.length > 60 ? 'gap-px' : 'gap-[2px]'}`}>
          {days.map((day, index) => {
            const value = values[index];
            const height = peak > 0 && value ? Math.max(2, (value / peak) * 100) : 0;
            return (
              <div
                key={day.date}
                onMouseEnter={() => setPointed(index)}
                onMouseLeave={() => setPointed(null)}
                className="group flex h-full min-w-0 flex-1 items-end justify-center"
              >
                {value === null ? (
                  // Unknown, not zero: marked rather than drawn as an empty bar.
                  <span className="block w-full max-w-8 border-t-2 border-dotted border-muted" />
                ) : (
                  <span
                    className={`block w-full max-w-8 rounded-t-[4px] transition-colors ${
                      index === pointed ? 'bg-accent' : 'bg-accent/40 group-hover:bg-accent'
                    } ${day.whole ? '' : 'opacity-60'}`}
                    style={{ height: `${height}%` }}
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>
      <div aria-hidden="true" className="relative mt-1.5 h-4 text-[11px] tabular-nums text-muted">
        {days.map((day, index) =>
          ticks.has(index) ? (
            <span
              key={day.date}
              className={`absolute top-0 whitespace-nowrap ${phoneTicks.has(index) ? '' : 'max-sm:hidden'}`}
              style={tickPosition(index, days.length)}
            >
              {name(day)}
            </span>
          ) : null,
        )}
      </div>
      <table className="sr-only">
        <caption>{title}</caption>
        <tbody>
          {days.map((day, index) => (
            <tr key={day.date}>
              <td>{reading(index)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/** Where a bucket's label sits: under its bar, kept inside the chart at both ends. */
function tickPosition(index: number, total: number): React.CSSProperties {
  if (index === 0) return { left: 0 };
  if (index === total - 1) return { right: 0 };
  return { left: `${((index + 0.5) / total) * 100}%`, transform: 'translateX(-50%)' };
}

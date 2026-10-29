'use client';

import { useState } from 'react';
import { useLocale } from '@/components/providers';
import type { TranslationKey, Translator } from '@/lib/i18n/translate';
import {
  CHART_METRICS,
  chartBars,
  crossesYear,
  formatDayValue,
  shortDay,
  tickIndexes,
  type ChartBar,
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

/**
 * One bar per UTC day of the period, or per week of a long one. A period of
 * one day has nothing to compare and draws no chart; its tiles say it all.
 */
export function UsageChart({ days, locale }: { days: ChartDay[]; locale: string }) {
  const { t } = useLocale();
  const [metric, setMetric] = useState<ChartMetric>('cost');
  const [pointed, setPointed] = useState<number | null>(null);
  if (days.length < 2) return null;
  const bars = chartBars(days, metric);
  const weekly = bars.length < days.length;
  const withYear = crossesYear(bars);
  const peak = Math.max(0, ...bars.map((bar) => bar.value ?? 0));
  const ticks = new Set(tickIndexes(bars.length));
  const phoneTicks = new Set(tickIndexes(bars.length, 4));
  const shown = pointed !== null && pointed < bars.length ? pointed : bars.length - 1;
  const label = t(CHART_LABEL[metric]);
  const title = t(weekly ? 'settings.usage.chartLabelWeekly' : 'settings.usage.chartLabel', {
    metric: label,
  });
  const reading = (index: number) => {
    const bar = bars[index];
    const value = bar.value;
    return `${barName(bar, { weekly, locale, withYear, t })} · ${
      value === null ? t('settings.usage.notRecorded') : formatDayValue(metric, value)
    }`;
  };
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
        <div className="flex h-full items-end gap-[2px] pt-6">
          {bars.map((bar, index) => {
            const value = bar.value;
            const height = peak > 0 && value ? Math.max(2, (value / peak) * 100) : 0;
            return (
              <div
                key={bar.first}
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
                    } ${bar.whole ? '' : 'opacity-60'}`}
                    style={{ height: `${height}%` }}
                  />
                )}
              </div>
            );
          })}
        </div>
      </div>
      <div aria-hidden="true" className="relative mt-1.5 h-4 text-[11px] tabular-nums text-muted">
        {bars.map((bar, index) =>
          ticks.has(index) ? (
            <span
              key={bar.first}
              className={`absolute top-0 whitespace-nowrap ${phoneTicks.has(index) ? '' : 'max-sm:hidden'}`}
              style={tickPosition(index, bars.length)}
            >
              {shortDay(bar.first, locale, withYear)}
            </span>
          ) : null,
        )}
      </div>
      <table className="sr-only">
        <caption>{title}</caption>
        <tbody>
          {bars.map((bar, index) => (
            <tr key={bar.first}>
              <td>{reading(index)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/** `Oct 9`, or `Oct 6 – Oct 12` for a week, marked when it is not whole. */
function barName(
  bar: ChartBar,
  {
    weekly,
    locale,
    withYear,
    t,
  }: { weekly: boolean; locale: string; withYear: boolean; t: Translator },
): string {
  const first = shortDay(bar.first, locale, withYear);
  // A day is partial only while it is today.
  if (!weekly) return bar.whole ? first : `${first} (${t('settings.usage.today')})`;
  const range =
    bar.first === bar.last
      ? first
      : t('settings.usage.dateRange', { from: first, to: shortDay(bar.last, locale, withYear) });
  return bar.whole ? range : `${range} (${t('settings.usage.partialWeek')})`;
}

/** Where a bar's label sits: under its bar, kept inside the chart at both ends. */
function tickPosition(index: number, total: number): React.CSSProperties {
  if (index === 0) return { left: 0 };
  if (index === total - 1) return { right: 0 };
  return { left: `${((index + 0.5) / total) * 100}%`, transform: 'translateX(-50%)' };
}

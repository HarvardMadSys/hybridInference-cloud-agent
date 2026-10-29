'use client';

// Turns per UTC day over the selected range, one cell a day, Monday at the
// top. A day with turns is a button: hovering or focusing it tells its
// numbers, choosing it lists the conversations with turns that day (choosing
// it again shows every day). A long range is drawn a window of weeks at a
// time, and says which weeks are drawn.

import { useState } from 'react';
import { useLocale } from '@/components/providers';
import type { ContextActivityDay } from '@/lib/api/ownerUsage';
import {
  heatmapWeeks,
  heatmapWindow,
  type HeatmapCell,
  type HeatmapWeek,
} from './contextInsightsPresent';

const LEVEL_CLASS = [
  'bg-surface-muted',
  'bg-accent/25',
  'bg-accent/45',
  'bg-accent/70',
  'bg-accent',
];

export function ContextActivityHeatmap({
  since,
  until,
  activity,
  selected,
  onSelect,
}: {
  since: string;
  until: string;
  activity: ContextActivityDay[];
  selected: string | null;
  onSelect: (day: string | null) => void;
}) {
  const { t, locale } = useLocale();
  const [page, setPage] = useState(0);
  const weeks = heatmapWeeks(since, until, activity);
  const view = heatmapWindow(weeks, page);
  if (weeks.length === 0) return null;
  const first = view.weeks[0];
  const last = view.weeks[view.weeks.length - 1];
  return (
    <section aria-label={t('contextInsights.heatmap')} className="mt-4">
      <h4 className="text-xs font-semibold text-foreground">{t('contextInsights.heatmap')}</h4>
      <p className="mt-0.5 text-xs text-muted">{t('contextInsights.heatmap.note')}</p>
      <div className="mt-2 flex items-start gap-1 overflow-x-auto pb-1">
        <WeekdayLabels />
        {view.weeks.map((week, index) => (
          <div key={week.start} className="flex shrink-0 flex-col gap-1">
            {/* As wide as a cell: a month's name runs over the next columns. */}
            <span aria-hidden="true" className="relative h-3 w-3.5">
              <span className="absolute left-0 top-0 whitespace-nowrap text-[10px] leading-3 text-muted">
                {monthLabel(week, view.weeks[index - 1], locale)}
              </span>
            </span>
            {week.cells.map((cell) => (
              <Cell
                key={cell.day}
                cell={cell}
                selected={selected === cell.day}
                onSelect={onSelect}
              />
            ))}
          </div>
        ))}
      </div>
      <div className="mt-1 flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted">
        <span className="flex items-center gap-1" aria-hidden="true">
          {t('contextInsights.heatmap.less')}
          {LEVEL_CLASS.map((className) => (
            <span key={className} className={`h-2.5 w-2.5 rounded-sm ${className}`} />
          ))}
          {t('contextInsights.heatmap.more')}
        </span>
        {view.partial ? (
          <span className="flex flex-wrap items-center gap-2">
            <span>
              {t('contextInsights.heatmap.window', {
                from: first.start,
                to: last.cells[6].day,
              })}
            </span>
            <button
              type="button"
              disabled={view.page >= view.pages - 1}
              onClick={() => setPage(view.page + 1)}
              className="ui-button ui-button-ghost min-h-8 px-2 py-0.5 text-[11px] disabled:opacity-50 [@media(pointer:coarse)]:min-h-11"
            >
              {t('contextInsights.heatmap.earlier')}
            </button>
            <button
              type="button"
              disabled={view.page === 0}
              onClick={() => setPage(view.page - 1)}
              className="ui-button ui-button-ghost min-h-8 px-2 py-0.5 text-[11px] disabled:opacity-50 [@media(pointer:coarse)]:min-h-11"
            >
              {t('contextInsights.heatmap.later')}
            </button>
          </span>
        ) : null}
      </div>
    </section>
  );
}

const CELL = 'h-3.5 w-3.5 rounded-sm [@media(pointer:coarse)]:h-5 [@media(pointer:coarse)]:w-5';
/** 2026-10-26 was a Monday: the rows' weekdays, named in the reader's language. */
const WEEK_OF = [
  '2026-10-26',
  '2026-10-27',
  '2026-10-28',
  '2026-10-29',
  '2026-10-30',
  '2026-10-31',
  '2026-11-01',
];

/** Monday, Wednesday and Friday named beside their rows (UTC days). */
function WeekdayLabels() {
  const { locale } = useLocale();
  const format = new Intl.DateTimeFormat(locale, { weekday: 'short', timeZone: 'UTC' });
  return (
    <div aria-hidden="true" className="flex shrink-0 flex-col gap-1 pr-1 text-[10px] text-muted">
      <span className="h-3" />
      {WEEK_OF.map((day, index) => (
        <span key={day} className={`${CELL} flex items-center whitespace-nowrap leading-none`}>
          {index % 2 === 0 && index < 6 ? format.format(new Date(`${day}T00:00:00Z`)) : ''}
        </span>
      ))}
    </div>
  );
}

/** The month a week column starts, written where the month changes. */
function monthLabel(week: HeatmapWeek, previous: HeatmapWeek | undefined, locale: string): string {
  const first = week.cells.find((cell) => cell.inRange) ?? week.cells[0];
  const before = previous
    ? (previous.cells.find((cell) => cell.inRange) ?? previous.cells[0])
    : null;
  if (before && before.day.slice(0, 7) === first.day.slice(0, 7)) return '';
  return new Intl.DateTimeFormat(locale, { month: 'short', timeZone: 'UTC' }).format(
    new Date(`${first.day}T00:00:00Z`),
  );
}

function Cell({
  cell,
  selected,
  onSelect,
}: {
  cell: HeatmapCell;
  selected: boolean;
  onSelect: (day: string | null) => void;
}) {
  const { t, locale } = useLocale();
  const base = CELL;
  if (!cell.inRange) return <span aria-hidden="true" className={`${base} opacity-0`} />;
  const entry = cell.activity;
  if (!entry || entry.turns === 0) {
    return (
      <span
        title={t('contextInsights.heatmap.empty', { day: cell.day })}
        className={`${base} ${LEVEL_CLASS[0]}`}
      />
    );
  }
  const label = t('contextInsights.heatmap.cell', {
    day: cell.day,
    turns: entry.turns.toLocaleString(locale),
    conversations: entry.conversations.toLocaleString(locale),
    requests: entry.main_requests.toLocaleString(locale),
    compactions: entry.compactions.toLocaleString(locale),
  });
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      aria-pressed={selected}
      data-day={cell.day}
      onClick={() => onSelect(selected ? null : cell.day)}
      className={`${base} ${LEVEL_CLASS[cell.level]} ui-focus-ring ${
        selected ? 'ring-2 ring-foreground ring-offset-1 ring-offset-surface' : ''
      }`}
    />
  );
}

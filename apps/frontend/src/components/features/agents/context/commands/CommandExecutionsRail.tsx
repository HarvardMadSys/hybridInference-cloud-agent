'use client';

// The Command Executions rail and list column: summary tiles, attention,
// programs, and the scrolling row list. Split from the section that owns the
// read so each file stays within the repository's size limit; the data props
// are the same objects the section already received.

import { useLocale } from '@/components/providers';
import type { ThreadCommandsApi } from '@/lib/api/agents';
import { formatShare } from '@/components/features/settings/usagePresent';
import { clip, durationLabel } from './commandsPresent';

/** The rail is collapsible on a narrow card and always shown on a wide one. */
export function railClass(open: boolean): string {
  return open ? 'flex' : 'hidden';
}

export function SummaryRail({ page }: { page: ThreadCommandsApi | null }) {
  const { t, locale } = useLocale();
  // The main list column owns the initial loading/error state so it is visible
  // even when this rail is collapsed; nothing is duplicated here.
  if (!page) return null;
  const summary = page.summary;
  const failure =
    summary.failure_rate == null
      ? null
      : `${(summary.failure_rate * 100).toFixed(1).replace(/\.0$/, '')}%`;
  // No measured duration at all is unknown, not a zero sum; a recorded zero is
  // still 0ms because durations_known counts it.
  const cumulative = summary.durations_known === 0 ? null : durationLabel(summary.cumulative_ms);
  return (
    <div className="min-w-0 shrink-0">
      <h4
        className="mb-1.5 flex items-center gap-1 border-b border-border pb-1 text-[11px] font-semibold text-foreground"
        title={t('executions.rail.summaryTip')}
      >
        {t('executions.rail.summary')}
      </h4>
      <dl className="grid grid-cols-2 gap-x-2.5 gap-y-1.5">
        <Fact value={summary.settled.toLocaleString(locale)} label={t('executions.rail.settled')} />
        <Fact
          value={
            failure === null
              ? summary.failed.toLocaleString(locale)
              : `${summary.failed.toLocaleString(locale)} · ${failure}`
          }
          label={t('executions.rail.failed')}
          title={t('executions.rail.failedTip')}
          tone="danger"
        />
        <Fact
          value={cumulative ?? t('executions.duration.none')}
          label={t('executions.rail.cumulative')}
          title={t('executions.rail.cumulativeTip')}
          tone="time"
        />
        <Fact
          value={summary.background.toLocaleString(locale)}
          label={t('executions.rail.background')}
          tone="background"
        />
      </dl>
      {summary.durations_missing > 0 ? (
        <p className="mt-1 text-[10px] text-muted">
          {t('executions.duration.none')}: {summary.durations_missing.toLocaleString(locale)}
        </p>
      ) : null}
    </div>
  );
}

/** One KPI tile: a bold value above a short label, as DSH's summary grid. */
function Fact({
  label,
  value,
  title,
  tone,
}: {
  label: string;
  value: string;
  title?: string;
  tone?: 'danger' | 'time' | 'background';
}) {
  const colour =
    tone === 'danger'
      ? 'text-rose-600 dark:text-rose-400'
      : tone === 'time'
        ? 'text-sky-700 dark:text-sky-300'
        : tone === 'background'
          ? 'text-teal-700 dark:text-teal-300'
          : 'text-foreground';
  return (
    <div className="min-w-0 rounded-md bg-surface-muted px-1.5 py-1" title={title}>
      <dd className={`truncate text-sm font-bold leading-tight tabular-nums ${colour}`}>{value}</dd>
      <dt className="mt-0.5 truncate text-[10px] text-muted">{label}</dt>
    </div>
  );
}

export function Programs({
  page,
  filterText,
  onFilterText,
  onSelect,
}: {
  page: ThreadCommandsApi;
  filterText: string;
  onFilterText: (value: string) => void;
  onSelect: (name: string | null) => void;
}) {
  const { t, locale } = useLocale();
  const needle = filterText.trim().toLowerCase();
  const programs = page.programs.filter((program) => program.name.toLowerCase().includes(needle));
  if (page.programs.length === 0) return null;
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col">
      <div className="mb-1.5 flex flex-none items-center justify-between gap-2 border-b border-border pb-1">
        <h4
          className="min-w-0 truncate text-[11px] font-semibold text-foreground"
          title={t('executions.rail.programsScope')}
        >
          {t('executions.rail.programs', { count: page.programs.length.toLocaleString(locale) })}
        </h4>
        {page.programs.length > 6 || needle ? (
          <input
            type="search"
            value={filterText}
            onChange={(event) => onFilterText(event.target.value)}
            placeholder={t('executions.programSearch')}
            aria-label={t('executions.programSearchLabel')}
            className="ui-input min-h-6 w-24 shrink-0 px-1.5 text-[10px]"
          />
        ) : null}
      </div>
      <ul
        aria-label={t('executions.rail.programs', {
          count: page.programs.length.toLocaleString(locale),
        })}
        className="grid max-h-40 min-h-0 flex-1 gap-px overflow-y-auto overscroll-contain lg:max-h-none"
      >
        {programs.map((program) => (
          <li key={program.name}>
            <button
              type="button"
              aria-pressed={page.program === program.name}
              title={t(
                page.program === program.name
                  ? 'executions.rail.programTipOn'
                  : 'executions.rail.programTip',
                { name: program.name },
              )}
              onClick={() => onSelect(program.name)}
              className={`ui-focus-ring relative flex w-full items-baseline gap-1.5 rounded px-1 py-0.5 text-left text-[11px] leading-snug ${
                page.program === program.name
                  ? 'bg-accent/10 text-foreground'
                  : 'odd:bg-surface-muted/60 hover:bg-surface-hover'
              }`}
            >
              <span className="min-w-0 flex-1 truncate font-mono text-foreground">
                {program.name}
              </span>
              <span className="shrink-0 tabular-nums text-muted">
                ×{program.count.toLocaleString(locale)}
              </span>
              <span className="w-8 shrink-0 text-right text-[10px] tabular-nums text-muted">
                {formatShare(program.share)}
              </span>
              {/* A thin track inside the row: the share is visible without
                  adding tall spacing, as the DSH reference draws it. */}
              <span
                aria-hidden="true"
                className="pointer-events-none absolute bottom-0 left-1.5 h-px rounded bg-accent/40"
                style={{ width: `calc(${Math.max(2, program.share * 100)}% - 12px)` }}
              />
            </button>
          </li>
        ))}
      </ul>
      {programs.length === 0 ? (
        <p className="mt-2 text-[11px] text-muted">{t('executions.noMatch')}</p>
      ) : null}
    </div>
  );
}

export function Attention({
  page,
  onSelectProgram,
}: {
  page: ThreadCommandsApi;
  onSelectProgram: (name: string | null) => void;
}) {
  const { t, locale } = useLocale();
  const attention = page.attention;
  const rows: {
    key: string;
    label: string;
    value: string;
    count?: string;
    onClick?: () => void;
  }[] = [];
  if (attention.slowest) {
    const ms = durationLabel(attention.slowest.duration_ms);
    rows.push({
      key: 'slowest',
      label: t('executions.rail.slowest'),
      value: clip(attention.slowest.command, 34),
      count: ms ?? undefined,
    });
  }
  if (attention.failing) {
    const name = attention.failing.name;
    rows.push({
      key: 'failing',
      label: t('executions.rail.failing'),
      value: name,
      count: `×${attention.failing.count.toLocaleString(locale)}`,
      onClick: () => onSelectProgram(name),
    });
  }
  if (attention.repeats) {
    rows.push({
      key: 'repeats',
      label: t('executions.rail.repeats'),
      value: clip(attention.repeats.command, 34),
      count: `×${attention.repeats.count.toLocaleString(locale)}`,
    });
  }
  if (attention.background > 0) {
    rows.push({
      key: 'background',
      label: t('executions.rail.bgRuns'),
      value: '',
      count: `×${attention.background.toLocaleString(locale)}`,
    });
  }
  if (rows.length === 0) return null;
  return (
    <div className="min-w-0 shrink-0">
      <h4 className="mb-1 flex items-center gap-1 border-b border-border pb-1 text-[11px] font-semibold text-foreground">
        {t('executions.rail.attention')}
      </h4>
      <ul className="grid gap-px">
        {rows.map((row) => (
          <li key={row.key}>
            <button
              type="button"
              onClick={row.onClick}
              disabled={!row.onClick}
              className="ui-focus-ring flex w-full items-baseline gap-1.5 rounded px-1 py-0.5 text-left text-[11px] enabled:hover:bg-surface-hover disabled:cursor-default"
            >
              <span className="w-12 shrink-0 text-[10px] text-muted">{row.label}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-foreground">{row.value}</span>
              <span className="shrink-0 tabular-nums text-muted">{row.count}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function Coverage({ page, shown }: { page: ThreadCommandsApi; shown: number }) {
  const { t, locale } = useLocale();
  const coverage = page.coverage;
  const total = coverage.executions_total.toLocaleString(locale);
  const covered = coverage.executions_covered.toLocaleString(locale);
  const seen = shown.toLocaleString(locale);
  const remaining = page.page.total - shown;
  const rangeLine = t('executions.page.range', {
    from: '1',
    to: seen,
    total: String(page.page.total),
  });
  const moreLine = page.page.next_cursor
    ? ` ${t('executions.page.more', { count: String(remaining) })}`
    : '';
  return (
    <div className="mt-1 flex flex-none flex-wrap gap-x-2 gap-y-0.5 text-[10px] leading-tight text-muted">
      <p>
        {coverage.truncated
          ? t('executions.coverage', { covered, total })
          : t('executions.coverageAll', { total })}
      </p>
      {coverage.copied_jobs_excluded > 0 ? (
        <p>
          {t('executions.coverageCopied', {
            count: coverage.copied_jobs_excluded.toLocaleString(locale),
          })}
        </p>
      ) : null}
      {coverage.results_partial ? <p>{t('executions.coveragePartial')}</p> : null}
      {coverage.unsupported_runtimes.length > 0 ? (
        <p>{t('executions.unsupported', { runtimes: coverage.unsupported_runtimes.join(', ') })}</p>
      ) : null}
      {page.page.total > 0 ? (
        <p>
          {rangeLine}
          {moreLine}
        </p>
      ) : null}
    </div>
  );
}

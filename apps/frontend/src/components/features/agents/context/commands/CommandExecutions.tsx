'use client';

// Command Executions for one conversation: a cumulative summary, the programs
// it leaned on, the executions that stand out, and a searchable, sortable,
// expandable list. The page owns one read per (filter, sort, search, program)
// combination so chips, counts and rows always agree; "show more" appends the
// next page rather than re-reading the whole history.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocale } from '@/components/providers';
import {
  getThreadContextCommands,
  type CommandExecutionApi,
  type CommandFilter,
  type CommandSort,
  type ThreadCommandsApi,
} from '@/lib/api/agents';
import type { TranslationKey } from '@/lib/i18n/translate';
import { formatShare } from '@/components/features/settings/usagePresent';
import { Section, type OpenPoint, type OpenTurn } from '../ContextSections';
import { CommandRow } from './CommandRow';
import {
  COMMAND_FILTERS,
  COMMAND_SORTS,
  clip,
  cutoffLabel,
  durationLabel,
} from './commandsPresent';

const FILTER_LABEL: Record<CommandFilter, TranslationKey> = {
  all: 'executions.filter.all',
  failed: 'executions.filter.failed',
  slow: 'executions.filter.slow',
  background: 'executions.filter.background',
};

const SORT_LABEL: Record<CommandSort, TranslationKey> = {
  latest: 'executions.sort.latest',
  slowest: 'executions.sort.slowest',
};

const SEARCH_DEBOUNCE_MS = 250;

interface Query {
  filter: CommandFilter;
  sort: CommandSort;
  q: string;
  program: string | null;
}

export function CommandExecutions({
  jobId,
  conversationKey,
  onOpenPoint,
  onOpenTurn,
}: {
  jobId: string;
  /** The conversation the data belongs to; a change drops what was loaded. */
  conversationKey: string;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t, locale } = useLocale();
  const [query, setQuery] = useState<Query>({
    filter: 'all',
    sort: 'latest',
    q: '',
    program: null,
  });
  const [text, setText] = useState('');
  const [programText, setProgramText] = useState('');
  const [pages, setPages] = useState<ThreadCommandsApi[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const generation = useRef(0);

  // A search is applied after the reader stops typing; the other controls act
  // at once.
  useEffect(() => {
    if (text === query.q) return undefined;
    const timer = window.setTimeout(
      () => setQuery((previous) => ({ ...previous, q: text })),
      SEARCH_DEBOUNCE_MS,
    );
    return () => window.clearTimeout(timer);
  }, [text, query.q]);

  const load = useCallback(
    async (wanted: Query, cursor: string | null, append: boolean, signal: AbortSignal) => {
      const ticket = ++generation.current;
      setLoading(true);
      try {
        const page = await getThreadContextCommands(
          jobId,
          {
            filter: wanted.filter,
            sort: wanted.sort,
            q: wanted.q,
            program: wanted.program,
            cursor,
          },
          signal,
        );
        if (signal.aborted || ticket !== generation.current) return;
        if (page === null) {
          setPages([]);
          setError(null);
          return;
        }
        setError(null);
        setPages((previous) => (append && previous.length > 0 ? [...previous, page] : [page]));
      } catch (cause) {
        if (signal.aborted || ticket !== generation.current) return;
        setError(
          cause instanceof Error && cause.message ? cause.message : t('executions.loadError'),
        );
      } finally {
        if (!signal.aborted && ticket === generation.current) setLoading(false);
      }
    },
    [jobId, t],
  );

  useEffect(() => {
    const controller = new AbortController();
    setPages([]);
    void load(query, null, false, controller.signal);
    return () => controller.abort();
  }, [load, query, conversationKey]);

  const page = pages[0] ?? null;
  const executions = useMemo(
    () => (pages.length > 0 ? dedupe(pages.flatMap((entry) => entry.executions)) : []),
    [pages],
  );
  const last = pages.at(-1) ?? null;
  const more = last?.page.next_cursor ?? null;
  const cut = page?.slow_cutoff_ms ?? 10_000;
  const cutoff = cutoffLabel(cut);

  const showMore = () => {
    if (!more) return;
    const controller = new AbortController();
    void load(query, more, true, controller.signal);
  };

  const selectProgram = (name: string | null) => {
    setQuery((previous) => ({ ...previous, program: previous.program === name ? null : name }));
  };

  return (
    <Section title={t('executions.title')}>
      <p className="mt-1 text-xs text-muted">{t('executions.note')}</p>
      <div className="mt-3 grid gap-3 lg:grid-cols-[minmax(0,18rem)_minmax(0,1fr)] lg:items-start">
        <div className="grid min-w-0 gap-3">
          <SummaryRail
            page={page}
            loading={loading}
            error={error}
            onRetry={() => setQuery((v) => ({ ...v }))}
          />
          {page ? (
            <Programs
              page={page}
              filterText={programText}
              onFilterText={setProgramText}
              onSelect={selectProgram}
            />
          ) : null}
          {page ? <Attention page={page} onSelectProgram={selectProgram} /> : null}
        </div>
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="search"
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder={t('executions.search')}
              aria-label={t('executions.searchLabel')}
              className="ui-input min-h-9 min-w-0 flex-1 text-xs"
            />
            <div className="flex gap-1" role="group" aria-label={t('executions.sort')}>
              {COMMAND_SORTS.map((sort) => (
                <Chip
                  key={sort}
                  active={query.sort === sort}
                  onClick={() => setQuery((previous) => ({ ...previous, sort }))}
                >
                  {t(SORT_LABEL[sort])}
                </Chip>
              ))}
            </div>
          </div>
          {page ? (
            <div
              className="mt-2 flex flex-wrap gap-1"
              role="group"
              aria-label={t('executions.filter')}
            >
              {COMMAND_FILTERS.map((filter) => (
                <Chip
                  key={filter}
                  active={query.filter === filter}
                  title={filter === 'slow' ? t('executions.filter.slowTip', { cutoff }) : undefined}
                  onClick={() => setQuery((previous) => ({ ...previous, filter }))}
                >
                  {t(FILTER_LABEL[filter])} {page.counts[filter].toLocaleString(locale)}
                </Chip>
              ))}
            </div>
          ) : null}
          {page && query.program ? (
            <button
              type="button"
              onClick={() => selectProgram(null)}
              className="ui-button ui-button-ghost mt-2 min-h-8 px-2 text-[11px]"
            >
              {t('executions.rail.programTipOn', { name: query.program })}
            </button>
          ) : null}
          {page && page.executions.length === 0 ? (
            <p className="mt-3 rounded-lg border border-border px-3 py-2 text-xs text-muted">
              {t(page.summary.total === 0 ? 'executions.empty' : 'executions.noMatch')}
            </p>
          ) : (
            <ul
              aria-label={t('executions.title')}
              className="mt-2 divide-y divide-border rounded-lg border border-border py-1"
            >
              {executions.map((execution) => (
                <CommandRow
                  key={execution.id}
                  execution={execution}
                  jobId={jobId}
                  onOpenPoint={onOpenPoint}
                  onOpenTurn={onOpenTurn}
                />
              ))}
            </ul>
          )}
          {page ? <Coverage page={page} shown={executions.length} /> : null}
          {more ? (
            <button
              type="button"
              onClick={showMore}
              disabled={loading}
              className="ui-button ui-button-secondary mt-2 min-h-9 px-3 text-xs"
            >
              {t('executions.page.showMore')}
            </button>
          ) : null}
        </div>
      </div>
    </Section>
  );
}

function Chip({
  active,
  title,
  onClick,
  children,
}: {
  active: boolean;
  title?: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-pressed={active}
      title={title}
      onClick={onClick}
      className={`ui-focus-ring min-h-8 rounded-full border px-2.5 text-[11px] tabular-nums ${
        active
          ? 'border-accent bg-accent/10 text-foreground'
          : 'border-border text-muted hover:text-foreground'
      }`}
    >
      {children}
    </button>
  );
}

function SummaryRail({
  page,
  loading,
  error,
  onRetry,
}: {
  page: ThreadCommandsApi | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  const { t, locale } = useLocale();
  if (error && !page) {
    return (
      <div role="alert" className="rounded-lg border border-border p-3 text-xs">
        <p className="text-foreground">{t('executions.loadError')}</p>
        <p className="mt-1 text-muted">{error}</p>
        <button
          type="button"
          onClick={onRetry}
          className="ui-button ui-button-secondary mt-2 min-h-9"
        >
          {t('executions.retry')}
        </button>
      </div>
    );
  }
  if (!page) {
    return (
      <p role="status" className="rounded-lg border border-border p-3 text-xs text-muted">
        {loading ? t('executions.details.loading') : t('executions.empty')}
      </p>
    );
  }
  const summary = page.summary;
  const failure =
    summary.failure_rate == null
      ? null
      : `${(summary.failure_rate * 100).toFixed(1).replace(/\.0$/, '')}%`;
  const cumulative = durationLabel(summary.cumulative_ms);
  return (
    <div className="rounded-lg border border-border p-3">
      <h4 className="text-xs font-semibold text-foreground">{t('executions.rail.summary')}</h4>
      <p className="mt-0.5 text-[11px] text-muted">{t('executions.rail.summaryTip')}</p>
      <dl className="mt-2 grid grid-cols-2 gap-2">
        <Fact label={t('executions.rail.settled')} value={summary.settled.toLocaleString(locale)} />
        <Fact
          label={t('executions.rail.failed')}
          value={
            failure === null
              ? `${summary.failed.toLocaleString(locale)}`
              : `${summary.failed.toLocaleString(locale)} · ${failure}`
          }
          title={t('executions.rail.failedTip')}
        />
        <Fact
          label={t('executions.rail.cumulative')}
          value={cumulative ?? t('executions.duration.none')}
          title={t('executions.rail.cumulativeTip')}
        />
        <Fact
          label={t('executions.rail.background')}
          value={summary.background.toLocaleString(locale)}
        />
      </dl>
      <p className="mt-2 text-[11px] text-muted">{t('executions.rail.failedTip')}</p>
      <p className="mt-0.5 text-[11px] text-muted">
        {t('executions.rail.cumulativeTip')}
        {summary.durations_missing > 0
          ? ` · ${t('executions.duration.none')}: ${summary.durations_missing.toLocaleString(locale)}`
          : ''}
      </p>
    </div>
  );
}

function Fact({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="min-w-0" title={title}>
      <dt className="text-[11px] text-muted">{label}</dt>
      <dd className="mt-0.5 text-sm font-semibold tabular-nums text-foreground">{value}</dd>
    </div>
  );
}

function Programs({
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
    <div className="rounded-lg border border-border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-xs font-semibold text-foreground">
          {t('executions.rail.programs', { count: page.programs.length.toLocaleString(locale) })}
        </h4>
        <input
          type="search"
          value={filterText}
          onChange={(event) => onFilterText(event.target.value)}
          placeholder={t('executions.programSearch')}
          aria-label={t('executions.programSearchLabel')}
          className="ui-input min-h-8 w-32 text-[11px]"
        />
      </div>
      <p className="mt-0.5 text-[11px] text-muted">{t('executions.rail.programsScope')}</p>
      <ul className="mt-2 grid gap-1">
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
              className={`ui-focus-ring flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-[11px] ${
                page.program === program.name ? 'bg-accent/10' : 'hover:bg-surface-hover'
              }`}
            >
              <span className="min-w-0 flex-1 truncate font-mono text-foreground">
                {program.name}
              </span>
              <span className="shrink-0 tabular-nums text-muted">
                ×{program.count.toLocaleString(locale)}
              </span>
              <span className="w-10 shrink-0 text-right tabular-nums text-muted">
                {formatShare(program.share)}
              </span>
            </button>
            <div
              aria-hidden="true"
              className="mt-0.5 h-1 rounded-full bg-accent/30"
              style={{ width: `${Math.max(2, program.share * 100)}%` }}
            />
          </li>
        ))}
      </ul>
      {programs.length === 0 ? (
        <p className="mt-2 text-[11px] text-muted">{t('executions.noMatch')}</p>
      ) : null}
    </div>
  );
}

function Attention({
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
    <div className="rounded-lg border border-border p-3">
      <h4 className="text-xs font-semibold text-foreground">{t('executions.rail.attention')}</h4>
      <ul className="mt-1.5 grid gap-1">
        {rows.map((row) => (
          <li key={row.key}>
            <button
              type="button"
              onClick={row.onClick}
              disabled={!row.onClick}
              className="ui-focus-ring flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-[11px] enabled:hover:bg-surface-hover disabled:cursor-default"
            >
              <span className="w-16 shrink-0 text-muted">{row.label}</span>
              <span className="min-w-0 flex-1 truncate font-mono text-foreground">{row.value}</span>
              <span className="shrink-0 tabular-nums text-muted">{row.count}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Coverage({ page, shown }: { page: ThreadCommandsApi; shown: number }) {
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
    <div className="mt-2 grid gap-1 text-[11px] text-muted">
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

/** A later page repeating a row already shown is the same execution, once. */
function dedupe(rows: CommandExecutionApi[]): CommandExecutionApi[] {
  const seen = new Set<string>();
  return rows.filter((row) => {
    if (seen.has(row.id)) return false;
    seen.add(row.id);
    return true;
  });
}

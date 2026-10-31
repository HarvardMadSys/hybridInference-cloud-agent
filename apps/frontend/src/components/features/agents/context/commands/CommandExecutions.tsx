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
import type { OpenPoint, OpenTurn } from '../ContextSections';
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

/** How often a running conversation's commands are read again while visible. */
export const COMMANDS_REFRESH_MS = 5_000;

/** Read one page and apply it, unless a newer read or an abort won. */
async function fetchPage(
  jobId: string,
  wanted: Query,
  cursor: string | null,
  signal: AbortSignal,
  ticket: number,
  generation: { current: number },
  apply: {
    clear: () => void;
    append: (page: ThreadCommandsApi, add: boolean) => void;
    fail: (message: string) => void;
    fallback: string;
  },
  append: boolean,
) {
  try {
    const page = await getThreadContextCommands(
      jobId,
      { filter: wanted.filter, sort: wanted.sort, q: wanted.q, program: wanted.program, cursor },
      signal,
    );
    if (signal.aborted || ticket !== generation.current) return;
    if (page === null) {
      apply.clear();
      return;
    }
    apply.append(page, append);
  } catch (cause) {
    if (signal.aborted || ticket !== generation.current) return;
    apply.fail(cause instanceof Error && cause.message ? cause.message : apply.fallback);
  }
}

/** A query's identity for the refresh effect, without an inline nullish branch. */
function queryKeyOf(query: Query): string {
  return [query.filter, query.sort, query.q, query.program].map((part) => part ?? '').join('|');
}

/** Apply typed search text once the reader pauses. */
function useDebouncedSearch(text: string, applied: string, apply: (q: string) => void) {
  useEffect(() => {
    if (text === applied) return undefined;
    const timer = window.setTimeout(() => apply(text), SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [text, applied, apply]);
}

/** Re-read a running conversation's first page on a stable cadence. */
function useCommandRefresh(
  active: boolean,
  queryRef: { current: Query },
  busy: { current: boolean },
  pending: { current: boolean },
  runPending: { current: () => void },
  queryKey: string,
) {
  // A running turn adds commands and settles running ones: read the first page
  // again on the shared cadence, only while visible and never overlapping a
  // read. Nothing the read itself changes is a dependency, so a refresh cannot
  // abort itself.
  useEffect(() => {
    if (!active) return undefined;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      pending.current = true;
      runPending.current();
    }, COMMANDS_REFRESH_MS);
    return () => window.clearInterval(timer);
    // The query and the load function are reached through refs on purpose:
    // the read's own state changes must not re-run this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, queryKey]);

  // The turn just settled: read once more so a command that finished between
  // polls does not stay "Running" with no further refresh scheduled.
  const wasActive = useRef(active);
  useEffect(() => {
    const settled = wasActive.current && !active;
    wasActive.current = active;
    if (!settled) return;
    pending.current = true;
    runPending.current();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active, queryKey]);
}

export function CommandExecutions({
  jobId,
  conversationKey,
  active = false,
  onOpenPoint,
  onOpenTurn,
}: {
  jobId: string;
  /** The conversation the data belongs to; a change drops what was loaded. */
  conversationKey: string;
  /** The turn is still running: read again while the page is visible. */
  active?: boolean;
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
  // A narrow card keeps the rail collapsed so the command list stays primary.
  const [railOpen, setRailOpen] = useState(false);
  const [pages, setPages] = useState<ThreadCommandsApi[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const generation = useRef(0);

  // A search is applied after the reader stops typing; the other controls act
  // at once.
  useDebouncedSearch(text, query.q, (q) => setQuery((previous) => ({ ...previous, q })));

  // The request in flight, so a refresh never overlaps another read and its
  // controller is not tied to an effect that a state change re-runs.
  const inFlight = useRef<AbortController | null>(null);
  const busy = useRef(false);
  const queryRef = useRef(query);
  queryRef.current = query;

  const runPendingRef = useRef<() => void>(() => {});
  const load = useCallback(
    async (wanted: Query, cursor: string | null, append: boolean) => {
      if (busy.current) return;
      const controller = new AbortController();
      inFlight.current = controller;
      busy.current = true;
      const ticket = ++generation.current;
      setLoading(true);
      try {
        await fetchPage(
          jobId,
          wanted,
          cursor,
          controller.signal,
          ticket,
          generation,
          {
            clear: () => {
              setPages([]);
              setError(null);
            },
            append: (page, add) => {
              // A successful page clears an earlier read error.
              setError(null);
              setPages((previous) => (add && previous.length > 0 ? [...previous, page] : [page]));
            },
            fail: (message) => setError(message),
            fallback: t('executions.loadError'),
          },
          append,
        );
      } finally {
        // Cleanup belongs to this request only: an older request finishing
        // after a newer one started must not clear the newer one's state.
        if (inFlight.current === controller) {
          inFlight.current = null;
          busy.current = false;
          if (!controller.signal.aborted && ticket === generation.current) setLoading(false);
          runPendingRef.current();
        }
      }
    },
    [jobId, t],
  );

  const cancel = useCallback(() => {
    inFlight.current?.abort();
    inFlight.current = null;
    busy.current = false;
  }, []);

  const pending = useRef(false);
  const loadRef = useRef(load);
  loadRef.current = load;
  const runPending = useCallback(() => {
    if (!pending.current || busy.current) return;
    // A hidden tab keeps the request queued: the visibility listener runs it
    // when the reader returns instead of dropping it here.
    if (document.visibilityState !== 'visible') return;
    pending.current = false;
    void loadRef.current(queryRef.current, null, false);
  }, []);
  // A queued final refresh waits for the in-flight read rather than being
  // dropped when it finds one, and runs when the tab is visible again.
  runPendingRef.current = runPending;
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === 'visible') runPending();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, [runPending]);

  // A new conversation or query starts from its first page and cancels the
  // previous read, so a paged list never mixes two generations.
  useEffect(() => {
    cancel();
    setPages([]);
    void load(query, null, false);
    return cancel;
  }, [load, query, conversationKey, cancel]);

  useCommandRefresh(active, queryRef, busy, pending, runPendingRef, queryKeyOf(query));

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
    void load(query, more, true);
  };

  const selectProgram = (name: string | null) => {
    setQuery((previous) => ({ ...previous, program: previous.program === name ? null : name }));
  };

  return (
    <section className="mt-6 min-w-0">
      {/* One compact DSH-style card: a title row, a bounded body, and a list
          that scrolls inside the card rather than growing the page. */}
      <div className="ui-card flex max-h-[560px] min-w-0 flex-col overflow-hidden">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-border px-3 py-1.5">
          <h3 className="shrink-0 text-xs font-semibold text-foreground">
            {t('executions.title')}
          </h3>
          <div
            className="flex min-w-0 flex-wrap items-center gap-1"
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
                {t(FILTER_LABEL[filter])}
                {page ? (
                  <b className="ml-1 font-semibold tabular-nums">
                    {page.counts[filter].toLocaleString(locale)}
                  </b>
                ) : null}
              </Chip>
            ))}
          </div>
          <button
            type="button"
            aria-expanded={railOpen}
            aria-controls="command-executions-rail"
            onClick={() => setRailOpen((open) => !open)}
            className="ui-focus-ring ml-auto min-h-7 shrink-0 rounded-md border border-border px-2 text-[11px] text-muted hover:text-foreground lg:hidden"
          >
            {railOpen ? t('executions.rail.hide') : t('executions.rail.show')}
          </button>
        </div>
        <div className="grid min-h-0 flex-1 gap-3 p-2.5 lg:grid-cols-[minmax(180px,1fr)_minmax(0,2fr)]">
          <div
            id="command-executions-rail"
            className={`${railClass(railOpen)} min-h-0 min-w-0 flex-col gap-2 lg:flex`}
          >
            <SummaryRail
              page={page}
              loading={loading}
              error={error}
              onRetry={() => setQuery((v) => ({ ...v }))}
            />
            {page ? <Attention page={page} onSelectProgram={selectProgram} /> : null}
            {page ? (
              <Programs
                page={page}
                filterText={programText}
                onFilterText={setProgramText}
                onSelect={selectProgram}
              />
            ) : null}
          </div>
          <div className="flex min-h-0 min-w-0 flex-col">
            <div className="flex flex-wrap items-center gap-1.5">
              <input
                type="search"
                value={text}
                onChange={(event) => setText(event.target.value)}
                placeholder={t('executions.search')}
                aria-label={t('executions.searchLabel')}
                className="ui-input min-h-7 min-w-0 flex-1 px-2 text-[11px]"
              />
              <div className="flex shrink-0 gap-1" role="group" aria-label={t('executions.sort')}>
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
            {page && query.program ? (
              <button
                type="button"
                onClick={() => selectProgram(null)}
                className="ui-focus-ring mt-1 w-fit rounded px-1 text-left text-[11px] text-accent hover:underline"
              >
                {t('executions.rail.programTipOn', { name: query.program })}
              </button>
            ) : null}
            {page && page.executions.length === 0 ? (
              <p className="mt-2 rounded-md border border-border px-2 py-1.5 text-[11px] text-muted">
                {t(page.summary.total === 0 ? 'executions.empty' : 'executions.noMatch')}
              </p>
            ) : (
              // The list owns the remaining height and scrolls internally, so
              // expanded output and many rows never grow the page.
              <ul
                aria-label={t('executions.title')}
                className="mt-1.5 min-h-[120px] flex-1 overflow-y-auto overscroll-contain"
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
                {more ? (
                  <li className="px-1 py-1">
                    <button
                      type="button"
                      onClick={showMore}
                      disabled={loading}
                      className="ui-focus-ring min-h-7 w-full rounded-md border border-border text-[11px] text-muted hover:text-foreground"
                    >
                      {t('executions.page.showMore')}
                    </button>
                  </li>
                ) : null}
              </ul>
            )}
            {page ? <Coverage page={page} shown={executions.length} /> : null}
          </div>
        </div>
      </div>
    </section>
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
        className="grid min-h-0 flex-1 gap-px overflow-y-auto overscroll-contain"
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
              className={`ui-focus-ring flex w-full items-baseline gap-1.5 rounded px-1 py-0.5 text-left text-[11px] leading-snug ${
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

/** The rail is collapsible on a narrow card and always shown on a wide one. */
function railClass(open: boolean): string {
  return open ? 'flex' : 'hidden';
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

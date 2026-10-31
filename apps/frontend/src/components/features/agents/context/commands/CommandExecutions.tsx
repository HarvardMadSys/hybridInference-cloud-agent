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
import type { OpenPoint, OpenTurn } from '../ContextSections';
import { CommandRow } from './CommandRow';
import { Attention, Coverage, Programs, SummaryRail, railClass } from './CommandExecutionsRail';
import { COMMAND_FILTERS, COMMAND_SORTS, cutoffLabel } from './commandsPresent';

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
            <SummaryRail page={page} />
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
            <CommandList
              page={page}
              error={error}
              executions={executions}
              more={more}
              loading={loading}
              jobId={jobId}
              onRetry={() => setQuery((v) => ({ ...v }))}
              onShowMore={showMore}
              onOpenPoint={onOpenPoint}
              onOpenTurn={onOpenTurn}
            />
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

/** The list column: loading/error/empty states, rows, and the next-page action. */
function CommandList({
  page,
  error,
  executions,
  more,
  loading,
  jobId,
  onRetry,
  onShowMore,
  onOpenPoint,
  onOpenTurn,
}: {
  page: ThreadCommandsApi | null;
  error: string | null;
  executions: CommandExecutionApi[];
  more: string | null;
  loading: boolean;
  jobId: string;
  onRetry: () => void;
  onShowMore: () => void;
  onOpenPoint: OpenPoint;
  onOpenTurn: OpenTurn;
}) {
  const { t } = useLocale();
  if (!page && error) {
    return (
      <div role="alert" className="mt-2 rounded-md border border-border px-2 py-1.5 text-[11px]">
        <p className="text-foreground">{t('executions.loadError')}</p>
        <p className="mt-0.5 text-muted">{error}</p>
        <button
          type="button"
          onClick={onRetry}
          className="ui-button ui-button-secondary mt-1.5 min-h-7 px-2 text-[11px]"
        >
          {t('executions.retry')}
        </button>
      </div>
    );
  }
  if (!page) {
    return (
      <p
        role="status"
        className="mt-2 rounded-md border border-border px-2 py-1.5 text-[11px] text-muted"
      >
        {t('executions.details.loading')}
      </p>
    );
  }
  if (page.executions.length === 0) {
    return (
      <p className="mt-2 rounded-md border border-border px-2 py-1.5 text-[11px] text-muted">
        {t(page.summary.total === 0 ? 'executions.empty' : 'executions.noMatch')}
      </p>
    );
  }
  return (
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
            onClick={onShowMore}
            disabled={loading}
            className="ui-focus-ring min-h-7 w-full rounded-md border border-border text-[11px] text-muted hover:text-foreground"
          >
            {t('executions.page.showMore')}
          </button>
        </li>
      ) : null}
    </ul>
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

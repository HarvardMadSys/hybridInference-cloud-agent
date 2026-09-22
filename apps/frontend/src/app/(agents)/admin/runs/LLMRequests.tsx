'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import {
  getAdminLlmRequestSources,
  getAdminLlmRequests,
  type AdminLlmRequest,
  type AdminLlmRequestPage,
  type AdminLlmRequestSource,
} from '@/lib/api/adminRuns';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import { accessDenied, BUTTON, CARD, dateTime, ErrorNotice, money, number } from './shared';

/** Fixed page the panel asks for; the server accepts 1..100. */
export const LLM_REQUEST_PAGE_SIZE = 50;

/** How often a visible, still-first-page read is repeated while a run executes. */
const AUTO_REFRESH_MS = 15_000;

const UNAVAILABLE = '—';

/** `HTTP 200`, never a claim about the task or the stream. */
export function statusLabel(code: number | null): string {
  return code == null ? UNAVAILABLE : `HTTP ${code}`;
}

/** The request model and, when the gateway served a different one, that too. */
export function modelLabel(row: AdminLlmRequest): { text: string; title?: string } {
  if (!row.model && !row.served_model_id) return { text: UNAVAILABLE };
  if (!row.served_model_id || row.served_model_id === row.model) {
    return { text: row.model ?? row.served_model_id ?? UNAVAILABLE };
  }
  if (!row.model) return { text: row.served_model_id ?? UNAVAILABLE };
  return {
    text: `${row.model} → ${row.served_model_id}`,
    title: `Requested ${row.model}; served ${row.served_model_id}`,
  };
}

function nonnegative(value: number | null): value is number {
  return value !== null && Number.isFinite(value) && value >= 0;
}

/** Keep short timings in milliseconds and longer ones in seconds. */
export function durationLabel(value: number | null): string {
  if (!nonnegative(value)) return UNAVAILABLE;
  return value < 1000 ? `${number(value)} ms` : `${(value / 1000).toFixed(3)} s`;
}

/** Input already includes cache reads; cache writes are not hits. */
export function cacheHitRate(row: AdminLlmRequest): number | null {
  const { tokens_in: input, cache_read: cached } = row;
  if (!nonnegative(input) || input === 0 || !nonnegative(cached) || cached > input) return null;
  return (cached / input) * 100;
}

/** An estimate: the ledger measures response completion, not the last token. */
export function timePerOutputToken(row: AdminLlmRequest): number | null {
  const { tokens_out: output, ttft_ms: first, latency_ms: total } = row;
  if (!nonnegative(output) || output <= 1 || !nonnegative(first) || !nonnegative(total))
    return null;
  if (total < first) return null;
  const value = (total - first) / (output - 1);
  return Number.isFinite(value) ? value : null;
}

function tokenLabel(value: number | null): string {
  return nonnegative(value) ? number(value) : UNAVAILABLE;
}

function startTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? UNAVAILABLE
    : date.toLocaleTimeString('en-GB', { timeZone: 'UTC', hour12: false });
}

const SOURCE_REASONS: Record<string, string> = {
  attribution_unavailable: 'This execution was never attributed to a gateway ledger.',
  binding_changed: 'This execution was attributed by a different gateway installation.',
  invalid_window: 'This execution has no usable attribution window yet.',
};

export function sourceExplanation(source: AdminLlmRequestSource): string {
  return (
    (source.reason ? SOURCE_REASONS[source.reason] : undefined) ??
    'Gateway request detail is unavailable for this execution.'
  );
}

/**
 * Whether a page continues the read currently on screen.
 *
 * A continuation is only valid for one source and one fixed window. The server
 * refuses a window that moved, but the panel also holds pages across a refresh,
 * and a page that answers the previous read must never be appended to the new
 * one under its header.
 */
export function continues(next: AdminLlmRequestPage, current: AdminLlmRequestPage): boolean {
  return (
    next.source_id === current.source_id &&
    next.window.since === current.window.since &&
    next.window.until === current.window.until
  );
}

function isRestart(error: unknown): boolean {
  return (
    error instanceof APIError &&
    (error.code === 'request_cursor_expired' || error.code === 'request_window_changed')
  );
}

/** Per-request metrics for the selected execution, loaded when the panel mounts. */
export function LLMRequests({
  jobId,
  attemptId,
  running,
  refreshKey = 0,
  onAccessDenied,
}: {
  jobId: string;
  attemptId?: number;
  running: boolean;
  refreshKey?: number;
  onAccessDenied: () => void;
}) {
  const [sources, setSources] = useState<AdminLlmRequestSource[] | null>(null);
  const [sourcesFor, setSourcesFor] = useState<string | null>(null);
  const [sourceReason, setSourceReason] = useState<string | null>(null);
  const [sourceId, setSourceId] = useState<string | null>(null);
  const [page, setPage] = useState<AdminLlmRequestPage | null>(null);
  const [rows, setRows] = useState<AdminLlmRequest[]>([]);
  const [pagesLoaded, setPagesLoaded] = useState(0);
  const [loadingSources, setLoadingSources] = useState(false);
  const [loading, setLoading] = useState(false);
  const [moreLoading, setMoreLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [restart, setRestart] = useState(false);
  const [stale, setStale] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const sourcesRequest = useRef<AbortController | null>(null);
  const pageRequest = useRef<AbortController | null>(null);
  const moreRequest = useRef<AbortController | null>(null);
  // Two counters, because a page read and the source read start for different
  // reasons: replacing a page must not invalidate the list that is still being
  // read, and re-reading the list must invalidate every page read.
  const sourcesGeneration = useRef(0);
  const pageGeneration = useRef(0);
  const pageRun = useRef(0);

  const executionKey = `${jobId}:${attemptId ?? ''}`;
  const key = `${executionKey}:${sourceId ?? ''}`;
  const loadedKey = useRef('');
  const loadedExecution = useRef('');

  /**
   * Retire the page reads that belong to the source being replaced.
   *
   * The generation is bumped here rather than only where a read starts, so a
   * response that is already in flight cannot be mistaken for one of the new
   * selection: it is dropped, not appended, and its cursor never replaces the
   * current one.
   */
  const retirePages = useCallback(() => {
    pageRequest.current?.abort();
    pageRequest.current = null;
    moreRequest.current?.abort();
    moreRequest.current = null;
    // Every flag a retired request would have cleared is cleared here, so a
    // cancelled read can never leave a control disabled for good.
    setLoading(false);
    setMoreLoading(false);
    pageGeneration.current += 1;
    return pageGeneration.current;
  }, []);

  /**
   * Retire every read, the source list included.
   *
   * Used when the execution itself changes: the list has to be re-read, so the
   * read that is doing it is the one thing that must survive. Clearing the
   * selected source changes the panel's selection key, which is why retirement
   * there must not reach the source request.
   */
  const retireAll = useCallback(() => {
    sourcesRequest.current?.abort();
    sourcesRequest.current = null;
    setLoadingSources(false);
    retirePages();
    sourcesGeneration.current += 1;
    return sourcesGeneration.current;
  }, [retirePages]);

  const fail = useCallback(
    (caught: unknown) => {
      if (accessDenied(caught)) {
        onAccessDenied();
        return;
      }
      if (isRestart(caught)) {
        setRestart(true);
        setError(getErrorMessage(caught));
        return;
      }
      setError(getErrorMessage(caught));
    },
    [onAccessDenied],
  );

  // The references this execution holds. Metadata only: no gateway is called
  // until one of them is actually read.
  useEffect(() => {
    const controller = new AbortController();
    const current = retireAll();
    sourcesRequest.current = controller;
    const executionChanged = loadedExecution.current !== executionKey;
    loadedExecution.current = executionKey;
    if (executionChanged) {
      // A different execution holds different references. Its source id is not
      // this execution's to query, and nothing read for the previous one may
      // stay on screen while the new list is being read.
      setSourceId(null);
      setSourcesFor(null);
      setRows([]);
      setPage(null);
      setPagesLoaded(0);
      setStale(null);
      loadedKey.current = '';
    }
    setLoadingSources(true);
    setSources(null);
    setSourceReason(null);
    setError(null);
    setRestart(false);
    getAdminLlmRequestSources(jobId, { attempt_id: attemptId, limit: 100 }, controller.signal)
      .then((next) => {
        if (controller.signal.aborted || sourcesGeneration.current !== current) return;
        setSources(next.sources);
        setSourcesFor(executionKey);
        setSourceReason(next.reason);
        const queryable = next.sources.filter((source) => source.availability === 'queryable');
        setSourceId((chosen) =>
          chosen && queryable.some((source) => source.source_id === chosen)
            ? chosen
            : queryable.length === 1
              ? queryable[0].source_id
              : null,
        );
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted && sourcesGeneration.current === current) fail(caught);
      })
      .finally(() => {
        if (!controller.signal.aborted && sourcesGeneration.current === current)
          setLoadingSources(false);
      });
    return () => {
      controller.abort();
      if (sourcesRequest.current === controller) sourcesRequest.current = null;
    };
  }, [jobId, attemptId, executionKey, revision, refreshKey, retireAll, fail]);

  // One page at a time, for one reference. A refresh keeps the rows that are
  // already on screen until the new ones arrive; switching reference clears
  // them first so a late response cannot fill the new selection.
  useEffect(() => {
    // The selected source is only usable once the list in front of us was read
    // for this execution: on the render where the execution changes, the old
    // id is still in state and must not be queried.
    if (!sourceId || sourcesFor !== executionKey) return;
    const controller = new AbortController();
    const current = pageGeneration.current;
    const run = ++pageRun.current;
    pageRequest.current = controller;
    const sameSelection = loadedKey.current === key;
    loadedKey.current = key;
    if (!sameSelection) {
      setRows([]);
      setPage(null);
      setPagesLoaded(0);
      setStale(null);
    }
    setLoading(true);
    setError(null);
    setRestart(false);
    getAdminLlmRequests(
      jobId,
      { source_id: sourceId, limit: LLM_REQUEST_PAGE_SIZE },
      controller.signal,
    )
      .then((next) => {
        if (controller.signal.aborted || pageGeneration.current !== current) return;
        if (pageRun.current !== run) return;
        setPage(next);
        setRows(next.requests);
        setPagesLoaded(1);
        setStale(next.fetched_at);
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted && pageGeneration.current === current) fail(caught);
      })
      .finally(() => {
        if (!controller.signal.aborted && pageGeneration.current === current) setLoading(false);
      });
    return () => {
      controller.abort();
      if (pageRequest.current === controller) pageRequest.current = null;
    };
  }, [jobId, executionKey, sourceId, sourcesFor, revision, refreshKey, key, fail]);

  // Changing job, execution or source retires every in-flight read first, so a
  // page that arrives late is dropped instead of being appended to a list it
  // does not describe. The same cleanup covers unmounting the panel.
  useEffect(
    () => () => {
      retirePages();
    },
    [key, retirePages],
  );

  // Only a visible first page of a running execution keeps re-reading itself,
  // and never while a read is already in flight. After "load more" the page set
  // is a deliberate read and stops moving.
  useEffect(() => {
    if (!sourceId || !running || pagesLoaded !== 1 || restart) return;
    if (loading || moreLoading) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      setRevision((value) => value + 1);
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [sourceId, running, pagesLoaded, restart, loading, moreLoading]);

  async function loadMore() {
    // A first-page read replaces the page this continuation came from, so a
    // page must never be fetched while one is in flight: its cursor belongs to
    // the read that is being replaced.
    if (!page?.next_cursor || !sourceId || loading || moreLoading) return;
    const controller = new AbortController();
    const current = pageGeneration.current;
    const run = pageRun.current;
    moreRequest.current = controller;
    setMoreLoading(true);
    setError(null);
    try {
      const next = await getAdminLlmRequests(
        jobId,
        { source_id: sourceId, limit: LLM_REQUEST_PAGE_SIZE, cursor: page.next_cursor },
        controller.signal,
      );
      if (controller.signal.aborted || pageGeneration.current !== current) return;
      if (pageRun.current !== run) return;
      if (!continues(next, page)) {
        // The continuation answered a read that is no longer on screen. Its
        // rows describe a different window, and appending them would mix two
        // reads under one header.
        setRestart(true);
        setError('That page belongs to a different read. Load the first page again.');
        return;
      }
      setPage(next);
      setRows((previous) => [...previous, ...next.requests]);
      setPagesLoaded((count) => count + 1);
      setStale(next.fetched_at);
    } catch (caught) {
      // The loaded pages and their cursor stay: the same continuation is worth
      // retrying, and a failed page is not the end of the result.
      if (!controller.signal.aborted && pageGeneration.current === current) fail(caught);
    } finally {
      // Cleared whenever this call is still the one the panel is waiting on,
      // including when a refresh or a selection change cancelled it. Leaving
      // the flag set on an aborted request disables "Load more" for good.
      if (moreRequest.current === controller) {
        moreRequest.current = null;
        setMoreLoading(false);
      }
    }
  }

  const queryable = (sources ?? []).filter((source) => source.availability === 'queryable');
  const unavailable = (sources ?? []).filter((source) => source.availability !== 'queryable');
  return (
    <section aria-labelledby="llm-requests-heading" className="mt-8">
      <h2
        id="llm-requests-heading"
        className="text-lg font-semibold text-gray-900 dark:text-gray-50"
      >
        LLM requests
      </h2>
      <div className={`${CARD} mt-4 overflow-hidden`}>
        {loadingSources && !page && (
          <p role="status" className="px-5 py-4 text-sm text-gray-500 dark:text-gray-400">
            Loading requests…
          </p>
        )}
        {queryable.length > 1 && (
          <label className="block px-5 py-4 text-sm">
            Source
            <select
              className="ml-3 rounded-lg border border-gray-200 bg-white px-2 py-1 text-sm dark:border-white/10 dark:bg-gray-900"
              value={sourceId ?? ''}
              onChange={(event) => setSourceId(event.target.value || null)}
            >
              <option value="">Choose an execution</option>
              {queryable.map((source) => (
                <option key={source.source_id} value={source.source_id ?? ''}>
                  Execution {source.attempt_no} · {dateTime(source.window_since)}
                </option>
              ))}
            </select>
          </label>
        )}
        {sources !== null && queryable.length === 0 && !loadingSources && (
          <p className="px-5 py-4 text-sm text-gray-500 dark:text-gray-400">
            {sourceReason === 'not_started'
              ? 'No execution has started in this run.'
              : 'This execution has no queryable gateway source.'}
          </p>
        )}
        {unavailable.length > 0 && (
          <ul className="space-y-1 px-5 py-3 text-xs text-amber-700 dark:text-amber-400">
            {unavailable.map((source) => (
              <li key={`${source.attempt_id}:${source.source_id ?? 'none'}`}>
                Execution {source.attempt_no}: {sourceExplanation(source)}
              </li>
            ))}
          </ul>
        )}
        {error && (
          <div className="px-5 pb-4">
            <ErrorNotice>
              {error}
              {rows.length > 0 && ` Showing the last successful read from ${dateTime(stale)}.`}
            </ErrorNotice>
            <button
              type="button"
              className={`${BUTTON} mt-2`}
              disabled={loading || loadingSources}
              onClick={() => setRevision((value) => value + 1)}
            >
              {restart ? 'Start again from the first page' : 'Retry'}
            </button>
          </div>
        )}
        {sourceId && (
          <>
            <div
              className="overflow-x-auto"
              role="region"
              aria-label="LLM request metrics"
              tabIndex={0}
            >
              <table
                aria-label="LLM requests"
                className="w-full min-w-[64rem] text-right text-sm tabular-nums"
              >
                <thead className="border-b border-gray-100 text-xs text-gray-500 dark:border-white/5 dark:text-gray-400">
                  <tr className="[&>th]:px-3 [&>th]:py-4 [&>th]:font-medium [&>th]:whitespace-nowrap">
                    <th
                      scope="col"
                      className="text-left !pl-5"
                      title="Request model and start time in UTC"
                    >
                      Request
                    </th>
                    <th scope="col">HTTP</th>
                    <th scope="col" title="Input tokens, including cache">
                      Input
                    </th>
                    <th scope="col">Output</th>
                    <th scope="col">Cache read</th>
                    <th scope="col">Cache write</th>
                    <th scope="col" title="Cache read tokens / input tokens (including cache)">
                      Cache hit rate
                    </th>
                    <th scope="col" title="Time to first token">
                      TTFT
                    </th>
                    <th
                      scope="col"
                      title="Estimated time per output token: (duration − TTFT) / (output tokens − 1), in ms/token"
                    >
                      TPOT
                    </th>
                    <th scope="col">Duration</th>
                    <th scope="col" className="!pr-5">
                      Cost
                    </th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                  {loading && rows.length === 0 && (
                    <tr>
                      <td
                        colSpan={11}
                        className="px-5 py-4 text-left text-gray-500 dark:text-gray-400"
                      >
                        Loading requests…
                      </td>
                    </tr>
                  )}
                  {rows.map((row) => (
                    <RequestRow key={row.request_id} row={row} />
                  ))}
                  {!loading && rows.length === 0 && !error && (
                    <tr>
                      <td
                        colSpan={11}
                        className="px-5 py-4 text-left text-gray-500 dark:text-gray-400"
                      >
                        No request records are stored for this window yet.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            {page?.next_cursor && (
              <div className="flex justify-end border-t border-gray-100 px-5 py-3 dark:border-white/5">
                <button
                  type="button"
                  className={BUTTON}
                  disabled={moreLoading || loading || loadingSources || restart}
                  onClick={() => void loadMore()}
                >
                  {moreLoading ? 'Loading…' : 'Load more'}
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}

function RequestRow({ row }: { row: AdminLlmRequest }) {
  const amount = money(row.spent_usd);
  const model = modelLabel(row);
  const hitRate = cacheHitRate(row);
  const tpot = timePerOutputToken(row);
  return (
    <tr className="[&>td]:px-3 [&>td]:py-4 [&>td]:whitespace-nowrap">
      <td className="min-w-[10rem] max-w-[16rem] text-left !pl-5" title={row.request_id}>
        <div className="whitespace-normal break-words font-medium" title={model.title}>
          {model.text}
        </div>
        <time
          className="mt-1 block text-xs text-gray-500 dark:text-gray-400"
          dateTime={row.request_started_at}
          title={dateTime(row.request_started_at)}
        >
          {startTime(row.request_started_at)}
        </time>
        {row.usage_estimated && (
          <span
            className="mt-1 block text-xs text-amber-700 dark:text-amber-400"
            title="Contains estimated usage"
          >
            Estimated
          </span>
        )}
      </td>
      <td title={statusLabel(row.status_code)}>{row.status_code ?? UNAVAILABLE}</td>
      <td>{tokenLabel(row.tokens_in)}</td>
      <td
        title={row.reasoning == null ? undefined : `Reasoning tokens: ${tokenLabel(row.reasoning)}`}
      >
        {tokenLabel(row.tokens_out)}
      </td>
      <td>{tokenLabel(row.cache_read)}</td>
      <td>{tokenLabel(row.cache_write)}</td>
      <td>{hitRate === null ? UNAVAILABLE : `${hitRate.toFixed(1)}%`}</td>
      <td>{durationLabel(row.ttft_ms)}</td>
      <td>{tpot === null ? UNAVAILABLE : `${tpot.toFixed(2)} ms/token`}</td>
      <td>{durationLabel(row.latency_ms)}</td>
      <td className="!pr-5" title={amount.exact ?? undefined}>
        {amount.exact === null ? UNAVAILABLE : amount.display}
      </td>
    </tr>
  );
}

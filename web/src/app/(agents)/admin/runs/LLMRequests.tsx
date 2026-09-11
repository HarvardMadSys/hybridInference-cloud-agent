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

const UNAVAILABLE = 'Unavailable';

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

/** A duration in milliseconds, or an explicit "not reported". */
export function durationLabel(value: number | null): string {
  return value == null ? 'Not reported' : `${number(value)} ms`;
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

/**
 * Per-request gateway detail for one execution, read on demand.
 *
 * Collapsed until asked: nothing here runs on a page load, and nothing here
 * writes. Each page reports its own read time because the ledger is written
 * after a response completes, so the count is "records loaded", never a claim
 * that the window has been reconciled.
 */
export function LLMRequests({
  jobId,
  attemptId,
  running,
  onAccessDenied,
}: {
  jobId: string;
  attemptId?: number;
  running: boolean;
  onAccessDenied: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
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
    if (!expanded) return;
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
  }, [expanded, jobId, attemptId, executionKey, revision, retireAll, fail]);

  // One page at a time, for one reference. A refresh keeps the rows that are
  // already on screen until the new ones arrive; switching reference clears
  // them first so a late response cannot fill the new selection.
  useEffect(() => {
    // The selected source is only usable once the list in front of us was read
    // for this execution: on the render where the execution changes, the old
    // id is still in state and must not be queried.
    if (!expanded || !sourceId || sourcesFor !== executionKey) return;
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
  }, [expanded, jobId, executionKey, sourceId, sourcesFor, revision, key, fail]);

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
    if (!expanded || !sourceId || !running || pagesLoaded !== 1 || restart) return;
    if (loading || moreLoading) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      setRevision((value) => value + 1);
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [expanded, sourceId, running, pagesLoaded, restart, loading, moreLoading]);

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
  const selected = (sources ?? []).find((source) => source.source_id === sourceId) ?? null;
  // The rows on screen were read from one fixed window. Fresh source metadata
  // describes the read that has not happened yet, so a failed refresh must not
  // relabel rows that belong to the window before it.
  // The page wins over the source: those are the rows on screen.
  const shownAttemptNo = page?.attempt_no ?? selected?.attempt_no ?? null;
  const shownSince = page?.window.since ?? selected?.window_since ?? null;
  const shownUntil = page?.window.until ?? selected?.window_until ?? null;

  return (
    <section aria-labelledby="llm-requests-heading" className="mt-8">
      <details
        className={`${CARD} overflow-hidden`}
        onToggle={(event) => setExpanded(event.currentTarget.open)}
      >
        <summary
          id="llm-requests-heading"
          className="flex cursor-pointer flex-wrap items-center justify-between gap-3 px-5 py-4 text-lg font-semibold text-gray-900 dark:text-gray-50"
        >
          LLM requests
          <span className="text-xs font-normal text-gray-500 dark:text-gray-400">
            Per-request detail from the gateway ledger
          </span>
        </summary>
        <div className="border-t border-gray-100 px-5 py-4 dark:border-white/5">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-xs text-gray-500 dark:text-gray-400">
              {shownAttemptNo !== null
                ? `Execution ${shownAttemptNo} · Window ${dateTime(shownSince)} – ${dateTime(shownUntil)}`
                : `Execution ${attemptId ?? 'unavailable'}`}
            </p>
            <button
              type="button"
              className={BUTTON}
              disabled={loading || loadingSources || !sourceId}
              onClick={() => setRevision((value) => value + 1)}
            >
              Refresh
            </button>
          </div>
          {stale && (
            <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
              Read at {dateTime(stale)}. Logs may arrive later; a refresh reads the first page
              again.
            </p>
          )}
          {loadingSources && (
            <p role="status" className="mt-3 text-sm text-gray-500 dark:text-gray-400">
              Loading request sources…
            </p>
          )}
          {queryable.length > 1 && (
            <label className="mt-3 block text-sm">
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
            <p className="mt-3 text-sm text-gray-500 dark:text-gray-400">
              {sourceReason === 'not_started'
                ? 'No execution has started in this run.'
                : 'This execution has no queryable gateway source.'}
            </p>
          )}
          {unavailable.length > 0 && (
            <ul className="mt-2 space-y-1 text-xs text-amber-700 dark:text-amber-400">
              {unavailable.map((source) => (
                <li key={`${source.attempt_id}:${source.source_id ?? 'none'}`}>
                  Execution {source.attempt_no}: {sourceExplanation(source)}
                </li>
              ))}
            </ul>
          )}
          {error && (
            <ErrorNotice>
              {error}
              {rows.length > 0 && ' Showing the last successful read.'}
            </ErrorNotice>
          )}
          {restart && (
            <button
              type="button"
              className={`${BUTTON} mt-2`}
              onClick={() => setRevision((value) => value + 1)}
            >
              Start again from the first page
            </button>
          )}
          {sourceId && (
            <>
              <div className="mt-4 overflow-x-auto">
                <table className="w-full text-left text-sm">
                  <thead className="text-xs text-gray-500 dark:text-gray-400">
                    <tr>
                      <th className="px-3 py-2 font-medium">Started</th>
                      <th className="px-3 py-2 font-medium">Model</th>
                      <th className="px-3 py-2 font-medium">HTTP</th>
                      <th className="px-3 py-2 font-medium">Input / output</th>
                      <th className="px-3 py-2 font-medium">Cost</th>
                      <th className="px-3 py-2 font-medium">TTFT</th>
                      <th className="px-3 py-2 font-medium">Duration</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                    {loading && rows.length === 0 && (
                      <tr>
                        <td
                          colSpan={7}
                          className="px-3 py-3 text-sm text-gray-500 dark:text-gray-400"
                        >
                          Loading requests…
                        </td>
                      </tr>
                    )}
                    {rows.map((row) => (
                      <RequestRow key={row.request_id} row={row} />
                    ))}
                    {!loading && rows.length === 0 && (
                      <tr>
                        <td
                          colSpan={7}
                          className="px-3 py-3 text-sm text-gray-500 dark:text-gray-400"
                        >
                          No request records are stored for this window yet.
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
              <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
                <p className="text-xs text-gray-500 dark:text-gray-400">
                  Loaded {rows.length} record{rows.length === 1 ? '' : 's'}.
                  {pagesLoaded > 1 ? ` ${pagesLoaded} pages read.` : ''}
                </p>
                {page?.next_cursor && (
                  <button
                    type="button"
                    className={BUTTON}
                    disabled={moreLoading || loading}
                    onClick={() => void loadMore()}
                  >
                    {moreLoading ? 'Loading…' : 'Load more'}
                  </button>
                )}
              </div>
            </>
          )}
          <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
            Request logs are written after a response completes, so this list can lag the run, and a
            refresh can reveal rows a later page will not. Gateway totals above cover the whole
            window and are not a subtotal of this page.
          </p>
        </div>
      </details>
    </section>
  );
}

function RequestRow({ row }: { row: AdminLlmRequest }) {
  const amount = money(row.spent_usd);
  const model = modelLabel(row);
  return (
    <>
      <tr>
        <td className="px-3 py-2 whitespace-nowrap tabular-nums">
          {dateTime(row.request_started_at)}
        </td>
        <td className="max-w-[16rem] px-3 py-2 break-words" title={model.title}>
          {model.text}
        </td>
        <td className="px-3 py-2 tabular-nums">{statusLabel(row.status_code)}</td>
        <td className="px-3 py-2 tabular-nums">
          {number(row.tokens_in)} / {number(row.tokens_out)}
        </td>
        <td className="px-3 py-2 tabular-nums" title={amount.exact ?? undefined}>
          {amount.display}
        </td>
        <td className="px-3 py-2 tabular-nums">{durationLabel(row.ttft_ms)}</td>
        <td className="px-3 py-2 tabular-nums">{durationLabel(row.latency_ms)}</td>
      </tr>
      <tr>
        <td colSpan={7} className="px-3 pb-3">
          <details>
            <summary className="cursor-pointer text-xs text-gray-500 dark:text-gray-400">
              Request detail
            </summary>
            <dl className="mt-2 grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-gray-600 sm:grid-cols-2 dark:text-gray-400">
              <div className="min-w-0">
                <dt className="text-gray-500 dark:text-gray-500">Request ID</dt>
                <dd className="break-all font-mono">{row.request_id}</dd>
              </div>
              <div className="min-w-0">
                <dt className="text-gray-500 dark:text-gray-500">Logged at</dt>
                <dd>{dateTime(row.logged_at)}</dd>
              </div>
              <div className="min-w-0">
                <dt className="text-gray-500 dark:text-gray-500">Cache read / write</dt>
                <dd className="tabular-nums">
                  {number(row.cache_read)} / {number(row.cache_write)}
                </dd>
              </div>
              <div className="min-w-0">
                <dt className="text-gray-500 dark:text-gray-500">Reasoning tokens</dt>
                <dd className="tabular-nums">{number(row.reasoning)}</dd>
              </div>
              <div className="min-w-0">
                <dt className="text-gray-500 dark:text-gray-500">Exact recorded cost</dt>
                <dd className="font-mono">{row.spent_usd ?? UNAVAILABLE}</dd>
              </div>
              <div className="min-w-0">
                <dt className="text-gray-500 dark:text-gray-500">Usage</dt>
                <dd>
                  {row.usage_estimated ? 'Contains estimated usage' : 'Reported by the gateway'}
                </dd>
              </div>
            </dl>
          </details>
        </td>
      </tr>
    </>
  );
}

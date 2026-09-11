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
  const selection = useRef<AbortController | null>(null);
  const more = useRef<AbortController | null>(null);
  const generation = useRef(0);

  const key = `${jobId}:${attemptId ?? ''}:${sourceId ?? ''}`;
  const loadedKey = useRef('');

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
    const current = ++generation.current;
    selection.current?.abort();
    more.current?.abort();
    setLoadingSources(true);
    setSources(null);
    setSourceReason(null);
    setError(null);
    setRestart(false);
    getAdminLlmRequestSources(jobId, { attempt_id: attemptId, limit: 100 }, controller.signal)
      .then((next) => {
        if (controller.signal.aborted || generation.current !== current) return;
        setSources(next.sources);
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
        if (!controller.signal.aborted && generation.current === current) fail(caught);
      })
      .finally(() => {
        if (!controller.signal.aborted && generation.current === current) setLoadingSources(false);
      });
    return () => controller.abort();
  }, [expanded, jobId, attemptId, revision, fail]);

  // One page at a time, for one reference. A refresh keeps the rows that are
  // already on screen until the new ones arrive; switching reference clears
  // them first so a late response cannot fill the new selection.
  useEffect(() => {
    if (!expanded || !sourceId) return;
    const controller = new AbortController();
    const current = generation.current;
    selection.current?.abort();
    selection.current = controller;
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
        if (controller.signal.aborted || generation.current !== current) return;
        setPage(next);
        setRows(next.requests);
        setPagesLoaded(1);
        setStale(next.fetched_at);
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted && generation.current === current) fail(caught);
      })
      .finally(() => {
        if (!controller.signal.aborted && generation.current === current) setLoading(false);
      });
    return () => controller.abort();
  }, [expanded, jobId, sourceId, revision, key, fail]);

  // Only a visible first page of a running execution keeps re-reading itself.
  // After "load more" the page set is a deliberate read and stops moving.
  useEffect(() => {
    if (!expanded || !sourceId || !running || pagesLoaded !== 1 || restart) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      setRevision((value) => value + 1);
    }, AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [expanded, sourceId, running, pagesLoaded, restart]);

  useEffect(
    () => () => {
      selection.current?.abort();
      more.current?.abort();
    },
    [],
  );

  async function loadMore() {
    if (!page?.next_cursor || !sourceId || moreLoading) return;
    const controller = new AbortController();
    const current = generation.current;
    more.current = controller;
    setMoreLoading(true);
    setError(null);
    try {
      const next = await getAdminLlmRequests(
        jobId,
        { source_id: sourceId, limit: LLM_REQUEST_PAGE_SIZE, cursor: page.next_cursor },
        controller.signal,
      );
      if (controller.signal.aborted || generation.current !== current) return;
      setPage(next);
      setRows((previous) => [...previous, ...next.requests]);
      setPagesLoaded((count) => count + 1);
      setStale(next.fetched_at);
    } catch (caught) {
      // The loaded pages and their cursor stay: the same continuation is worth
      // retrying, and a failed page is not the end of the result.
      if (!controller.signal.aborted && generation.current === current) fail(caught);
    } finally {
      if (!controller.signal.aborted) setMoreLoading(false);
    }
  }

  const queryable = (sources ?? []).filter((source) => source.availability === 'queryable');
  const unavailable = (sources ?? []).filter((source) => source.availability !== 'queryable');
  const selected = (sources ?? []).find((source) => source.source_id === sourceId) ?? null;

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
              {selected
                ? `Execution ${selected.attempt_no} · Window ${dateTime(selected.window_since)} – ${dateTime(selected.window_until)}`
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
                    disabled={moreLoading}
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

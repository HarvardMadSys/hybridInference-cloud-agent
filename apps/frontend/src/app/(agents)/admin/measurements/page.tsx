'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useEffect, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import {
  getMeasurement,
  getMeasurementWriterStatus,
  listMeasurements,
  listProfilingRuns,
  summarizeProfilingRun,
  type MeasurementDetail,
  type MeasurementFilters,
  type MeasurementPage,
  type MeasurementRecord,
  type MeasurementSummary,
  type MeasurementSummaryResponse,
  type MeasurementWriterStatus,
  type ProfilingRun,
  type ProfilingRunCatalogue,
} from '@/lib/api/adminMeasurements';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import {
  accessDenied,
  AdminRequired,
  BUTTON,
  CARD,
  dateTime,
  ErrorNotice,
  INPUT,
  number,
} from '../runs/shared';
import { MeasurementControlPanel } from './MeasurementControlPanel';
import {
  ATTACHED_STAGE_NOTE,
  GAP_NOTE,
  LISTED_OPERATIONS,
  MEASUREMENTS_PATH,
  MIN_PERCENTILE_SAMPLES,
  SERVER_SIDE_NOTE,
  TRUNCATED_STAGE_NOTE,
  coverageLine,
  duration,
  filterChips,
  measuredBetween,
  namesRecords,
  operationLabel,
  operationName,
  parseAttemptId,
  partialReasons,
  runLabel,
  sampleResult,
  selectionFromQuery,
  selectionQuery,
  timelineRows,
  timingRows,
  type FormattedDuration,
  type MeasurementSelection,
  type TimelineRow,
} from './present';

//: The catalogue is a bounded page, not the whole history.
const RUN_LIMIT = 50;

/** The operation menu's value when a link selected several operations at once. */
const SEVERAL = '\u0000several';

export default function AdminMeasurementsPage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AdminRequired />;
  return (
    <Suspense fallback={<MeasurementsSkeleton />}>
      <MeasurementsView key={state.user?.id ?? 'admin'} />
    </Suspense>
  );
}

/**
 * One admin read, with the states a read can end in.
 *
 * The read function and the nonce are the effect's dependencies. The nonce is
 * what Refresh changes: it is handed to the read rather than hidden in a key,
 * so the dependency is the read's identity and not a value the request uses.
 * A failed refresh keeps the previous answer on screen and says so. A new
 * question -- another filter, another sample -- drops it instead, so one
 * selection's records never sit under another's; and a refused session drops
 * it too, because revoked access must not keep showing records.
 */
function useAdminRead<T>(
  read: (signal: AbortSignal, nonce: number) => Promise<T>,
  nonce: number,
  onDenied: () => void,
  enabled = true,
): { data: T | null; error: string | null; loading: boolean; stale: boolean } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(enabled);
  const asked = useRef(read);

  useEffect(() => {
    if (asked.current !== read) {
      asked.current = read;
      setData(null);
    }
    if (!enabled) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    read(controller.signal, nonce)
      .then((next) => {
        if (!controller.signal.aborted) setData(next);
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted) return;
        if (accessDenied(e)) {
          setData(null);
          onDenied();
          return;
        }
        setError(getErrorMessage(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [read, nonce, onDenied, enabled]);

  return { data, error, loading, stale: Boolean(error) && data !== null };
}

/**
 * The page, with its selection kept in the address bar.
 *
 * The selection is this view's state, written back with native history so a
 * link can be shared; expanding a sample or changing a filter re-reads only
 * what depends on it instead of remounting every panel. A URL this view did
 * not write -- a link, the Admin tab, Back -- is read back into the state.
 */
function MeasurementsView() {
  const query = useSearchParams().toString();
  const [initial] = useState(() => selectionFromQuery(query));
  const [selection, setSelection] = useState<MeasurementSelection>(initial.selection);
  // A link this page cannot serve is a state, not a constant: it is shown as
  // it is until the admin chooses to read something else.
  const [linkError, setLinkError] = useState<string | null>(initial.error);
  const [denied, setDenied] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const onDenied = useCallback(() => setDenied(true), []);
  /** The query this view last read or wrote, so its own writes are not read back as links. */
  const seen = useRef(query);

  useEffect(() => {
    if (query === seen.current) return;
    seen.current = query;
    const next = selectionFromQuery(query);
    setSelection(next.selection);
    setLinkError(next.error);
  }, [query]);

  useEffect(() => {
    // A link this page cannot serve is left exactly as it is.
    if (linkError) return;
    const next = selectionQuery(selection);
    if (next === seen.current) return;
    seen.current = next;
    const path = window.location.pathname;
    window.history.replaceState(null, '', next ? `${path}?${next}` : path);
  }, [selection, linkError]);

  const update = useCallback((patch: Partial<MeasurementSelection>) => {
    setLinkError(null);
    setSelection((current) => ({ ...current, ...patch }));
  }, []);

  const readRuns = useCallback(
    (signal: AbortSignal, _nonce: number) => listProfilingRuns({ limit: RUN_LIMIT }, signal),
    [],
  );
  const runs = useAdminRead<ProfilingRunCatalogue>(readRuns, refreshKey, onDenied, !linkError);

  if (denied) return <AdminRequired />;

  // The page is about one run: the one the link names, else the newest. A
  // link that names records -- a turn, an attempt, an interaction -- is read
  // across every run instead, because it asked for those records wherever
  // they are.
  const records = namesRecords(selection);
  const runId = selection.run ?? (records ? null : (runs.data?.runs[0]?.run_id ?? null));
  const runKnown = selection.run !== null || records || runs.data !== null || runs.error !== null;

  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section="Measurements"
          description="Explore operation timings and investigate slow sandbox interactions."
        />
        <AdminNavigation current="measurements" />

        <div className="mt-8 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-50">Measurements</h2>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              How long sandbox startup, terminal opening and file access take,{' '}
              <span
                className="cursor-help underline decoration-dotted underline-offset-2"
                title={SERVER_SIDE_NOTE}
              >
                timed on the server
              </span>
              .
            </p>
          </div>
          <button
            className={BUTTON}
            type="button"
            onClick={() => setRefreshKey((value) => value + 1)}
          >
            Refresh
          </button>
        </div>

        {linkError ? (
          <ErrorNotice>
            {linkError} Nothing was read.{' '}
            <Link className="underline" href={MEASUREMENTS_PATH}>
              Show all measurements
            </Link>
          </ErrorNotice>
        ) : (
          <>
            <MeasurementControlPanel onDenied={onDenied} refreshKey={refreshKey} />
            <WriterLoss onDenied={onDenied} refreshKey={refreshKey} />
            {runs.error && (
              <ErrorNotice>
                {runs.error}{' '}
                {runs.stale
                  ? 'Couldn’t refresh the list of runs; showing the previous one.'
                  : 'The list of runs could not be read, so samples are shown from every run.'}
              </ErrorNotice>
            )}
            {runId && (
              <TimingsPanel
                key={runId}
                runId={runId}
                runs={runs.data?.runs ?? []}
                onPick={(run) => update({ run, measurement: null })}
                onDenied={onDenied}
                refreshKey={refreshKey}
              />
            )}
            <SamplesPanel
              selection={selection}
              runId={runId}
              ready={runKnown}
              update={update}
              onDenied={onDenied}
              refreshKey={refreshKey}
            />
          </>
        )}
      </section>
    </div>
  );
}

/**
 * Samples this server process lost before storing them, when it lost any.
 *
 * Nothing is shown otherwise: a healthy writer is not something the reader
 * needs to read about. The counters are this replica's, not the fleet's.
 */
function WriterLoss({ onDenied, refreshKey }: { onDenied: () => void; refreshKey: number }) {
  const read = useCallback(
    (signal: AbortSignal, _nonce: number) => getMeasurementWriterStatus(signal),
    [],
  );
  const { data } = useAdminRead<MeasurementWriterStatus>(read, refreshKey, onDenied);
  if (!data) return null;
  const counts = data.counts;
  const parts: [string, number][] = [
    ['rejected', counts.rejected],
    ['conflicting', counts.conflict],
    ['dropped with the queue full', counts.dropped_full],
    ['timed out', counts.dropped_timeout],
    ['dropped at shutdown', counts.dropped_shutdown],
    ['failed to write', counts.failed],
  ];
  const lost = parts.reduce((total, [, count]) => total + count, 0);
  if (lost === 0) return null;
  const breakdown = parts
    .filter(([, count]) => count > 0)
    .map(([label, count]) => `${number(count)} ${label}`)
    .join(', ');
  return (
    <p
      className="mt-3 text-xs text-amber-700 dark:text-amber-400"
      title={`${breakdown}. Counted by the server process that answered this page, not across every replica.`}
    >
      {number(lost)} sample{lost === 1 ? ' was' : 's were'} lost on this server before they could be
      stored.
    </p>
  );
}

// ---------------------------------------------------------------------------
// Timings over one run
// ---------------------------------------------------------------------------

function TimingsPanel({
  runId,
  runs,
  onPick,
  onDenied,
  refreshKey,
}: {
  runId: string;
  runs: ProfilingRun[];
  onPick: (runId: string) => void;
  onDenied: () => void;
  refreshKey: number;
}) {
  const read = useCallback(
    (signal: AbortSignal, _nonce: number) => summarizeProfilingRun(runId, [], signal),
    [runId],
  );
  const { data, error, loading, stale } = useAdminRead<MeasurementSummaryResponse>(
    read,
    refreshKey,
    onDenied,
  );
  const rows = data ? timingRows(data) : [];
  const run = runs.find((entry) => entry.run_id === runId) ?? null;

  return (
    <section aria-labelledby="timings-heading" className="mt-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3
          id="timings-heading"
          className="text-base font-semibold text-gray-900 dark:text-gray-50"
        >
          Timings
        </h3>
        {runs.length > 1 || (runs.length === 1 && !run) ? (
          <label className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            Run
            <select
              className={`${INPUT} w-64 py-1.5`}
              value={runId}
              onChange={(event) => onPick(event.target.value)}
            >
              {!run && <option value={runId}>{runId}</option>}
              {runs.map((entry) => (
                <option key={entry.run_id} value={entry.run_id}>
                  {runLabel(entry)}
                </option>
              ))}
            </select>
          </label>
        ) : (
          <span className="text-xs text-gray-500 dark:text-gray-400" title={runId}>
            Run {run ? runLabel(run) : runId}
          </span>
        )}
      </div>

      {error && (
        <ErrorNotice>
          {error}
          {stale && ' Couldn’t refresh; showing the previous read.'}
        </ErrorNotice>
      )}

      {loading && !data ? (
        <div className={`${CARD} mt-3`}>
          <RowsSkeleton rows={2} />
        </div>
      ) : (
        data && (
          <>
            <div className={`${CARD} mt-3 overflow-x-auto`}>
              {rows.length === 0 ? (
                <p className="px-5 py-5 text-sm text-gray-500 dark:text-gray-400">
                  No samples stored for this run yet.
                </p>
              ) : (
                <table className="w-full min-w-[720px] text-left text-sm">
                  <thead className="border-b border-gray-100 bg-gray-50/70 text-xs uppercase text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
                    <tr>
                      <th className="px-5 py-3 font-medium">Operation</th>
                      <th className="px-5 py-3 text-right font-medium">Samples</th>
                      <th className="px-5 py-3 text-right font-medium">Min</th>
                      <th
                        className="px-5 py-3 text-right font-medium"
                        title="Half the samples were faster"
                      >
                        Median
                      </th>
                      <th
                        className="px-5 py-3 text-right font-medium"
                        title="95% of the samples were faster"
                      >
                        p95
                      </th>
                      <th className="px-5 py-3 text-right font-medium">Max</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                    {rows.map((row) => (
                      <tr key={row.key}>
                        <th scope="row" className="px-5 py-3 font-normal" title={row.title}>
                          <span className="font-medium text-gray-900 dark:text-gray-100">
                            {row.label}
                          </span>
                          {row.detail && (
                            <span className="ml-2 text-xs text-gray-500 dark:text-gray-400">
                              {row.detail}
                            </span>
                          )}
                        </th>
                        <td className="whitespace-nowrap px-5 py-3 text-right tabular-nums text-gray-700 dark:text-gray-300">
                          {number(row.samples)}
                          {row.excluded && (
                            <span
                              className="ml-1.5 cursor-help text-xs text-amber-700 dark:text-amber-400"
                              title={row.excluded.title}
                            >
                              ({row.excluded.label})
                            </span>
                          )}
                        </td>
                        <TimingCell value={row.min} />
                        <TimingCell value={row.p50} />
                        <TimingCell value={row.p95} />
                        <TimingCell value={row.max} />
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            {rows.some((row) => row.few) && (
              <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                A row with fewer than {number(MIN_PERCENTILE_SAMPLES)} counted samples gives only a
                rough figure.
              </p>
            )}
          </>
        )
      )}
    </section>
  );
}

function TimingCell({ value }: { value: FormattedDuration }) {
  return (
    <td
      className="whitespace-nowrap px-5 py-3 text-right tabular-nums text-gray-900 dark:text-gray-100"
      title={
        value.exact ??
        'No sample completed with collection closed, so there is nothing to compute this from.'
      }
    >
      {value.display}
    </td>
  );
}

// ---------------------------------------------------------------------------
// Samples
// ---------------------------------------------------------------------------

function SamplesPanel({
  selection,
  runId,
  ready,
  update,
  onDenied,
  refreshKey,
}: {
  selection: MeasurementSelection;
  runId: string | null;
  ready: boolean;
  update: (patch: Partial<MeasurementSelection>) => void;
  onDenied: () => void;
  refreshKey: number;
}) {
  const filters: MeasurementFilters = {
    run_id: runId ?? undefined,
    operation: selection.operation,
    request_kind: selection.request_kind,
    coverage_scope: selection.coverage_scope,
    outcome: selection.outcome ?? undefined,
    collection_complete: selection.collection_complete ?? undefined,
    job_id: selection.job_id ?? undefined,
    thread_id: selection.thread_id ?? undefined,
    // The URL reader refuses an attempt id it cannot parse before this panel
    // mounts, so this never widens a read by dropping one.
    attempt_id: parseAttemptId(selection.attempt_id ?? '').attemptId ?? undefined,
    interaction_id: selection.interaction_id ?? undefined,
  };
  // Keyed by value, so expanding a sample -- which changes the selection but
  // not the filters -- does not re-read the list.
  const filterKey = JSON.stringify(filters);
  const read = useCallback(
    (signal: AbortSignal, _nonce: number) =>
      listMeasurements(JSON.parse(filterKey) as MeasurementFilters, signal),
    [filterKey],
  );
  const { data, error, loading, stale } = useAdminRead<MeasurementPage>(
    read,
    refreshKey,
    onDenied,
    ready,
  );

  const [more, setMore] = useState<{
    rows: MeasurementSummary[];
    next: string | null;
  } | null>(null);
  const [moreLoading, setMoreLoading] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);
  const moreRequest = useRef<AbortController | null>(null);
  useEffect(() => {
    // A new first page starts the list again.
    moreRequest.current?.abort();
    setMore(null);
    setMoreLoading(false);
    setMoreError(null);
  }, [data]);
  useEffect(() => () => moreRequest.current?.abort(), []);

  const rows = data ? [...data.measurements, ...(more?.rows ?? [])] : [];
  const next = more ? more.next : (data?.next_cursor ?? null);

  async function loadMore() {
    if (!next || moreLoading) return;
    moreRequest.current?.abort();
    const controller = new AbortController();
    moreRequest.current = controller;
    setMoreLoading(true);
    setMoreError(null);
    try {
      const page = await listMeasurements(
        { ...(JSON.parse(filterKey) as MeasurementFilters), after: next },
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setMore((current) => ({
        rows: [...(current?.rows ?? []), ...page.measurements],
        next: page.next_cursor,
      }));
    } catch (e) {
      if (controller.signal.aborted) return;
      if (accessDenied(e)) onDenied();
      else setMoreError(getErrorMessage(e));
    } finally {
      if (!controller.signal.aborted) setMoreLoading(false);
    }
  }

  const chips = filterChips(selection);
  const operationValue =
    selection.operation.length === 0
      ? ''
      : selection.operation.length === 1
        ? selection.operation[0]
        : SEVERAL;
  const unlistedOperation =
    operationValue !== '' &&
    operationValue !== SEVERAL &&
    !LISTED_OPERATIONS.includes(operationValue);
  const open = selection.measurement;
  const openElsewhere =
    open !== null && data !== null && !rows.some((row) => row.measurement_id === open);
  const narrowed = chips.length > 0 || selection.operation.length > 0;

  return (
    <section aria-labelledby="samples-heading" className="mt-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h3
          id="samples-heading"
          className="text-base font-semibold text-gray-900 dark:text-gray-50"
        >
          Samples
        </h3>
        <label className="w-52 shrink-0">
          <span className="sr-only">Operation</span>
          <select
            className={`${INPUT} py-1.5`}
            value={operationValue}
            onChange={(event) => {
              const value = event.target.value;
              if (value === SEVERAL) return;
              update({ operation: value ? [value] : [], measurement: null });
            }}
          >
            <option value="">All operations</option>
            {LISTED_OPERATIONS.map((operation) => (
              <option key={operation} value={operation}>
                {operationName(operation)}
              </option>
            ))}
            {operationValue === SEVERAL && (
              <option value={SEVERAL}>{selection.operation.map(operationName).join(', ')}</option>
            )}
            {unlistedOperation && <option value={operationValue}>{operationValue}</option>}
          </select>
        </label>
      </div>

      {chips.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          <span className="text-gray-500 dark:text-gray-400">
            {runId ? 'Only' : 'From every run, only'}
          </span>
          {chips.map((chip) => (
            <span
              key={chip.label}
              className="inline-flex max-w-full items-center gap-1 rounded-full bg-gray-100 py-0.5 pl-2.5 pr-1 text-gray-700 dark:bg-white/5 dark:text-gray-300"
            >
              <span className="truncate">{chip.label}</span>
              <button
                type="button"
                className="rounded-full px-1 text-gray-400 hover:bg-gray-200 hover:text-gray-700 dark:text-gray-500 dark:hover:bg-white/10 dark:hover:text-gray-200"
                aria-label={`Remove filter: ${chip.label}`}
                onClick={() => update({ ...chip.clear, measurement: null })}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      {error && (
        <ErrorNotice>
          {error}
          {stale && ' Couldn’t refresh; showing the previous read.'}
        </ErrorNotice>
      )}

      {openElsewhere && open && (
        <div className={`${CARD} mt-3 px-5 py-4`}>
          <div className="mb-3 flex items-center justify-between gap-3">
            <p className="truncate text-sm font-medium text-gray-900 dark:text-gray-100">
              Sample {open}
            </p>
            <button
              type="button"
              className="text-xs text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100"
              onClick={() => update({ measurement: null })}
            >
              Close
            </button>
          </div>
          <SampleBreakdown
            measurementId={open}
            runId={runId}
            update={update}
            onDenied={onDenied}
            refreshKey={refreshKey}
          />
        </div>
      )}

      {(loading || !ready) && !data ? (
        <div className={`${CARD} mt-3`}>
          <RowsSkeleton rows={4} />
        </div>
      ) : (
        data && (
          <>
            <div className={`${CARD} mt-3 overflow-x-auto`}>
              {rows.length === 0 ? (
                <p
                  className="px-5 py-5 text-sm text-gray-500 dark:text-gray-400"
                  title={data.empty_reason}
                >
                  {narrowed
                    ? 'No samples match these filters.'
                    : 'No samples yet. Turn measurement on, then use a sandbox, open a terminal or browse files.'}
                </p>
              ) : (
                <table className="w-full min-w-[720px] text-left text-sm">
                  <thead className="border-b border-gray-100 bg-gray-50/70 text-xs uppercase text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
                    <tr>
                      <th className="px-5 py-3 font-medium">Operation</th>
                      <th className="px-5 py-3 text-right font-medium">Duration</th>
                      <th className="px-5 py-3 font-medium">Result</th>
                      <th className="px-5 py-3 font-medium">Time</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                    {rows.map((row) => (
                      <SampleRow
                        key={row.measurement_id}
                        row={row}
                        open={open === row.measurement_id}
                        runId={runId}
                        update={update}
                        onDenied={onDenied}
                        refreshKey={refreshKey}
                      />
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            {moreError && <ErrorNotice>{moreError}</ErrorNotice>}
            {next && (
              <div className="mt-5 flex justify-center">
                <button
                  type="button"
                  className={BUTTON}
                  disabled={moreLoading}
                  onClick={() => void loadMore()}
                >
                  {moreLoading ? 'Loading…' : 'Load more samples'}
                </button>
              </div>
            )}
          </>
        )
      )}
    </section>
  );
}

const RESULT_TONE = {
  ok: 'text-gray-500 dark:text-gray-400',
  warn: 'text-amber-700 dark:text-amber-400',
  bad: 'text-red-600 dark:text-red-400',
} as const;

function SampleRow({
  row,
  open,
  runId,
  update,
  onDenied,
  refreshKey,
}: {
  row: MeasurementSummary;
  open: boolean;
  runId: string | null;
  update: (patch: Partial<MeasurementSelection>) => void;
  onDenied: () => void;
  refreshKey: number;
}) {
  const measured = duration(row.duration_us);
  const result = sampleResult(row);
  const toggle = () => update({ measurement: open ? null : row.measurement_id });
  return (
    <>
      <tr
        className={`cursor-pointer transition-colors hover:bg-gray-50/70 dark:hover:bg-white/[0.03] ${
          open ? 'bg-gray-50/70 dark:bg-white/[0.03]' : ''
        }`}
        onClick={toggle}
      >
        <th scope="row" className="px-5 py-3 font-normal">
          {/* The button is the keyboard's way in; its click reaches the row. */}
          <button
            type="button"
            aria-expanded={open}
            className="flex items-center gap-2 text-left font-medium text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson dark:text-gray-100"
          >
            <span
              aria-hidden
              className={`text-[10px] text-gray-400 transition-transform dark:text-gray-500 ${open ? 'rotate-90' : ''}`}
            >
              ▶
            </span>
            {operationLabel(row)}
          </button>
        </th>
        <td
          className="whitespace-nowrap px-5 py-3 text-right tabular-nums text-gray-900 dark:text-gray-100"
          title={measured.exact ?? undefined}
        >
          {measured.display}
        </td>
        <td className={`whitespace-nowrap px-5 py-3 text-xs ${RESULT_TONE[result.tone]}`}>
          <span title={result.title}>{result.label}</span>
        </td>
        <td
          className="whitespace-nowrap px-5 py-3 text-xs text-gray-500 dark:text-gray-400"
          title={`Received ${dateTime(row.received_at)}`}
        >
          {dateTime(row.observed_at)}
        </td>
      </tr>
      {open && (
        <tr>
          <td colSpan={4} className="bg-gray-50/70 px-5 pb-5 pt-2 dark:bg-white/[0.03]">
            {/* Zero intrinsic width: the breakdown fills the row without
                reflowing the columns above it. */}
            <div className="w-0 min-w-full">
              <SampleBreakdown
                measurementId={row.measurement_id}
                runId={runId}
                update={update}
                onDenied={onDenied}
                refreshKey={refreshKey}
              />
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// One sample: where its time went
// ---------------------------------------------------------------------------

function SampleBreakdown({
  measurementId,
  runId,
  update,
  onDenied,
  refreshKey,
}: {
  measurementId: string;
  runId: string | null;
  update: (patch: Partial<MeasurementSelection>) => void;
  onDenied: () => void;
  refreshKey: number;
}) {
  const read = useCallback(
    (signal: AbortSignal, _nonce: number) => getMeasurement(measurementId, signal),
    [measurementId],
  );
  const { data, error, loading, stale } = useAdminRead<MeasurementDetail>(
    read,
    refreshKey,
    onDenied,
  );
  return (
    <div>
      {error && (
        <ErrorNotice>
          {error}
          {stale && ' Couldn’t refresh; showing the previous read.'}
        </ErrorNotice>
      )}
      {loading && !data ? (
        <RowsSkeleton rows={3} />
      ) : (
        data && <BreakdownBody detail={data} runId={runId} update={update} />
      )}
    </div>
  );
}

const LINK = 'text-crimson hover:underline dark:text-crimson-light';

function BreakdownBody({
  detail,
  runId,
  update,
}: {
  detail: MeasurementDetail;
  runId: string | null;
  update: (patch: Partial<MeasurementSelection>) => void;
}) {
  const record = detail.record;
  const reasons = partialReasons(record);
  const between = measuredBetween(record);
  const runtime = detail.runtime_id
    ? `${detail.runtime_id}${detail.runtime_version ? ` ${detail.runtime_version}` : ''}`
    : null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-gray-500 dark:text-gray-400">
        {detail.thread_id && (
          <Link
            className={LINK}
            href={`/admin/sessions/${encodeURIComponent(detail.thread_id)}`}
            title={detail.thread_id}
          >
            Session
          </Link>
        )}
        {detail.job_id && (
          <Link
            className={LINK}
            href={`/admin/runs/${encodeURIComponent(detail.job_id)}`}
            title={detail.job_id}
          >
            Turn
          </Link>
        )}
        {detail.host_id && <span>{detail.host_id}</span>}
        {runtime && <span>{runtime}</span>}
        {detail.execution_profile && <span>{detail.execution_profile}</span>}
        {detail.operation === 'terminal.open' && (
          <button
            type="button"
            className={LINK}
            onClick={() =>
              update({
                ...(runId ? { run: runId } : {}),
                interaction_id: detail.interaction_id,
                measurement: null,
              })
            }
          >
            Every request of this terminal opening
          </button>
        )}
      </div>

      {reasons.length > 0 && (
        <div
          role="alert"
          className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:bg-amber-500/10 dark:text-amber-200"
        >
          <ul className="list-disc space-y-0.5 pl-4">
            {reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <p className="text-xs text-gray-500 dark:text-gray-400">
          <span className="font-medium text-gray-700 dark:text-gray-300">Where the time went</span>
          {between && (
            <>
              {' · '}
              <span className="cursor-help" title={coverageLine(record.coverage_scope)}>
                {between}
              </span>
            </>
          )}
        </p>
        <Timeline record={record} />
      </div>

      <details className="text-xs">
        <summary className="cursor-pointer select-none text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100">
          Raw record
        </summary>
        <pre className="mt-2 max-h-96 overflow-auto rounded-lg bg-white p-3 font-mono text-[11px] leading-relaxed text-gray-700 ring-1 ring-inset ring-gray-200 dark:bg-gray-950 dark:text-gray-300 dark:ring-white/10">
          {JSON.stringify(detail, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function Timeline({ record }: { record: MeasurementRecord }) {
  const rows = timelineRows(record);
  const total = duration(record.duration_us);
  if (rows.length === 0) {
    return (
      <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
        Total {total.display}. This record has no stages, so there is no breakdown.
      </p>
    );
  }
  return (
    <table className="mt-2 w-full table-fixed text-left text-xs" aria-label="Where the time went">
      <colgroup>
        <col className="w-60" />
        <col />
        <col className="w-24" />
        <col className="w-16" />
      </colgroup>
      <thead className="sr-only">
        <tr>
          <th>Stage</th>
          <th>Position in the total</th>
          <th>Duration</th>
          <th>Share of the total</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <TimelineLine key={row.key} row={row} total={record.duration_us} />
        ))}
        <tr className="border-t border-gray-200 dark:border-white/10">
          <th scope="row" className="pt-1.5 font-medium text-gray-700 dark:text-gray-300">
            Total
          </th>
          <td />
          <td
            className="pt-1.5 text-right font-medium tabular-nums text-gray-900 dark:text-gray-100"
            title={total.exact ?? undefined}
          >
            {total.display}
          </td>
          <td />
        </tr>
      </tbody>
    </table>
  );
}

/** A hatch in the text colour, so an interval nothing covers reads as empty rather than as work. */
const HATCH = 'repeating-linear-gradient(135deg, currentColor 0 2px, transparent 2px 5px)';

function nameClass(row: TimelineRow): string {
  if (row.kind === 'gap') return 'italic text-gray-500 dark:text-gray-400';
  if (row.kind === 'attached') return '';
  return 'text-gray-800 dark:text-gray-200';
}

function barClass(row: TimelineRow): string {
  if (row.kind === 'gap') return 'text-gray-400 dark:text-gray-500';
  if (row.problem) return 'bg-red-500 dark:bg-red-400';
  return row.depth ? 'bg-sky-300 dark:bg-sky-600' : 'bg-sky-500 dark:bg-sky-400';
}

function nameTitle(row: TimelineRow): string | undefined {
  if (row.kind === 'gap') return GAP_NOTE;
  if (row.kind !== 'attached') return row.rawName ?? undefined;
  const source = row.source ? ` · measured by ${row.source}` : '';
  return `${row.rawName ?? row.name}${source}. ${ATTACHED_STAGE_NOTE}`;
}

function TimelineLine({ row, total }: { row: TimelineRow; total: number }) {
  const measured = duration(row.durationUs);
  const attached = row.kind === 'attached';
  const placed =
    !attached &&
    row.startUs !== null &&
    row.durationUs !== null &&
    Number.isFinite(total) &&
    total > 0;
  const left = placed ? Math.min(100, Math.max(0, ((row.startUs ?? 0) / total) * 100)) : 0;
  const width = placed
    ? Math.min(100 - left, Math.max(0.4, ((row.durationUs ?? 0) / total) * 100))
    : 0;

  return (
    <tr className={attached ? 'text-gray-500 dark:text-gray-400' : ''}>
      <th
        scope="row"
        className={`truncate py-1 pr-3 font-normal ${nameClass(row)}`}
        style={row.depth ? { paddingLeft: '1.25rem' } : undefined}
        title={nameTitle(row)}
      >
        {row.name}
        {attached && row.source && (
          <span className="text-gray-400 dark:text-gray-500"> · {row.source}</span>
        )}
        {row.problem && (
          <span className="ml-1.5 text-red-600 dark:text-red-400">{row.problem}</span>
        )}
      </th>
      <td className="py-1" aria-hidden>
        {placed && (
          <div className="relative h-2 rounded-sm bg-gray-100 dark:bg-white/5">
            <div
              className={`absolute inset-y-0 rounded-sm ${barClass(row)}`}
              style={{
                left: `${left}%`,
                width: `${width}%`,
                backgroundImage: row.kind === 'gap' ? HATCH : undefined,
              }}
            />
          </div>
        )}
      </td>
      <td
        className="py-1 pl-3 text-right tabular-nums"
        title={
          row.truncated
            ? `${measured.exact ?? ''} · ${TRUNCATED_STAGE_NOTE}`
            : (measured.exact ?? undefined)
        }
      >
        {row.truncated ? '≥ ' : ''}
        {measured.display}
      </td>
      <td className="py-1 pl-2 text-right tabular-nums text-gray-400 dark:text-gray-500">
        {row.share ?? ''}
      </td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// Shells
// ---------------------------------------------------------------------------

function RowsSkeleton({ rows }: { rows: number }) {
  return (
    <div className="space-y-2 px-5 py-4" aria-hidden>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="h-7 animate-pulse rounded-lg bg-gray-100 dark:bg-white/5" />
      ))}
    </div>
  );
}

function MeasurementsSkeleton() {
  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <div className={`${CARD} mt-6 h-14 animate-pulse`} />
        <div className={`${CARD} mt-8 h-32 animate-pulse`} />
        <div className={`${CARD} mt-8 h-64 animate-pulse`} />
      </section>
    </div>
  );
}

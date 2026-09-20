'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import {
  getMeasurement,
  getMeasurementWriterStatus,
  listMeasurements,
  listProfilingRuns,
  summarizeProfilingRun,
  type MeasurementDetail,
  type MeasurementPage,
  type MeasurementSummary,
  type MeasurementSummaryResponse,
  type MeasurementWriterStatus,
  type ProfilingAdmissionInstance,
  type ProfilingRun,
  type ProfilingRunCatalogue,
} from '@/lib/api/adminMeasurements';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../AdminNavigation';
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
import {
  ATTACHED_STAGE_NOTE,
  DISTRIBUTION_BASIS,
  EMPTY_SELECTION,
  MEASUREMENTS_PATH,
  SERVER_ONLY_NOTICE,
  TRUNCATED_STAGE_NOTE,
  completenessLines,
  coverageLine,
  coverageScope,
  detailHeading,
  duration,
  factLines,
  factValue,
  groupRows,
  isCompleteOutcome,
  listNotice,
  outcomeLabel,
  parseAttemptId,
  partialReasons,
  rowCompleteness,
  rowIsPartial,
  sampleCaution,
  selectionFromForm,
  selectionFromQuery,
  selectionHref,
  selectionSummary,
  stageRows,
  summaryTotals,
  targetLines,
  type FormKey,
  type MeasurementSelection,
} from './present';

//: The catalogue is a bounded page, not the whole history; the page says so.
const RUN_LIMIT = 50;

export default function AdminMeasurementsPage() {
  const { state } = useAuth();
  // Subscribed here, one level above the panels, so a URL change re-renders
  // this page rather than only the component that happens to read it.
  const query = useSearchParams().toString();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AdminRequired />;
  return (
    <Suspense fallback={<MeasurementsSkeleton />}>
      <MeasurementsView key={`${state.user?.id ?? 'admin'}:${query}`} initialQuery={query} />
    </Suspense>
  );
}

/**
 * One admin read, with the states a read can end in.
 *
 * The read function and the nonce are the effect's dependencies. The nonce is
 * what Refresh changes: it is handed to the read rather than hidden in a key,
 * so the dependency is the read's identity and not a value the request uses.
 * A panel is remounted by its subject instead, which is why a value from one
 * subject can never sit beside another's.
 */
function useAdminRead<T>(
  read: (signal: AbortSignal, nonce: number) => Promise<T>,
  nonce: number,
  onDenied: () => void,
): { data: T | null; error: string | null; loading: boolean; stale: boolean } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
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
          // A revoked session must not keep showing records it already has.
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
  }, [read, nonce, onDenied]);

  return { data, error, loading, stale: Boolean(error) && data !== null };
}

function MeasurementsView({ initialQuery }: { initialQuery: string }) {
  const linked = selectionFromQuery(initialQuery);
  const [selection, setSelection] = useState<MeasurementSelection>(linked.selection);
  // A link this page cannot serve is a state, not a constant: reading the
  // whole catalogue instead is a choice the admin makes below, not one this
  // page makes for them by quietly dropping the parameter.
  const [linkError, setLinkError] = useState<string | null>(linked.error);
  const [denied, setDenied] = useState(false);
  const [refreshKey, setRefreshKey] = useState(0);
  const onDenied = useCallback(() => setDenied(true), []);

  useEffect(() => {
    // Native history keeps the app router out of this: the panels already hold
    // the applied selection, so a reactive navigation would only re-render it.
    // A link this page cannot serve is left exactly as it is.
    if (linkError) return;
    window.history.replaceState(null, '', selectionHref(window.location.pathname, selection));
  }, [selection, linkError]);

  if (denied) return <AdminRequired />;

  const apply = (next: MeasurementSelection) => {
    setLinkError(null);
    setSelection(next);
  };

  return (
    <div className="flex-1 overflow-y-auto">
      <section className="mx-auto w-full max-w-7xl px-6 py-10 lg:px-12 lg:py-14">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="measurements" />

        <div className="mt-8 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h2 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
              Operation measurements
            </h2>
            <p className="mt-1 max-w-3xl text-sm text-gray-500 dark:text-gray-400">
              Stored platform-operation records read back as they were frozen: an experiment list,
              one page of its samples, a single record with its stages and reasons, and the
              distribution the server aggregates over the whole experiment.
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
            {linkError} Nothing was read, because answering a wider question than this link asked
            would hide what happened to it.{' '}
            <Link className="underline" href={MEASUREMENTS_PATH}>
              Read all experiments instead
            </Link>
            .
          </ErrorNotice>
        ) : (
          <>
            <CoverageNotice />
            <WriterStatusPanel onDenied={onDenied} refreshKey={refreshKey} />
            <RunsPanel selection={selection} onDenied={onDenied} refreshKey={refreshKey} />
            <MeasurementsPanel
              selection={selection}
              onApply={apply}
              onDenied={onDenied}
              refreshKey={refreshKey}
            />
            {selection.measurement && (
              <RecordPanel
                key={selection.measurement}
                measurementId={selection.measurement}
                selection={selection}
                onDenied={onDenied}
                refreshKey={refreshKey}
              />
            )}
            {selection.run && (
              <DistributionPanel
                key={selection.run}
                runId={selection.run}
                onDenied={onDenied}
                refreshKey={refreshKey}
              />
            )}
          </>
        )}
      </section>
    </div>
  );
}

/** The one notice the whole page is read under. */
function CoverageNotice() {
  return (
    <section
      className={`${CARD} mt-6 border-amber-200 bg-amber-50/70 p-5 dark:border-amber-500/25 dark:bg-amber-500/10`}
      aria-labelledby="coverage-heading"
    >
      <h3
        id="coverage-heading"
        className="text-sm font-semibold text-amber-900 dark:text-amber-300"
      >
        What these measurements cover
      </h3>
      <p className="mt-2 text-sm text-amber-900 dark:text-amber-200">{SERVER_ONLY_NOTICE}</p>
      <dl className="mt-3 grid gap-2 text-xs text-amber-900/90 dark:text-amber-200/90">
        {(['server_request', 'host_startup'] as const).map((scope) => (
          <div key={scope}>
            <dt className="font-semibold">{coverageScope(scope).label}</dt>
            <dd>{coverageScope(scope).basis}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function WriterStatusPanel({ onDenied, refreshKey }: { onDenied: () => void; refreshKey: number }) {
  const read = useCallback(
    (signal: AbortSignal, _nonce: number) => getMeasurementWriterStatus(signal),
    [],
  );
  const { data, error, loading, stale } = useAdminRead<MeasurementWriterStatus>(
    read,
    refreshKey,
    onDenied,
  );
  const counts = data?.counts;
  const lost = counts
    ? counts.rejected +
      counts.conflict +
      counts.dropped_full +
      counts.dropped_timeout +
      counts.dropped_shutdown +
      counts.failed
    : 0;
  return (
    <section className={`${CARD} mt-5 p-5`} aria-labelledby="writer-status-heading">
      <h3
        id="writer-status-heading"
        className="text-sm font-semibold text-gray-900 dark:text-gray-50"
      >
        Local persistence writer
      </h3>
      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
        This is the control-plane process that answered this Admin request, not a fleet aggregate.
        It makes queue loss visible without claiming another replica reported zero.
      </p>
      {error && (
        <ErrorNotice>
          {error}
          {stale && ' The last refresh failed; these are the previous successful counters.'}
        </ErrorNotice>
      )}
      {loading && !data ? (
        <div className="mt-3 h-8 animate-pulse rounded-lg bg-gray-100 dark:bg-white/5" />
      ) : (
        data && (
          <dl className="mt-3 grid gap-3 text-xs sm:grid-cols-3 lg:grid-cols-6">
            <ManifestItem label="Workers">{data.running ? 'Running' : 'Idle / off'}</ManifestItem>
            <ManifestItem label="Waiting">{number(data.pending)}</ManifestItem>
            <ManifestItem label="In flight">{number(data.in_flight)}</ManifestItem>
            <ManifestItem label="Queued bytes">{number(data.queued_bytes)}</ManifestItem>
            <ManifestItem label="Accepted">
              {number(data.counts.stored + data.counts.duplicate)}
            </ManifestItem>
            <ManifestItem label="Rejected or lost">{number(lost)}</ManifestItem>
          </dl>
        )
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Experiments
// ---------------------------------------------------------------------------

function RunsPanel({
  selection,
  onDenied,
  refreshKey,
}: {
  selection: MeasurementSelection;
  onDenied: () => void;
  refreshKey: number;
}) {
  const read = useCallback(
    (signal: AbortSignal, _nonce: number) => listProfilingRuns({ limit: RUN_LIMIT }, signal),
    [],
  );
  const { data, error, loading, stale } = useAdminRead<ProfilingRunCatalogue>(
    read,
    refreshKey,
    onDenied,
  );

  return (
    <section className={`${CARD} mt-5 overflow-hidden`} aria-labelledby="experiments-heading">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-gray-100 px-5 py-4 dark:border-white/5">
        <div>
          <h3
            id="experiments-heading"
            className="text-sm font-semibold text-gray-900 dark:text-gray-50"
          >
            Experiments
          </h3>
          <p className="mt-1 max-w-3xl text-xs text-gray-500 dark:text-gray-400">
            A run appears here only once a producer has delivered a measurement for it, so an
            experiment that collected nothing is not listed at all -- and its absence is not
            evidence that it was switched off.
            {data &&
              ` ${number(data.count)} of at most ${number(data.limit)} runs, newest delivery first.`}
          </p>
        </div>
        {selection.run && (
          <Link
            className="text-xs font-medium text-crimson hover:underline dark:text-crimson-light"
            href={selectionHref(MEASUREMENTS_PATH, {
              ...selection,
              run: null,
              measurement: null,
              after: null,
            })}
          >
            Read every experiment
          </Link>
        )}
      </div>

      {error && (
        <ErrorNotice>
          {error}
          {stale && ' The last refresh failed; these are the previous successful read.'}
        </ErrorNotice>
      )}

      {loading && !data ? (
        <RowsSkeleton rows={3} />
      ) : (
        data && (
          <>
            {selection.run && <RunManifest runId={selection.run} catalogue={data} />}
            <div className="overflow-x-auto">
              <table className="min-w-full text-left text-sm">
                <thead className="border-b border-gray-100 bg-gray-50/70 text-[11px] uppercase tracking-wide text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
                  <tr>
                    {[
                      'Experiment',
                      'Operations',
                      'Target',
                      'Config',
                      'Expires',
                      'Delivered',
                      '',
                    ].map((column) => (
                      <th
                        key={column || 'open'}
                        className="whitespace-nowrap px-5 py-3 font-medium"
                      >
                        {column}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                  {data.runs.length === 0 && (
                    <tr>
                      <td
                        colSpan={7}
                        className="px-5 py-6 text-sm text-gray-500 dark:text-gray-400"
                      >
                        No experiment has delivered a measurement this deployment can read. That is
                        not the same as no experiment having run.
                      </td>
                    </tr>
                  )}
                  {data.runs.map((run) => (
                    <RunRow key={run.run_id} run={run} selection={selection} />
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )
      )}
    </section>
  );
}

function RunRow({ run, selection }: { run: ProfilingRun; selection: MeasurementSelection }) {
  const selected = selection.run === run.run_id;
  const targets = targetLines(run.target);
  return (
    <tr
      aria-current={selected ? 'true' : undefined}
      className={`align-top transition-colors hover:bg-gray-50/70 dark:hover:bg-white/[0.03] ${
        selected ? 'bg-gray-50 dark:bg-white/[0.04]' : ''
      }`}
    >
      <th scope="row" className="max-w-xs px-5 py-3 font-normal">
        {selected ? (
          <span className="block truncate font-medium text-gray-900 dark:text-gray-100">
            {run.run_id}
          </span>
        ) : (
          <Link
            className="block truncate font-medium text-crimson hover:underline dark:text-crimson-light"
            href={selectionHref(MEASUREMENTS_PATH, {
              ...selection,
              run: run.run_id,
              measurement: null,
              after: null,
            })}
          >
            {run.run_id}
          </Link>
        )}
        {selected && (
          <span className="text-xs text-gray-500 dark:text-gray-400">Selected below</span>
        )}
      </th>
      <td className="max-w-xs px-5 py-3 text-gray-700 dark:text-gray-300">
        <span className="flex flex-wrap gap-1">
          {run.operations.map((operation) => (
            <span
              key={operation}
              className="rounded-full bg-gray-100 px-2 py-0.5 text-xs text-gray-600 dark:bg-white/5 dark:text-gray-300"
            >
              {operation}
            </span>
          ))}
        </span>
      </td>
      <td className="px-5 py-3 text-xs text-gray-500 dark:text-gray-400">
        {targets.length === 0
          ? 'Every target'
          : targets.map((target) => `${target.name}=${target.value}`).join(', ')}
      </td>
      <td className="px-5 py-3 tabular-nums text-gray-700 dark:text-gray-300">
        {number(run.config_version)}
      </td>
      <td className="whitespace-nowrap px-5 py-3 text-xs text-gray-500 dark:text-gray-400">
        {dateTime(run.expires_at)}
      </td>
      <td className="whitespace-nowrap px-5 py-3 text-xs text-gray-500 dark:text-gray-400">
        {dateTime(run.received_at)}
      </td>
      <td className="px-5 py-3 text-right">
        {!selected && (
          <Link
            className="text-xs font-medium text-crimson hover:underline dark:text-crimson-light"
            href={selectionHref(MEASUREMENTS_PATH, {
              ...selection,
              run: run.run_id,
              measurement: null,
              after: null,
            })}
          >
            Read this experiment
          </Link>
        )}
      </td>
    </tr>
  );
}

/** The manifest of the selected experiment, when this catalogue page holds it. */
function RunManifest({ runId, catalogue }: { runId: string; catalogue: ProfilingRunCatalogue }) {
  const run = catalogue.runs.find((entry) => entry.run_id === runId) ?? null;
  if (!run) {
    return (
      <p className="border-b border-gray-100 px-5 py-3 text-xs text-gray-500 dark:border-white/5 dark:text-gray-400">
        This experiment is not among the {number(catalogue.count)} catalogue rows read here, so its
        manifest was not read. Its stored samples are still listed below; a wider catalogue would be
        a different question, not a bigger answer to this one.
      </p>
    );
  }
  const targets = targetLines(run.target);
  const targetText = targets.length
    ? targets.map((target) => `${target.name}=${target.value}`).join(', ')
    : 'Every target';
  return (
    <dl className="grid gap-x-8 gap-y-2 border-b border-gray-100 bg-gray-50/50 px-5 py-4 text-xs sm:grid-cols-2 lg:grid-cols-3 dark:border-white/5 dark:bg-white/[0.02]">
      <ManifestItem label="Experiment">{run.run_id}</ManifestItem>
      <ManifestItem label="Configured version">{number(run.config_version)}</ManifestItem>
      <ManifestItem label="Configured expiry">{dateTime(run.expires_at)}</ManifestItem>
      <ManifestItem label="Operations">
        {run.operations.length ? run.operations.join(', ') : 'No operation selected'}
      </ManifestItem>{' '}
      <ManifestItem label="Target">{targetText}</ManifestItem>
      <ManifestItem label="Admission instances">
        {run.admission_instances.length
          ? run.admission_instances.map(admissionLine).join(' · ')
          : 'None reported'}
      </ManifestItem>
      <ManifestItem label="Catalogue row created">{dateTime(run.created_at)}</ManifestItem>
      <ManifestItem label="Last received">{dateTime(run.received_at)}</ManifestItem>
      <ManifestItem label="Expiry is configuration">
        The configured instant, not the deadline a producer has left.
      </ManifestItem>
    </dl>
  );
}

function ManifestItem({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <dt className="font-medium text-gray-500 dark:text-gray-400">{label}</dt>
      <dd className="mt-0.5 break-words text-gray-700 dark:text-gray-300">{children}</dd>
    </div>
  );
}

function admissionLine(instance: ProfilingAdmissionInstance): string {
  const name =
    typeof instance.instance === 'string' && instance.instance
      ? instance.instance
      : 'instance not named';
  const rest = Object.entries(instance).filter(([key]) => key !== 'instance');
  return rest.length
    ? `${name} (${rest.map(([key, value]) => `${key}=${factValue(value)}`).join(', ')})`
    : name;
}

// ---------------------------------------------------------------------------
// The measurement list
// ---------------------------------------------------------------------------

function MeasurementsPanel({
  selection,
  onApply,
  onDenied,
  refreshKey,
}: {
  selection: MeasurementSelection;
  onApply: (next: MeasurementSelection) => void;
  onDenied: () => void;
  refreshKey: number;
}) {
  const [formError, setFormError] = useState<string | null>(null);
  const read = useCallback(
    (signal: AbortSignal, _nonce: number) =>
      listMeasurements(
        {
          run_id: selection.run ?? undefined,
          operation: selection.operation,
          request_kind: selection.request_kind,
          coverage_scope: selection.coverage_scope,
          outcome: selection.outcome ?? undefined,
          collection_complete: selection.collection_complete ?? undefined,
          job_id: selection.job_id ?? undefined,
          thread_id: selection.thread_id ?? undefined,
          // Parsed once, here, so an unparseable value can never be silently
          // dropped on the way to a wider read. The URL reader refuses those
          // links before this panel runs; the form is the other way in, and it
          // reaches the same parser.
          attempt_id: parseAttemptId(selection.attempt_id ?? '').attemptId ?? undefined,
          interaction_id: selection.interaction_id ?? undefined,
          after: selection.after ?? undefined,
        },
        signal,
      ),
    [selection],
  );
  const { data, error, loading, stale } = useAdminRead<MeasurementPage>(read, refreshKey, onDenied);

  // The key carries the applied value, so a new selection re-renders each
  // field with its own default rather than leaving the previous one on screen.
  const fields: { key: FormKey; label: string; placeholder: string; value: string }[] = [
    {
      key: 'operation',
      label: 'Operations',
      placeholder: 'files.list, files.open',
      value: selection.operation.join(', '),
    },
    {
      key: 'request_kind',
      label: 'Request kinds',
      placeholder: 'terminal.create',
      value: selection.request_kind.join(', '),
    },
    {
      key: 'coverage_scope',
      label: 'Coverage scopes',
      placeholder: 'server_request, host_startup',
      value: selection.coverage_scope.join(', '),
    },
    { key: 'outcome', label: 'Outcome', placeholder: 'Any', value: selection.outcome ?? '' },
    { key: 'job_id', label: 'Job ID', placeholder: 'Any', value: selection.job_id ?? '' },
    { key: 'thread_id', label: 'Thread ID', placeholder: 'Any', value: selection.thread_id ?? '' },
    {
      key: 'attempt_id',
      label: 'Attempt ID',
      placeholder: 'Any',
      value: selection.attempt_id ?? '',
    },
    {
      key: 'interaction_id',
      label: 'Interaction ID',
      placeholder: 'Any',
      value: selection.interaction_id ?? '',
    },
  ];

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget.elements;
    const value = (key: FormKey) => {
      const field = form.namedItem(key);
      return field instanceof HTMLInputElement || field instanceof HTMLSelectElement
        ? field.value
        : '';
    };
    const next = selectionFromForm(value, selection.run);
    // Refused here rather than parsed later: applying an attempt id this page
    // cannot send would clear the filter and list every attempt while the
    // field still showed the one that was asked for.
    const attempt = parseAttemptId(next.attempt_id ?? '');
    if (attempt.error) {
      setFormError(attempt.error);
      return;
    }
    setFormError(null);
    onApply(next);
  }

  return (
    <section className={`${CARD} mt-5 overflow-hidden`} aria-labelledby="measurements-heading">
      <div className="border-b border-gray-100 px-5 py-4 dark:border-white/5">
        <h3
          id="measurements-heading"
          className="text-sm font-semibold text-gray-900 dark:text-gray-50"
        >
          Measurements
        </h3>
        <p className="mt-1 max-w-3xl text-xs text-gray-500 dark:text-gray-400">
          {selectionSummary(selection)}
        </p>
      </div>

      <form
        onSubmit={apply}
        aria-label="Measurement filters"
        className="border-b border-gray-100 px-5 py-4 dark:border-white/5"
      >
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {fields.map((field) => (
            <label
              key={`${field.key}:${field.value}`}
              className="text-xs font-medium text-gray-500 dark:text-gray-400"
            >
              {field.label}
              <input
                className={`${INPUT} mt-1.5`}
                name={field.key}
                defaultValue={field.value}
                placeholder={field.placeholder}
              />
            </label>
          ))}
          <label className="text-xs font-medium text-gray-500 dark:text-gray-400">
            Collection
            <select
              className={`${INPUT} mt-1.5`}
              name="collection_complete"
              defaultValue={
                selection.collection_complete === null ? '' : String(selection.collection_complete)
              }
            >
              <option value="">Any</option>
              <option value="true">Closed only</option>
              <option value="false">Incomplete only</option>
            </select>
          </label>
        </div>
        <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
          A comma or space separates several values in one field. Applying resets the cursor,
          because a cursor is a position in one ordering and resuming it under different filters
          would splice two selections together. Attempt ID and Interaction ID are different keys,
          not two spellings of one: an attempt selects that execution&rsquo;s startup record, while
          an interaction selects the list, create and stream-attach requests of one terminal open
          &mdash; three records that carry no attempt at all.
        </p>
        {formError && (
          <div className="mt-3">
            <ErrorNotice>{formError} Nothing was re-read.</ErrorNotice>
          </div>
        )}
        <div className="mt-4 flex justify-end border-t border-gray-100 pt-4 dark:border-white/5">
          <button type="submit" className={BUTTON}>
            Apply filters
          </button>
        </div>
      </form>

      {error && (
        <ErrorNotice>
          {error}
          {stale && ' The last refresh failed; these are the previous successful read.'}
        </ErrorNotice>
      )}

      {loading && !data ? (
        <RowsSkeleton rows={5} />
      ) : (
        data && (
          <>
            <p className="border-b border-gray-100 px-5 py-3 text-xs text-gray-500 dark:border-white/5 dark:text-gray-400">
              {listNotice(data)}
            </p>
            {data.count === 0 ? null : <MeasurementTable page={data} selection={selection} />}
            <Pager page={data} selection={selection} />
          </>
        )
      )}
    </section>
  );
}

function MeasurementTable({
  page,
  selection,
}: {
  page: MeasurementPage;
  selection: MeasurementSelection;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-left text-sm">
        <thead className="border-b border-gray-100 bg-gray-50/70 text-[11px] uppercase tracking-wide text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
          <tr>
            {[
              'Operation',
              'Request',
              'Scope',
              'Duration',
              'Outcome',
              'Completeness',
              'Received',
              '',
            ].map((column) => (
              <th key={column || 'open'} className="whitespace-nowrap px-5 py-3 font-medium">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-white/5">
          {page.measurements.map((row) => (
            <MeasurementRow key={row.measurement_id} row={row} selection={selection} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MeasurementRow({
  row,
  selection,
}: {
  row: MeasurementSummary;
  selection: MeasurementSelection;
}) {
  const measured = duration(row.duration_us);
  const partial = rowIsPartial(row);
  const open = selection.measurement === row.measurement_id;
  return (
    <tr
      aria-current={open ? 'true' : undefined}
      className={`align-top transition-colors hover:bg-gray-50/70 dark:hover:bg-white/[0.03] ${
        open ? 'bg-gray-50 dark:bg-white/[0.04]' : ''
      }`}
    >
      <th scope="row" className="max-w-xs px-5 py-3 font-normal">
        <span className="block truncate font-medium text-gray-900 dark:text-gray-100">
          {row.operation}
        </span>
        <span className="block truncate text-xs text-gray-500 dark:text-gray-400">
          {row.measurement_id}
        </span>
      </th>
      <td className="px-5 py-3 text-xs text-gray-500 dark:text-gray-400">
        {row.request_kind || 'Not reported'}
      </td>
      <td className="px-5 py-3 text-xs text-gray-500 dark:text-gray-400">
        {coverageScope(row.coverage_scope).label}
      </td>
      <td
        className="whitespace-nowrap px-5 py-3 text-right tabular-nums text-gray-700 dark:text-gray-300"
        title={measured.exact ?? undefined}
      >
        {measured.display}
      </td>
      <td className="px-5 py-3 text-xs text-gray-700 dark:text-gray-300">
        {outcomeLabel(row.outcome)}
      </td>
      <td className="px-5 py-3 text-xs">
        <span
          className={
            partial ? 'text-amber-700 dark:text-amber-400' : 'text-gray-500 dark:text-gray-400'
          }
        >
          {rowCompleteness(row)}
        </span>
      </td>
      <td className="whitespace-nowrap px-5 py-3 text-xs text-gray-500 dark:text-gray-400">
        {dateTime(row.received_at)}
      </td>
      <td className="px-5 py-3 text-right">
        <Link
          className="text-xs font-medium text-crimson hover:underline dark:text-crimson-light"
          href={selectionHref(MEASUREMENTS_PATH, {
            ...selection,
            measurement: row.measurement_id,
          })}
        >
          {open ? 'Shown below' : 'Open record'}
        </Link>
      </td>
    </tr>
  );
}

/**
 * Paging, as links.
 *
 * A cursor resumes one ordering; the link carries the whole applied selection
 * with it, so the next page is the next page of the same question.
 */
function Pager({ page, selection }: { page: MeasurementPage; selection: MeasurementSelection }) {
  const first = selection.after
    ? selectionHref(MEASUREMENTS_PATH, { ...selection, after: null, measurement: null })
    : null;
  const nextSelection = page.next_cursor
    ? { ...selection, after: page.next_cursor, measurement: null }
    : null;
  const next = nextSelection ? selectionHref(MEASUREMENTS_PATH, nextSelection) : null;
  if (!first && !next) return null;
  return (
    <div className="flex items-center justify-between gap-3 border-t border-gray-100 px-5 py-4 text-xs dark:border-white/5">
      <span className="text-gray-500 dark:text-gray-400">
        {selection.after
          ? 'Continuing after the row this page started from.'
          : 'This is the first page of the selection.'}
      </span>
      <span className="flex gap-2">
        {first && (
          <Link className={BUTTON} href={first}>
            First page
          </Link>
        )}
        {next && (
          <Link className={BUTTON} href={next}>
            Next page
          </Link>
        )}
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// One frozen record
// ---------------------------------------------------------------------------

function RecordPanel({
  measurementId,
  selection,
  onDenied,
  refreshKey,
}: {
  measurementId: string;
  selection: MeasurementSelection;
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
    <section className={`${CARD} mt-5 overflow-hidden`} aria-labelledby="record-heading">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-gray-100 px-5 py-4 dark:border-white/5">
        <div>
          <h3 id="record-heading" className="text-sm font-semibold text-gray-900 dark:text-gray-50">
            {data ? detailHeading(data) : 'Record'} — frozen record
          </h3>
          <p className="mt-1 max-w-3xl text-xs text-gray-500 dark:text-gray-400">
            {measurementId}. Read from the stored record, not recomputed: that is what makes it the
            same sample the producer delivered.
          </p>
        </div>
        <Link
          className="text-xs font-medium text-crimson hover:underline dark:text-crimson-light"
          href={selectionHref(MEASUREMENTS_PATH, { ...selection, measurement: null })}
        >
          Close record
        </Link>
      </div>

      {error && (
        <ErrorNotice>
          {error}
          {stale && ' The last refresh failed; this is the previous successful read.'}
        </ErrorNotice>
      )}

      {loading && !data ? <RowsSkeleton rows={4} /> : data && <RecordBody detail={data} />}
    </section>
  );
}

function RecordBody({ detail }: { detail: MeasurementDetail }) {
  const record = detail.record;
  const reasons = partialReasons(record);
  const stages = stageRows(record);
  const totals = duration(record.duration_us);
  return (
    <div className="px-5 py-4">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <Badge tone={reasons.length ? 'warn' : 'plain'}>
          {reasons.length ? 'Partial or failed' : 'Nothing flags this record as partial'}
        </Badge>
        {/* The outcome is never shown without the completeness beside it. */}
        <Badge tone={isCompleteOutcome(record.outcome) ? 'plain' : 'warn'}>
          {outcomeLabel(record.outcome)}
        </Badge>
        <Badge tone="warn">{coverageLine(record.coverage_scope).split(' — ')[0]}</Badge>
        <Badge tone="plain">Total {totals.display}</Badge>
        {record.truncated && <Badge tone="warn">Truncated by the collection deadline</Badge>}
      </div>

      {reasons.length === 0 ? (
        <p className="mt-3 text-sm text-gray-700 dark:text-gray-300">
          Nothing in this record says it is partial. That is not a claim of completeness: the record
          covers the server side only, and its collection is the producer’s own.
        </p>
      ) : (
        <div
          role="alert"
          className="mt-3 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-500/25 dark:bg-amber-500/10 dark:text-amber-200"
        >
          <p className="font-medium">This record does not describe a whole operation.</p>
          <ul className="mt-1 list-inside list-disc space-y-1">
            {reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </div>
      )}

      <dl className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {completenessLines(record).map((line) => (
          <div
            key={line.label}
            className={`rounded-xl border p-3 text-xs ${
              line.warn
                ? 'border-amber-200 bg-amber-50/60 dark:border-amber-500/25 dark:bg-amber-500/10'
                : 'border-gray-200 dark:border-white/10'
            }`}
          >
            <dt className="font-semibold text-gray-900 dark:text-gray-100">{line.label}</dt>
            <dd className="mt-1 text-gray-600 dark:text-gray-400">{line.value}</dd>
          </div>
        ))}
      </dl>

      <h4 className="mt-6 text-sm font-semibold text-gray-900 dark:text-gray-50">Stage timings</h4>
      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
        In the order they were recorded. A stage’s share is of this record’s total; an attached
        child carries another producer’s duration and is never added to it.
      </p>
      {stages.length === 0 ? (
        <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
          This record lists no stages. That means no boundary was recorded, which is not the same as
          a stage that took no time.
        </p>
      ) : (
        <div className="mt-2 overflow-x-auto">
          <table className="min-w-full text-left text-sm">
            <thead className="border-b border-gray-100 text-[11px] uppercase tracking-wide text-gray-500 dark:border-white/5 dark:text-gray-400">
              <tr>
                {['Stage', 'Starts at', 'Duration', 'Share', 'Reported by', ''].map((column) => (
                  <th key={column || 'notes'} className="whitespace-nowrap px-3 py-2 font-medium">
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-white/5">
              {stages.map((row) => {
                const measured = duration(row.durationUs);
                return (
                  <tr key={row.key} className="align-top">
                    <th
                      scope="row"
                      className="px-3 py-2 font-normal text-gray-900 dark:text-gray-100"
                      style={
                        row.depth ? { paddingLeft: `${row.depth * 1.5 + 0.75}rem` } : undefined
                      }
                    >
                      {row.name}
                      {row.depth > 0 && (
                        <span className="ml-2 rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-500 dark:bg-white/5 dark:text-gray-400">
                          attached
                        </span>
                      )}
                      {row.callId && (
                        <span className="mt-1 block text-[11px] text-gray-400 dark:text-gray-500">
                          call {row.callId}
                        </span>
                      )}
                    </th>
                    <td className="whitespace-nowrap px-3 py-2 text-xs text-gray-500 dark:text-gray-400">
                      {row.startUs === null
                        ? 'No offset on this timeline'
                        : duration(row.startUs).display}
                    </td>
                    <td
                      className="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300"
                      title={measured.exact ?? undefined}
                    >
                      {measured.display}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2 text-right tabular-nums text-gray-500 dark:text-gray-400">
                      {row.share ?? 'Not on this timeline'}
                    </td>
                    <td className="px-3 py-2 text-xs text-gray-500 dark:text-gray-400">
                      {row.source ?? 'This record’s producer'}
                    </td>
                    <td className="px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
                      {row.truncated && <span>{TRUNCATED_STAGE_NOTE}</span>}
                      {row.depth > 0 && !row.truncated && (
                        <span className="text-gray-400 dark:text-gray-500">
                          {ATTACHED_STAGE_NOTE}
                        </span>
                      )}
                      {row.outcome && (
                        <span className="mt-1 block text-gray-500 dark:text-gray-400">
                          {outcomeLabel(row.outcome)}
                        </span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <div>
          <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-50">Milestones</h4>
          {record.milestones?.length ? (
            <ul className="mt-2 space-y-1 text-xs text-gray-600 dark:text-gray-400">
              {record.milestones.map((milestone, index) => (
                <li key={`${milestone.name}:${index}`} className="flex justify-between gap-3">
                  <span className="truncate">{milestone.name}</span>
                  <span className="whitespace-nowrap tabular-nums">
                    {duration(milestone.offset_us).display} · {milestone.source}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
              No milestone was recorded. Offsets are relative to the record’s start.
            </p>
          )}
        </div>
        <div>
          <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-50">Spans</h4>
          {record.spans?.length ? (
            <ul className="mt-2 space-y-1 text-xs text-gray-600 dark:text-gray-400">
              {record.spans.map((span) => (
                <li key={span.span_id} className="flex justify-between gap-3">
                  <span className="truncate">
                    {span.name}{' '}
                    <span className="text-gray-400 dark:text-gray-500">#{span.ordinal}</span>
                    {span.parent_span_id && (
                      <span className="ml-1 text-gray-400 dark:text-gray-500">(child)</span>
                    )}
                  </span>
                  <span className="whitespace-nowrap tabular-nums">
                    {span.duration_us === undefined
                      ? 'No duration reported'
                      : duration(span.duration_us).display}
                    {span.truncated ? ' (truncated)' : ''} · {outcomeLabel(span.outcome)}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
              No span was recorded for this measurement.
            </p>
          )}
        </div>
      </div>

      <h4 className="mt-6 text-sm font-semibold text-gray-900 dark:text-gray-50">
        Identity and provenance
      </h4>
      <dl className="mt-2 grid gap-x-8 gap-y-2 text-xs sm:grid-cols-2 lg:grid-cols-3">
        <ManifestItem label="Outcome">{outcomeLabel(record.outcome)}</ManifestItem>
        <ManifestItem label="Failure stage">{record.failure_stage ?? 'None recorded'}</ManifestItem>
        <ManifestItem label="Measurement">{detail.measurement_id}</ManifestItem>
        <ManifestItem label="Interaction">
          <Link
            className="text-crimson hover:underline dark:text-crimson-light"
            href={selectionHref(MEASUREMENTS_PATH, {
              ...EMPTY_SELECTION,
              run: detail.profiling_run_id,
              interaction_id: detail.interaction_id,
            })}
          >
            {detail.interaction_id}
          </Link>
        </ManifestItem>
        <ManifestItem label="Attempt">
          {detail.attempt_id === null ? (
            // Two different absences, said apart: a file or terminal request
            // genuinely has no attempt, while a startup record that lost its
            // attribution never got one. Neither is zero.
            detail.operation === 'attempt.startup' ? (
              'Not attributed'
            ) : (
              'Not a startup record'
            )
          ) : (
            <Link
              className="text-crimson hover:underline dark:text-crimson-light"
              href={selectionHref(MEASUREMENTS_PATH, {
                ...EMPTY_SELECTION,
                run: detail.profiling_run_id,
                attempt_id: String(detail.attempt_id),
              })}
            >
              {detail.attempt_id}
            </Link>
          )}
        </ManifestItem>
        <ManifestItem label="Experiment">
          <Link
            className="text-crimson hover:underline dark:text-crimson-light"
            href={selectionHref(MEASUREMENTS_PATH, {
              ...EMPTY_SELECTION,
              run: detail.profiling_run_id,
            })}
          >
            {detail.profiling_run_id}
          </Link>
        </ManifestItem>
        <ManifestItem label="Job">
          {detail.job_id ? (
            <Link
              className="text-crimson hover:underline dark:text-crimson-light"
              href={`/admin/runs/${encodeURIComponent(detail.job_id)}`}
            >
              {detail.job_id}
            </Link>
          ) : (
            'Not linked to a job'
          )}
        </ManifestItem>
        <ManifestItem label="Thread">
          {detail.thread_id ? (
            <Link
              className="text-crimson hover:underline dark:text-crimson-light"
              href={`/admin/conversations/${encodeURIComponent(detail.thread_id)}`}
            >
              {detail.thread_id}
            </Link>
          ) : (
            'Not linked to a thread'
          )}
        </ManifestItem>
        <ManifestItem label="Attempt">
          {detail.attempt_id ?? 'No attempt (not a startup record)'}
        </ManifestItem>
        <ManifestItem label="Owner">{detail.owner_id ?? 'Not reported'}</ManifestItem>
        <ManifestItem label="Host">{detail.host_id ?? 'Not reported'}</ManifestItem>
        <ManifestItem label="Runtime">
          {detail.runtime_id
            ? `${detail.runtime_id}${detail.runtime_version ? ` @ ${detail.runtime_version}` : ''}`
            : 'Not reported'}
        </ManifestItem>
        <ManifestItem label="Execution profile">
          {detail.execution_profile ?? 'Not reported'}
        </ManifestItem>
        <ManifestItem label="Producer">
          {record.producer} · {record.producer_instance_id}
        </ManifestItem>
        <ManifestItem label="Timing sources">
          {record.timing_sources?.length ? record.timing_sources.join(', ') : 'Not reported'}
        </ManifestItem>
        <ManifestItem label="Producers on this record">
          {record.producers?.length ? record.producers.join(', ') : 'Not reported'}
        </ManifestItem>
        <ManifestItem label="Clock domain">{record.clock_domain_id ?? 'Not reported'}</ManifestItem>
        <ManifestItem label="Schema / boundary version">
          {number(record.schema_version)} / {number(record.boundary_version)}
        </ManifestItem>
        <ManifestItem label="Measured boundary">
          {record.start_boundary && record.end_boundary
            ? `${record.start_boundary} → ${record.end_boundary}`
            : 'Not reported'}
        </ManifestItem>
        <ManifestItem label="Observed at (producer clock)">
          {dateTime(record.observed_at)}
        </ManifestItem>
        <ManifestItem label="Received at (database clock)">
          {dateTime(detail.received_at)}
        </ManifestItem>
        <ManifestItem label="Run expiry">{dateTime(record.run_expires_at)}</ManifestItem>
        <ManifestItem label="Boundary frozen">
          {record.boundary_frozen === undefined
            ? 'Not reported'
            : record.boundary_frozen
              ? 'Frozen with the end boundary'
              : 'Not frozen'}
        </ManifestItem>
        <ManifestItem label="Payload hash">{detail.payload_hash}</ManifestItem>
      </dl>

      {record.facts && Object.keys(record.facts).length > 0 && (
        <>
          <h4 className="mt-6 text-sm font-semibold text-gray-900 dark:text-gray-50">Facts</h4>
          <dl className="mt-2 grid gap-x-8 gap-y-2 text-xs sm:grid-cols-2 lg:grid-cols-3">
            {factLines(record.facts).map((fact) => (
              <ManifestItem key={fact.name} label={fact.name}>
                {fact.value}
              </ManifestItem>
            ))}
          </dl>
        </>
      )}
    </div>
  );
}

function Badge({ tone, children }: { tone: 'plain' | 'warn'; children: ReactNode }) {
  const styles =
    tone === 'warn'
      ? 'bg-amber-100 text-amber-900 ring-amber-600/20 dark:bg-amber-500/10 dark:text-amber-300 dark:ring-amber-500/25'
      : 'bg-gray-100 text-gray-700 ring-gray-500/20 dark:bg-white/5 dark:text-gray-300 dark:ring-white/15';
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 font-medium ring-1 ring-inset ${styles}`}
    >
      {children}
    </span>
  );
}

// ---------------------------------------------------------------------------
// The distribution
// ---------------------------------------------------------------------------

function DistributionPanel({
  runId,
  onDenied,
  refreshKey,
}: {
  runId: string;
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

  return (
    <section className={`${CARD} mt-5 overflow-hidden`} aria-labelledby="distribution-heading">
      <div className="border-b border-gray-100 px-5 py-4 dark:border-white/5">
        <h3
          id="distribution-heading"
          className="text-sm font-semibold text-gray-900 dark:text-gray-50"
        >
          Distribution over the whole experiment
        </h3>
        <p className="mt-1 max-w-3xl text-xs text-gray-500 dark:text-gray-400">
          Experiment {runId}. {DISTRIBUTION_BASIS} It is not filtered by the form above: the summary
          route has no filter, and pretending otherwise would describe a subset it never computed.
        </p>
      </div>

      {error && (
        <ErrorNotice>
          {error}
          {stale && ' The last refresh failed; this is the previous successful read.'}
        </ErrorNotice>
      )}

      {loading && !data ? (
        <RowsSkeleton rows={4} />
      ) : (
        data &&
        (data.groups.length === 0 ? (
          <p className="px-5 py-5 text-sm text-gray-500 dark:text-gray-400">
            The server aggregated every stored sample for this experiment and returned no groups at
            all, so nothing has been stored for it yet. Percentiles are not zero -- there is nothing
            to compute them from.
          </p>
        ) : (
          <>
            <p className="border-b border-gray-100 px-5 py-3 text-xs text-gray-500 dark:border-white/5 dark:text-gray-400">
              {summaryLine(data)}
            </p>{' '}
            <div className="overflow-x-auto">
              <table className="min-w-full text-left text-sm">
                <thead className="border-b border-gray-100 bg-gray-50/70 text-[11px] uppercase tracking-wide text-gray-500 dark:border-white/5 dark:bg-white/[0.02] dark:text-gray-400">
                  <tr>
                    {['Group', 'Sampled', 'Interactions', 'p50', 'p95', 'Min', 'Max', ''].map(
                      (column) => (
                        <th
                          key={column || 'basis'}
                          className="whitespace-nowrap px-5 py-3 font-medium"
                        >
                          {column}
                        </th>
                      ),
                    )}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                  {groupRows(data).map((row) => (
                    <tr key={row.key} className="align-top">
                      <th scope="row" className="max-w-sm px-5 py-3 font-normal">
                        <span className="flex flex-wrap gap-1">
                          {row.dimensions.map((dimension) => (
                            <span
                              key={dimension.name}
                              className="rounded-full bg-gray-100 px-2 py-0.5 text-[11px] text-gray-600 dark:bg-white/5 dark:text-gray-300"
                            >
                              {dimension.name}={dimension.value}
                            </span>
                          ))}
                        </span>
                      </th>
                      <td className="whitespace-nowrap px-5 py-3 text-right tabular-nums text-gray-700 dark:text-gray-300">
                        {number(row.sampleCount)}
                        <span className="block text-[11px] text-gray-500 dark:text-gray-400">
                          {number(row.eligibleCount)} eligible
                        </span>
                      </td>
                      <td className="whitespace-nowrap px-5 py-3 text-right tabular-nums text-gray-700 dark:text-gray-300">
                        {number(row.interactionCount)}
                      </td>
                      <Percentile value={row.p50} />
                      <Percentile value={row.p95} />
                      <Percentile value={row.min} />
                      <Percentile value={row.max} />
                      <td className="px-5 py-3 text-xs text-gray-500 dark:text-gray-400">
                        {row.excluded}
                        {row.insufficient && (
                          <span className="mt-1 block text-amber-700 dark:text-amber-400">
                            {sampleCaution(row.eligibleCount)}
                          </span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ))
      )}
    </section>
  );
}

function Percentile({ value }: { value: { display: string; exact: string | null } }) {
  return (
    <td
      className="whitespace-nowrap px-5 py-3 text-right tabular-nums text-gray-700 dark:text-gray-300"
      title={value.exact ?? undefined}
    >
      {value.display}
    </td>
  );
}

function summaryLine(response: MeasurementSummaryResponse): string {
  const totals = summaryTotals(response);
  return (
    `${number(totals.samples)} stored sample(s) across ${number(response.groups.length)} group(s), ` +
    `${number(totals.eligible)} eligible for the percentiles; ` +
    `${number(totals.incomplete)} with collection incomplete and ${number(totals.failed)} not ` +
    'successful were kept out of them but are counted here. A sample can be both, so those two ' +
    'numbers may count one sample twice and are not a partition of the stored rows. Interactions ' +
    'are not additive across groups, so the largest group ' +
    `reports ${number(totals.interactions)} distinct rather than a total this page cannot compute.`
  );
}

// ---------------------------------------------------------------------------
// Shells
// ---------------------------------------------------------------------------

function RowsSkeleton({ rows }: { rows: number }) {
  return (
    <div className="space-y-2 px-5 py-4" aria-hidden>
      {Array.from({ length: rows }, (_, index) => (
        <div key={index} className="h-8 animate-pulse rounded-lg bg-gray-100 dark:bg-white/5" />
      ))}
    </div>
  );
}

function MeasurementsSkeleton() {
  return (
    <>
      <div className={`${CARD} mt-6 h-32 animate-pulse`} />
      <div className={`${CARD} mt-5 h-48 animate-pulse`} />
      <div className={`${CARD} mt-5 h-64 animate-pulse`} />
    </>
  );
}

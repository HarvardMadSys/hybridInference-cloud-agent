'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

import {
  getMeasurement,
  listMeasurements,
  type MeasurementDetail,
  type MeasurementSummary,
} from '@/lib/api/adminMeasurements';
import { getErrorMessage } from '@/lib/utils/errors';

import {
  EMPTY_SELECTION,
  coverageLine,
  detailHeading,
  duration,
  outcomeLabel,
  partialReasons,
  selectionHref,
  stageRows,
  MEASUREMENTS_PATH,
} from '../measurements/present';
import { accessDenied, BUTTON, CARD, ErrorNotice } from './shared';

/** How many stored startup records one attempt may show before the page stops. */
const ATTEMPT_PAGE_LIMIT = 5;

/**
 * The startup measurement of one execution, read by job and attempt.
 *
 * This panel exists to answer one question an operator asks of a specific
 * attempt -- how long did this execution's startup take, and what did it wait
 * on -- and to refuse the answers the data cannot give. A missing sample is
 * *not* a fast startup: collection may have been off, this attempt may not have
 * been selected by the experiment, the record may have been dropped in
 * delivery, or the runner may be an older build. The panel names those causes
 * instead of rendering a zero.
 *
 * It reads through the same Admin API and the same presentation helpers the
 * Measurements page uses, so a number here and the same number there cannot
 * disagree about what was measured or how complete it is.
 */
export function StartupMeasurement({
  jobId,
  attemptId,
  onAccessDenied,
}: {
  jobId: string;
  attemptId: number;
  onAccessDenied: () => void;
}) {
  const [rows, setRows] = useState<MeasurementSummary[] | null>(null);
  const [emptyReason, setEmptyReason] = useState<string | null>(null);
  const [detail, setDetail] = useState<MeasurementDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setRows(null);
    setDetail(null);
    setError(null);
    setLoading(true);
    listMeasurements(
      {
        job_id: jobId,
        // The precise key: an attempt names one execution, and only a startup
        // record carries one. A file or terminal request has none, so this
        // cannot pick one of those up by accident.
        attempt_id: attemptId,
        operation: ['attempt.startup'],
        limit: ATTEMPT_PAGE_LIMIT,
      },
      controller.signal,
    )
      .then(async (page) => {
        if (controller.signal.aborted) return;
        setRows(page.measurements);
        setEmptyReason(page.empty_reason ?? null);
        const newest = page.measurements[0];
        if (!newest) return;
        // One extra read for the newest record's stages. A failed detail read
        // leaves the summary row on screen rather than replacing it with an
        // error, because the list is already a true answer to the question that
        // was asked -- except when the failure is the session being refused.
        // Swallowing that one would leave revoked Admin data on screen and tell
        // the page above nothing, so a refusal is handed straight back.
        const full = await getMeasurement(newest.measurement_id, controller.signal).catch(
          (e: unknown) => {
            if (accessDenied(e)) throw e;
            return null;
          },
        );
        if (!controller.signal.aborted) setDetail(full);
      })
      .catch((e: unknown) => {
        if (controller.signal.aborted) return;
        if (accessDenied(e)) onAccessDenied();
        else setError(getErrorMessage(e));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [jobId, attemptId, revision, onAccessDenied]);

  const filterHref = selectionHref(MEASUREMENTS_PATH, {
    ...EMPTY_SELECTION,
    job_id: jobId,
    attempt_id: String(attemptId),
  });

  return (
    <section aria-labelledby="startup-measurement-heading" className="mt-8">
      <div className="flex items-center justify-between gap-4">
        <h3
          id="startup-measurement-heading"
          className="text-sm font-semibold text-gray-900 dark:text-gray-50"
        >
          Startup measurement
        </h3>
        <button
          className={BUTTON}
          disabled={loading}
          type="button"
          onClick={() => setRevision((value) => value + 1)}
        >
          Refresh
        </button>
      </div>
      <p className="mt-1 max-w-3xl text-xs text-gray-500 dark:text-gray-400">
        Opt-in profiling of this attempt&rsquo;s startup, read by job and attempt from the same
        stored records the Measurements page lists. Runner-rooted: it covers the claim through the
        supervisor dispatching the prompt. Control-plane queueing, first model output and anything
        the browser waits for are outside it.
      </p>

      <div className={`${CARD} mt-4 overflow-hidden`}>
        {loading && rows === null && (
          <p role="status" className="px-5 py-4 text-sm text-gray-500 dark:text-gray-400">
            Loading startup measurement…
          </p>
        )}
        {error && (
          <div className="px-5 py-4">
            <ErrorNotice>{error}</ErrorNotice>
          </div>
        )}
        {rows !== null && rows.length === 0 && !error && (
          <div className="px-5 py-4">
            <p className="text-sm text-gray-700 dark:text-gray-200">
              No saved startup measurement for this attempt.
            </p>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              {emptyReason ?? 'An empty page is not zero latency.'} A missing sample is{' '}
              <strong>not</strong> a fast startup and not a failed one: it says nothing was stored,
              and the reason is not in this response. See the{' '}
              <Link className="underline" href={filterHref}>
                same selection on the Measurements page
              </Link>{' '}
              to check the experiment and the writer counters.
            </p>
          </div>
        )}
        {rows !== null && rows.length > 0 && (
          <div className="px-5 py-4">
            <dl className="flex flex-wrap gap-x-8 gap-y-4 text-sm">
              {[
                ['Duration', duration(rows[0].duration_us).display],
                ['Outcome', outcomeLabel(rows[0].outcome)],
                [
                  'Startup path',
                  // Four paths are declared; a record that cannot name one says
                  // so rather than being folded into the nearest match.
                  rows[0].startup_path
                    ? rows[0].startup_path === 'unknown'
                      ? 'Unclassified'
                      : rows[0].startup_path
                    : 'Not reported',
                ],
                ['Runtime', rows[0].runtime_id ?? 'Not reported'],
                ['Recorded', new Date(rows[0].observed_at).toLocaleString()],
              ].map(([label, value]) => (
                <div key={label} className="min-w-0">
                  <dt className="text-xs text-gray-500 dark:text-gray-400">{label}</dt>
                  <dd className="mt-1 break-words text-gray-800 dark:text-gray-200">{value}</dd>
                </div>
              ))}
            </dl>
            <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
              {coverageLine(rows[0].coverage_scope)}
            </p>
            {rows.length > 1 && (
              <p className="mt-2 text-xs text-amber-700 dark:text-amber-400">
                {rows.length} startup records are stored for this attempt. A retry is a second
                measurement, not a correction of the first, so read them separately rather than
                averaging them.
              </p>
            )}
            <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
              {rows[0].collection_complete
                ? 'Collection closed for this record.'
                : 'Collection did not close for this record, so its duration stops where collection did.'}{' '}
              End-to-end coverage is{' '}
              {rows[0].end_to_end_complete ? 'claimed' : 'not claimed (server-side only)'}.
            </p>
            <p className="mt-3 text-xs">
              <Link
                className="text-crimson hover:underline dark:text-crimson-light"
                href={filterHref}
              >
                Every stored record for this attempt
              </Link>
            </p>
          </div>
        )}
      </div>

      {detail && (
        <div className={`${CARD} mt-4 overflow-hidden`}>
          <div className="border-b border-gray-100 px-5 py-3 dark:border-white/5">
            <h4 className="text-xs font-semibold text-gray-900 dark:text-gray-50">
              {detailHeading(detail)} · {detail.measurement_id}
            </h4>
          </div>
          {partialReasons(detail.record).length > 0 && (
            <ul className="list-disc space-y-1 border-b border-gray-100 px-8 py-3 text-xs text-amber-700 dark:border-white/5 dark:text-amber-400">
              {partialReasons(detail.record).map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
          {stageRows(detail.record).length > 0 ? (
            <table className="w-full text-left text-xs">
              <thead className="text-gray-500 dark:text-gray-400">
                <tr>
                  <th className="px-5 py-2 font-medium">Stage</th>
                  <th className="px-5 py-2 text-right font-medium">Duration</th>
                  <th className="px-5 py-2 font-medium">Source</th>
                </tr>
              </thead>
              <tbody>
                {stageRows(detail.record).map((row) => (
                  <tr key={row.key} className="border-t border-gray-100 dark:border-white/5">
                    <td
                      className="px-5 py-2 text-gray-800 dark:text-gray-200"
                      style={{ paddingLeft: `${20 + row.depth * 20}px` }}
                    >
                      {row.name}
                      {row.truncated && (
                        <span className="ml-2 text-amber-700 dark:text-amber-400">
                          observed up to the collection deadline
                        </span>
                      )}
                    </td>
                    <td className="whitespace-nowrap px-5 py-2 text-right tabular-nums text-gray-700 dark:text-gray-300">
                      {duration(row.durationUs).display}
                    </td>
                    <td className="px-5 py-2 text-gray-500 dark:text-gray-400">
                      {row.source ?? 'this producer'}
                      {row.callId ? ` · ${row.callId}` : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="px-5 py-4 text-xs text-gray-500 dark:text-gray-400">
              No stage was recorded for this measurement, so it has a total and no breakdown. That
              is different from a startup that took no time.
            </p>
          )}
        </div>
      )}
    </section>
  );
}

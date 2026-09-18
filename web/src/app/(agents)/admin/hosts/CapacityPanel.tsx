'use client';

// The expanded capacity panel for one server.
//
// Pure props: the page owns the fetching, the range and the abort logic, and
// this component only draws what it is handed. That split is what makes the
// four states the panel has to distinguish testable one at a time — a skeleton
// while history loads, a pool that measured nothing, a curve that is still
// filling, and a curve whose newest point is stale.
//
// It draws no container of its own. The open row is already a tinted band
// inside the table's card, and a bordered, tinted box on that band was a third
// rounded rectangle in the same square inch: the panel reads as a detail of the
// row above it only if it is laid out like one. Its left edge is the table's
// own first-column padding, so "Capacity · box-a" sits directly under "box-a".

import type { AgentHostCapacityHistory, CapacityRange } from '@/lib/api/admin';

import CapacityChart from './CapacityChart';
import {
  CAPACITY_RANGES,
  type SandboxSummary,
  formatUtcTimestamp,
  rangeLabel,
  reasonLabel,
} from './capacity';

export interface CapacityPanelProps {
  id: string;
  /** Physical server name, as the row shows it. */
  name: string;
  /** The enrollment whose history this is, or null when it cannot be read. */
  source: string | null;
  /**
   * Every enrollment behind this physical name. One row can be several
   * deployments sharing a machine, and each keeps its own curve: the two are
   * never added and never spliced, so the operator picks which one to read.
   */
  sources: string[];
  onSourceChange: (host: string) => void;
  sandboxes: SandboxSummary;
  stale: boolean;
  range: CapacityRange;
  onRangeChange: (range: CapacityRange) => void;
  history: AgentHostCapacityHistory | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  /** True when the enrollment rotated while this panel was open. */
  restarted: boolean;
}

/**
 * One labelled fact in the panel's meta line.
 *
 * The whole fact is one text node, and the colon is part of it. Rendering the
 * label and its value as two boxes with a gap between them looks the same and
 * is not: a visual gap is not a text gap, so the two halves are concatenated
 * into one word for a screen reader and for anything matching on text —
 * "Curvebox-a-kata". The emphasis the two-tone version bought is not worth a
 * fact that cannot be read back.
 */
export default function CapacityPanel({
  id,
  name,
  source,
  sources,
  onSourceChange,
  sandboxes,
  stale,
  range,
  onRangeChange,
  history,
  loading,
  error,
  onRetry,
  restarted,
}: CapacityPanelProps) {
  const points = history?.points ?? [];
  // Reasons a source already carries on its own line are not repeated here;
  // what is left is what the panel itself concluded — two sources that cannot
  // be proven to be one Docker host, a duplicated sandbox id.
  const namedBySource = new Set<string>();
  for (const line of sandboxes.perSource) {
    if (line.reason !== null) namedBySource.add(line.reason);
  }
  const panelReasons = sandboxes.reasons.filter((reason) => !namedBySource.has(reason));
  // A complete count from one source is already visible in the table row.
  // Keep this section for provenance that changes how the total is read:
  // several sources, incomplete coverage, stale data, or an explanation.
  const showSandboxDiagnostics =
    sandboxes.perSource.length > 1 ||
    sandboxes.partial ||
    sandboxes.stale ||
    sandboxes.explanation !== null ||
    sandboxes.reasons.length > 0;
  const switcherId = `${id}-source`;

  return (
    <div id={id}>
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-50">
            Capacity · {name}
          </h3>
          {/* Which enrollment the curve belongs to — **only when there is a
              choice.** A physical row can be several enrollments, and a curve
              silently mixing them would be a different machine's load under
              this machine's name, so with more than one the operator picks and
              the choice is never carried across servers or merged.

              With one, the enrollment is not a choice and its name is not
              information: the heading above already names the server, and the
              operator did not come here to read an identity that only differs
              from it by a suffix. What is left is the case where no enrollment
              reports anything at all, which is the one fact the empty charts
              below cannot state for themselves. */}
          <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px]">
            {sources.length > 1 ? (
              <span className="inline-flex items-center gap-1.5">
                <label htmlFor={switcherId} className="text-gray-500 dark:text-gray-400">
                  Curve source
                </label>
                <select
                  id={switcherId}
                  value={source ?? ''}
                  onChange={(event) => onSourceChange(event.target.value)}
                  className="rounded-md border border-gray-300 bg-white px-1.5 py-0.5 text-[11px] font-medium text-gray-700 dark:border-white/15 dark:bg-gray-900 dark:text-gray-200"
                >
                  {sources.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </span>
            ) : source === null ? (
              <span className="text-gray-500 dark:text-gray-400">
                No enrollment reports capacity for this server.
              </span>
            ) : null}
          </div>
        </div>

        {/* One control, one selected item. Crimson is the colour of the data on
            this page; the loudest fill on the screen was a time-range button,
            which is a control, not a reading. */}
        <div
          role="group"
          aria-label="Time range"
          className="flex shrink-0 items-center gap-0.5 rounded-lg bg-gray-200/60 p-0.5 dark:bg-white/10"
        >
          {CAPACITY_RANGES.map((option) => (
            <button
              key={option}
              type="button"
              aria-pressed={range === option}
              onClick={() => onRangeChange(option)}
              className={`rounded-md px-2.5 py-1 text-xs transition-colors ${
                range === option
                  ? 'bg-white font-semibold text-gray-900 shadow-sm dark:bg-gray-900 dark:text-gray-50'
                  : 'font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
              }`}
            >
              {rangeLabel(option)}
            </button>
          ))}
        </div>
      </div>

      {stale && (
        <p className="mt-3 text-xs text-amber-700 dark:text-amber-300">
          The last observation is stale. Its last values remain visible, and the curve is not
          extended to now.
        </p>
      )}
      {restarted && (
        <p className="mt-2 text-xs text-amber-700 dark:text-amber-300">
          This host re-enrolled, so its history restarts from the new enrollment.
        </p>
      )}

      {error !== null && (
        <p role="alert" className="mt-3 text-xs text-red-700 dark:text-red-400">
          History could not be read: {error}
          <button type="button" onClick={onRetry} className="ml-2 underline">
            Retry
          </button>
        </p>
      )}

      {source === null ? null : loading && history === null ? (
        // One or two lines, in the shape of the charts that are coming: the
        // table above it keeps its numbers while this loads.
        <>
          <div
            className="mt-4 h-32 animate-pulse rounded-lg bg-gray-200/60 dark:bg-white/5"
            aria-hidden
          />
          <p className="mt-2 text-[11px] text-gray-500 dark:text-gray-400" aria-live="polite">
            Loading history…
          </p>
        </>
      ) : history === null ? (
        // Nothing to draw. When the read failed, the error above already says
        // so and this would only be a second, vaguer telling of it.
        error === null ? (
          <p className="mt-4 text-xs text-gray-500 dark:text-gray-400">
            No history is available for this server yet.
          </p>
        ) : null
      ) : (
        <>
          <div className="mt-4 grid gap-x-8 gap-y-5 lg:grid-cols-2">
            <CapacityChart
              metric="cpu"
              title="CPU usage (%)"
              points={points}
              since={history.since}
              until={history.until}
              resolutionSeconds={history.resolution_seconds}
              range={range}
              stale={stale}
            />
            <CapacityChart
              metric="memory"
              title="Memory usage (%)"
              points={points}
              since={history.since}
              until={history.until}
              resolutionSeconds={history.resolution_seconds}
              range={range}
              stale={stale}
            />
          </div>
          {loading && (
            <p className="mt-2 text-[11px] text-gray-500 dark:text-gray-400">Refreshing history…</p>
          )}
        </>
      )}

      <div
        hidden={!showSandboxDiagnostics}
        className="mt-5 border-t border-gray-200 pt-4 dark:border-white/10"
      >
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <h4 className="text-xs font-semibold text-gray-900 dark:text-gray-100">
            Retained sandboxes
          </h4>
          {/* Next to the heading, not at the far end of the row: the total is
              what the heading is about, and a figure 900 pixels away reads as
              the next column's. Its own element, so the sentence it holds
              stays one node — the detail separator lives in the layout gap. */}
          <p className="text-xs text-gray-700 dark:text-gray-300">
            {sandboxes.text}
            {sandboxes.partial && !sandboxes.text.includes('Partial') ? ' · Partial' : ''}
          </p>
        </div>
        {sandboxes.explanation !== null && (
          <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
            {sandboxes.explanation}
          </p>
        )}
        {panelReasons.length > 0 && (
          <ul className="mt-1.5 space-y-0.5 text-[11px] text-amber-700 dark:text-amber-300">
            {panelReasons.map((reason) => (
              <li key={reason}>{reasonLabel(reason)}</li>
            ))}
          </ul>
        )}
        {sandboxes.perSource.length > 0 && (
          // One row per enrollment, columns aligned. As a run of
          // space-separated words on one line, a namespace, a count and a
          // timestamp could not be told apart, and two enrollments read as one
          // sentence.
          <ul className="mt-2 divide-y divide-gray-200/70 rounded-lg border border-gray-200 bg-white/70 text-[11px] dark:divide-white/5 dark:border-white/10 dark:bg-white/[0.03]">
            {sandboxes.perSource.map((line) => (
              <li
                key={line.host}
                className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-3 py-2"
              >
                <span className="font-medium text-gray-800 dark:text-gray-200">{line.host}</span>
                <span className="truncate font-mono text-gray-500 dark:text-gray-400">
                  {line.namespace ?? 'no namespace'}
                </span>
                <span className="ml-auto tabular-nums text-gray-700 dark:text-gray-300">
                  {line.counts}
                </span>
                <span className="w-36 text-right tabular-nums text-gray-500 dark:text-gray-400">
                  {line.sampledAt === null ? '' : formatUtcTimestamp(line.sampledAt)}
                </span>
                {line.status !== 'available' && (
                  <span className="rounded bg-amber-50 px-1.5 py-0.5 font-medium text-amber-700 ring-1 ring-inset ring-amber-600/20 dark:bg-amber-500/10 dark:text-amber-300 dark:ring-amber-500/25">
                    {line.status}
                  </span>
                )}
                {line.reason !== null && (
                  <span className="w-full text-amber-700 dark:text-amber-300">
                    {reasonLabel(line.reason)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

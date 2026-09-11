'use client';

// The expanded capacity panel for one server.
//
// Pure props: the page owns the fetching, the range and the abort logic, and
// this component only draws what it is handed. That split is what makes the
// four states the panel has to distinguish testable one at a time — a skeleton
// while history loads, a pool that measured nothing, a curve that is still
// filling, and a curve whose newest point is stale.

import type { AgentHostCapacityHistory, CapacityRange } from '@/lib/api/admin';

import CapacityChart from './CapacityChart';
import {
  CAPACITY_RANGES,
  type SandboxSummary,
  formatUtcTimestamp,
  rangeLabel,
  reasonLabel,
} from './capacity';
import { formatAge } from './format';

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
  /** Age of the oldest contributing observation, advanced locally. */
  oldestAgeSeconds: number | null;
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

export default function CapacityPanel({
  id,
  name,
  source,
  sources,
  onSourceChange,
  sandboxes,
  oldestAgeSeconds,
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
  const switcherId = `${id}-source`;

  return (
    <div
      id={id}
      className="rounded-xl border border-gray-200 bg-gray-50/60 px-5 py-4 dark:border-white/10 dark:bg-white/[0.02]"
    >
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h3 className="text-sm font-medium text-gray-800 dark:text-gray-100">
            Capacity · {name}
          </h3>
          {/* Which enrollment the curve belongs to. A physical row can be
              several enrollments, and a curve silently mixing them would be a
              different machine's load under this machine's name. With more
              than one, the operator picks; the choice is never carried across
              servers, and never merged. */}
          {sources.length > 1 ? (
            <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-gray-500 dark:text-gray-400">
              <label htmlFor={switcherId}>Curve source</label>
              <select
                id={switcherId}
                value={source ?? ''}
                onChange={(event) => onSourceChange(event.target.value)}
                className="rounded-md border border-gray-200 bg-white px-1.5 py-0.5 text-[11px] text-gray-700 dark:border-white/10 dark:bg-gray-900 dark:text-gray-200"
              >
                {sources.map((option) => (
                  <option key={option} value={option}>
                    {option}
                  </option>
                ))}
              </select>
            </p>
          ) : (
            <p className="mt-0.5 text-[11px] text-gray-500 dark:text-gray-400">
              {source === null
                ? 'No enrollment reports capacity for this server.'
                : `Curve: ${source}`}
            </p>
          )}
          <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
            {oldestAgeSeconds === null
              ? 'No observation has been recorded for this server yet.'
              : `Oldest contributing observation: ${formatAge(oldestAgeSeconds)}`}
            {history === null
              ? ''
              : ` · one observation per minute, ${history.resolution_seconds}s buckets`}
          </p>
        </div>

        <div role="group" aria-label="Time range" className="flex shrink-0 items-center gap-1">
          {CAPACITY_RANGES.map((option) => (
            <button
              key={option}
              type="button"
              aria-pressed={range === option}
              onClick={() => onRangeChange(option)}
              className={`rounded-lg px-2.5 py-1 text-xs font-medium ${
                range === option
                  ? 'bg-crimson text-white dark:bg-crimson-light'
                  : 'border border-gray-200 text-gray-700 hover:bg-gray-50 dark:border-white/10 dark:text-gray-200 dark:hover:bg-white/5'
              }`}
            >
              {rangeLabel(option)}
            </button>
          ))}
        </div>
      </div>

      {stale && (
        <p className="mt-3 text-xs text-amber-700 dark:text-amber-300">
          The last observation is stale. Its value and age are still shown, and the curve is not
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
            className="mt-4 h-32 animate-pulse rounded-lg bg-gray-100 dark:bg-white/5"
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
          <div className="mt-4 grid gap-6 lg:grid-cols-2">
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
          <p className="mt-2 text-[11px] text-gray-500 dark:text-gray-400">
            Each point is one observation: CPU averages the interval before it, memory is
            instantaneous. Read {formatUtcTimestamp(history.observed_at)}.
          </p>
        </>
      )}

      <div className="mt-5 border-t border-gray-200 pt-3 dark:border-white/10">
        <h4 className="text-xs font-medium text-gray-700 dark:text-gray-200">Retained sandboxes</h4>
        <p className="mt-1 text-xs text-gray-600 dark:text-gray-300">
          {sandboxes.text}
          {sandboxes.partial && !sandboxes.text.includes('Partial') ? ' · Partial' : ''}
        </p>
        {sandboxes.explanation !== null && (
          <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
            {sandboxes.explanation}
          </p>
        )}
        {panelReasons.length > 0 && (
          <ul className="mt-1 list-inside list-disc text-[11px] text-gray-500 dark:text-gray-400">
            {panelReasons.map((reason) => (
              <li key={reason}>{reasonLabel(reason)}</li>
            ))}
          </ul>
        )}
        {sandboxes.perSource.length > 0 && (
          <ul className="mt-2 space-y-1 text-[11px] text-gray-500 dark:text-gray-400">
            {sandboxes.perSource.map((line) => (
              <li key={line.host} className="flex flex-wrap gap-x-2">
                <span className="font-medium text-gray-700 dark:text-gray-300">{line.host}</span>
                <span>{line.namespace ?? 'no namespace'}</span>
                <span>{line.counts}</span>
                <span>{line.sampledAt === null ? '' : formatUtcTimestamp(line.sampledAt)}</span>
                {line.status !== 'available' && <span>{line.status}</span>}
                {line.reason !== null && <span>{reasonLabel(line.reason)}</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

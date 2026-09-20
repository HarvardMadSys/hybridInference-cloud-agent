'use client';

import { useEffect, useState } from 'react';

import { formatBytes, formatCpu, formatDuration } from '@/components/features/agents/TurnFooter';
import type {
  AgentAttemptTelemetryApi,
  AttemptResourcesApi,
  RunResourceSummaryApi,
} from '@/lib/api/agents';
import { getAdminAttemptResources } from '@/lib/api/adminRuns';
import { getErrorMessage } from '@/lib/utils/errors';

import { turnSummaryReadings, turnSummaryRows, type ResourceReading } from './presentTurnResources';
import { accessDenied, BUTTON, CARD, ErrorNotice } from './shared';

function metric(value: string | null, partial = false): string {
  return value === null ? 'Unavailable' : `${partial ? '≥ ' : ''}${value}`;
}

export function Resources({
  jobId,
  attemptId,
  attemptCoverage = null,
  onAccessDenied,
}: {
  jobId: string;
  attemptId: number;
  /** The run's compact resource projection, or null when no attempt has one. */
  attemptCoverage?: RunResourceSummaryApi | null;
  onAccessDenied: () => void;
}) {
  const [telemetry, setTelemetry] = useState<AgentAttemptTelemetryApi | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setTelemetry(null);
    setLoading(true);
    setError(null);
    getAdminAttemptResources(jobId, attemptId, controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setTelemetry(next);
      })
      .catch((e: unknown) => {
        if (!controller.signal.aborted) {
          if (accessDenied(e)) onAccessDenied();
          else setError(getErrorMessage(e));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [jobId, attemptId, revision, onAccessDenied]);

  return (
    <section aria-labelledby="resources-heading" className="mt-8">
      <div className="flex items-center justify-between gap-4">
        <h2
          id="resources-heading"
          className="text-lg font-semibold text-gray-900 dark:text-gray-50"
        >
          Tools and resources
        </h2>
        <button
          type="button"
          className={BUTTON}
          disabled={loading}
          onClick={() => setRevision((value) => value + 1)}
        >
          Refresh resources
        </button>
      </div>
      {error && <ErrorNotice>{error}</ErrorNotice>}
      {loading ? (
        <p role="status" className="mt-4 text-sm text-gray-500 dark:text-gray-400">
          Loading resources…
        </p>
      ) : (
        telemetry &&
        (telemetry.resources ? (
          <>
            <TurnResourceSummary telemetry={telemetry} attemptCoverage={attemptCoverage} />
            <ResourceReadings resources={telemetry.resources} />
          </>
        ) : (
          <p className={`${CARD} mt-4 p-5 text-sm text-gray-500 dark:text-gray-400`}>
            Resource measurements are unavailable for this execution.
          </p>
        ))
      )}
    </section>
  );
}

/**
 * The whole-turn member execution window for one attempt.
 *
 * This is the member's own execution inside a shared sandbox, not the
 * platform's cost of the task, so the scope sentence is part of the panel
 * rather than a footnote to it. Every metric carries its own status and its
 * own reasons: an I/O metric no sandbox can produce must not read as if the
 * CPU number were short too. A summary with no final report never renders as a
 * finalized total.
 */
function TurnResourceSummary({
  telemetry,
  attemptCoverage,
}: {
  telemetry: AgentAttemptTelemetryApi;
  attemptCoverage: RunResourceSummaryApi | null;
}) {
  const summary = telemetry.resources?.turn_summary ?? null;
  if (!summary) {
    return (
      <div className={`${CARD} mt-4 p-5 text-sm text-gray-500 dark:text-gray-400`}>
        <h3 className="text-sm font-medium text-gray-900 dark:text-gray-50">
          Turn resource summary
        </h3>
        <p className="mt-2">
          Not collected for this execution. A turn recorded before whole-turn accounting existed, or
          one whose sandbox could not report it, has no member execution window here. This is not a
          measured zero.
        </p>
      </div>
    );
  }
  // The attempt counts are a run-level fact; the attempt endpoint does not
  // carry them, so they are merged from the run's compact projection.
  const rows = turnSummaryRows({
    ...summary,
    attempts_measured: attemptCoverage?.attempts_measured,
    attempts_total: attemptCoverage?.attempts_total,
  });
  const readings = turnSummaryReadings(summary);
  return (
    <div className={`${CARD} mt-4 overflow-hidden`}>
      <div className="border-b border-gray-100 p-5 dark:border-white/5">
        <h3 className="text-sm font-medium text-gray-900 dark:text-gray-50">
          Turn resource summary
        </h3>
        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
          One member execution window inside the shared sandbox. This is not the platform cost of
          the task.
        </p>
        <dl className="mt-4 divide-y divide-gray-100 text-xs dark:divide-white/5">
          {rows.map((row) => (
            <div key={row.label} className="grid gap-1 py-3 sm:grid-cols-[10rem_1fr]">
              <dt className="text-gray-500 dark:text-gray-400">{row.label}</dt>
              <dd className="tabular-nums text-gray-800 dark:text-gray-200">
                <span className="block">{row.value}</span>
                {row.detail && (
                  <span className="mt-1 block text-gray-500 dark:text-gray-400">{row.detail}</span>
                )}
              </dd>
            </div>
          ))}
        </dl>
      </div>
      <details className="p-5">
        <summary className="cursor-pointer text-sm font-medium">Method and reasons</summary>
        <dl className="mt-4 space-y-3 text-xs">
          {readings.map((reading, index) => (
            <div key={rows[index].label}>
              <dt className="text-gray-500 dark:text-gray-400">{rows[index].label}</dt>
              <dd className="mt-1 text-gray-800 dark:text-gray-200">
                <MethodLine reading={reading} />
              </dd>
            </div>
          ))}
        </dl>
      </details>
    </div>
  );
}

/** The method that produced one reading, with the full reasons behind it. */
function MethodLine({ reading }: { reading: ResourceReading }) {
  return (
    <>
      <span className="block font-mono text-[11px]">{reading.method ?? 'Method not reported'}</span>
      {reading.notes.map((note) => (
        <span key={note} className="mt-1 block text-gray-500 dark:text-gray-400">
          {note}
        </span>
      ))}
    </>
  );
}

function ResourceReadings({ resources }: { resources: AttemptResourcesApi }) {
  const tools = resources.tool_calls ?? [];
  const processes = resources.processes ?? [];
  const observerPartial =
    Boolean(resources.process_observer) &&
    (!resources.process_observation_complete ||
      Boolean(resources.processes_truncated) ||
      (resources.process_events_lost ?? 0) > 0);
  const sampled = resources.coverage > 0 && resources.sample_count > 0;
  const sampleIncomplete = resources.coverage < 0.9;

  return (
    <div className={`${CARD} mt-4 overflow-hidden`}>
      <div className="space-y-2 border-b border-gray-100 p-5 text-xs text-gray-500 dark:border-white/5 dark:text-gray-400">
        {resources.process_observer ? (
          <p>
            Process observer: {resources.process_observer} ·{' '}
            {observerPartial ? 'Incomplete' : 'Complete'}
            {(resources.process_events_lost ?? 0) > 0
              ? ` · ${resources.process_events_lost} events lost`
              : ''}
          </p>
        ) : (
          <p>Process observation unavailable.</p>
        )}
        {observerPartial && (
          <p className="text-amber-700 dark:text-amber-400">
            Some processes were not fully observed. Attributed resource values may be lower bounds.
          </p>
        )}
        <p>
          Tool CPU and I/O cover attributable exited processes. Memory is the largest single process
          peak, not the tool’s combined peak.
        </p>
      </div>
      {tools.length ? (
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-xs">
            <thead className="text-gray-500 dark:text-gray-400">
              <tr>
                {[
                  'Tool',
                  'Duration',
                  'Timing',
                  'Process CPU',
                  'Largest process peak',
                  'Read',
                  'Write',
                ].map((label) => (
                  <th key={label} className="whitespace-nowrap px-4 py-3 font-medium">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-white/5">
              {tools.map((tool) => {
                const partial = observerPartial || tool.process_resources_partial;
                return (
                  <tr key={tool.call_seq} className="align-top">
                    <th scope="row" className="max-w-52 px-4 py-3 text-left font-normal">
                      <span className="break-all font-medium">{tool.tool_name || 'Tool'}</span>
                      <p className="mt-1 text-gray-500 dark:text-gray-400">
                        Call {tool.call_seq}
                        {tool.process_resources_partial ? ' · Incomplete' : ''}
                      </p>
                      <p className="mt-1 break-all font-mono text-[10px] text-gray-400 dark:text-gray-500">
                        {tool.tool_use_id}
                      </p>
                    </th>
                    <td className="whitespace-nowrap px-4 py-3 tabular-nums">
                      {metric(formatDuration(tool.duration_ms))}
                    </td>
                    <td className="px-4 py-3">
                      {tool.timing_source === 'exact'
                        ? 'Exact · hooks'
                        : tool.timing_source === 'message'
                          ? 'Approximate · messages'
                          : 'Unavailable'}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 tabular-nums">
                      {metric(formatCpu(tool.process_cpu_usec), partial)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 tabular-nums">
                      {metric(formatBytes(tool.process_mem_peak_bytes), partial)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 tabular-nums">
                      {metric(formatBytes(tool.process_io_read_bytes), partial)}
                    </td>
                    <td className="whitespace-nowrap px-4 py-3 tabular-nums">
                      {metric(formatBytes(tool.process_io_write_bytes), partial)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="p-5 text-sm text-gray-500 dark:text-gray-400">
          No tool resource measurements recorded.
        </p>
      )}
      {resources.tool_calls_truncated && (
        <p className="px-5 pb-4 text-xs text-amber-700 dark:text-amber-400">
          Tool records are truncated; this is not a complete list.
        </p>
      )}
      <details className="border-t border-gray-100 p-5 dark:border-white/5">
        <summary className="cursor-pointer text-sm font-medium">
          Processes ({processes.length} recorded)
        </summary>
        {processes.length ? (
          <div className="mt-4 overflow-x-auto">
            <table className="min-w-full text-left text-xs">
              <thead className="text-gray-500 dark:text-gray-400">
                <tr>
                  {[
                    'Process',
                    'PID',
                    'Tool calls',
                    'CPU',
                    'Peak memory',
                    'Read / write',
                    'Exit',
                  ].map((label) => (
                    <th key={label} className="whitespace-nowrap px-3 py-2 font-medium">
                      {label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-white/5">
                {processes.map((process) => (
                  <tr key={process.process_seq}>
                    <th scope="row" className="max-w-48 break-all px-3 py-2 text-left font-normal">
                      {process.comm || 'Unknown'}
                    </th>
                    <td className="px-3 py-2 tabular-nums">{process.pid}</td>
                    <td className="px-3 py-2">
                      {process.tool_call_seqs.join(', ') || 'Unattributed'}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2">
                      {metric(formatCpu(process.cpu_usec))}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2">
                      {metric(formatBytes(process.mem_peak_bytes))}
                    </td>
                    <td className="whitespace-nowrap px-3 py-2">
                      {metric(formatBytes(process.io_read_bytes))} /{' '}
                      {metric(formatBytes(process.io_write_bytes))}
                    </td>
                    <td className="px-3 py-2">
                      {process.observed_exit
                        ? (process.exit_code ?? 'Unavailable')
                        : 'Not observed'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
            No process records available.
          </p>
        )}
        {resources.processes_truncated && (
          <p className="mt-3 text-xs text-amber-700 dark:text-amber-400">
            Process records are truncated.
          </p>
        )}
      </details>
      <div className="border-t border-gray-100 p-5 dark:border-white/5">
        <h3 className="text-sm font-medium">Whole-execution samples</h3>
        {sampled ? (
          <>
            <p
              className={`mt-2 text-xs ${sampleIncomplete ? 'text-amber-700 dark:text-amber-400' : 'text-gray-500 dark:text-gray-400'}`}
            >
              {Math.round(resources.coverage * 100)}% of the execution window sampled ·{' '}
              {resources.sample_count} samples{sampleIncomplete ? ' · Incomplete' : ''}
            </p>
            <dl className="mt-4 grid grid-cols-2 gap-4 text-xs sm:grid-cols-4">
              {[
                ['CPU', metric(formatCpu(resources.cpu_usec), sampleIncomplete)],
                ['Peak memory', metric(formatBytes(resources.mem_peak_bytes), sampleIncomplete)],
                ['Read', metric(formatBytes(resources.io_read_bytes), sampleIncomplete)],
                ['Write', metric(formatBytes(resources.io_write_bytes), sampleIncomplete)],
              ].map(([label, value]) => (
                <div key={label}>
                  <dt className="text-gray-500 dark:text-gray-400">{label}</dt>
                  <dd className="mt-1 tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
          </>
        ) : (
          <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
            Unavailable — whole-sandbox totals were not collected for this execution. Tool and
            process values cannot substitute for them.
          </p>
        )}
      </div>
    </div>
  );
}

'use client';

import { useEffect, useState } from 'react';

import { formatBytes, formatCpu, formatDuration } from '@/components/features/agents/TurnFooter';
import type { AgentAttemptTelemetryApi, AttemptResourcesApi } from '@/lib/api/agents';
import { getAdminAttemptResources } from '@/lib/api/adminRuns';
import { getErrorMessage } from '@/lib/utils/errors';

import { turnSummaryRows } from './presentTurnResources';
import { accessDenied, BUTTON, CARD, ErrorNotice } from './shared';

function metric(value: string | null, partial = false): string {
  return value === null ? 'Unavailable' : `${partial ? '≥ ' : ''}${value}`;
}

export function Resources({
  jobId,
  attemptId,
  onAccessDenied,
}: {
  jobId: string;
  attemptId: number;
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
      <h2 id="resources-heading" className="text-lg font-semibold text-gray-900 dark:text-gray-50">
        Tools and resources
      </h2>
      {error && (
        <div className="mt-4 flex items-center gap-3">
          <ErrorNotice>{error}</ErrorNotice>
          <button
            type="button"
            className={BUTTON}
            onClick={() => setRevision((value) => value + 1)}
          >
            Retry
          </button>
        </div>
      )}
      {loading ? (
        <p role="status" className="mt-4 text-sm text-gray-500 dark:text-gray-400">
          Loading resources…
        </p>
      ) : (
        telemetry &&
        (telemetry.resources ? (
          <>
            <TurnResourceSummary telemetry={telemetry} />
            <ResourceReadings resources={telemetry.resources} />
          </>
        ) : (
          <p className={`${CARD} mt-4 p-5 text-sm text-gray-500 dark:text-gray-400`}>
            Resource measurements are unavailable for this turn.
          </p>
        ))
      )}
    </section>
  );
}

/** The turn's measurements, with caveats attached to their values. */
function TurnResourceSummary({ telemetry }: { telemetry: AgentAttemptTelemetryApi }) {
  const summary = telemetry.resources?.turn_summary ?? null;
  if (!summary) {
    return (
      <p className={`${CARD} mt-4 p-5 text-sm text-gray-500 dark:text-gray-400`}>
        No resource measurements were recorded for this turn.
      </p>
    );
  }
  const rows = turnSummaryRows(summary);
  return (
    <div role="group" aria-label="Resource summary" className={`${CARD} mt-4 overflow-hidden`}>
      <p className="border-b border-gray-100 px-5 py-3 text-xs text-gray-500 dark:border-white/5 dark:text-gray-400">
        Agent and tools
        {summary.state === 'interrupted' ? ' · Collection interrupted' : ''}
        {summary.state === 'collecting' ? ' · Still collecting' : ''}
      </p>
      <dl className="divide-y divide-gray-100 text-sm dark:divide-white/5">
        {rows.map((row) => (
          <div key={row.label} className="flex items-baseline justify-between gap-4 px-5 py-3">
            <dt className="text-gray-500 dark:text-gray-400">{row.label}</dt>
            <dd
              className="text-right tabular-nums text-gray-900 dark:text-gray-100"
              title={row.detail ?? undefined}
              aria-label={row.detail ? `${row.value}. ${row.detail}` : undefined}
            >
              {row.value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
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
        <h3 className="text-sm font-medium">Whole-turn samples</h3>
        {sampled ? (
          <>
            <p
              className={`mt-2 text-xs ${sampleIncomplete ? 'text-amber-700 dark:text-amber-400' : 'text-gray-500 dark:text-gray-400'}`}
            >
              {Math.round(resources.coverage * 100)}% of the turn sampled · {resources.sample_count}{' '}
              samples{sampleIncomplete ? ' · Incomplete' : ''}
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
            Unavailable — whole-sandbox totals were not collected for this turn. Tool and process
            values cannot substitute for them.
          </p>
        )}
      </div>
    </div>
  );
}

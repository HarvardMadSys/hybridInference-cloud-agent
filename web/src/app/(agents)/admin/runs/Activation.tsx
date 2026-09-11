'use client';

import { useEffect, useState } from 'react';

import { formatDuration } from '@/components/features/agents/TurnFooter';
import type { AgentAttemptTelemetryApi } from '@/lib/api/agents';
import { getAdminAttemptResources } from '@/lib/api/adminRuns';
import { getErrorMessage } from '@/lib/utils/errors';

import { accessDenied, BUTTON, CARD, ErrorNotice } from './shared';

/**
 * What each stage is, in the words an operator would use for it.
 *
 * The keys are the runner's own stage vocabulary — the same words its failure
 * logs use — so a duration here and a "failed at checkout" log line name the
 * same moment. A key this build does not know is rendered verbatim rather
 * than dropped: a newer host must not be able to measure a stage the page
 * silently hides.
 */
const STAGE_LABELS: Record<string, string> = {
  reservation: 'Sandbox reservation',
  supervisor_acquire: 'Container and networks',
  sandbox_running: 'Running receipt',
  terminal_suspend: 'Terminal suspend',
  workspace_reset: 'Workspace reset',
  checkout: 'Repository import',
  setup: 'Setup script',
  workspace_permissions: 'Workspace permissions',
  native_workspace_resume: 'Session resume',
  runtime_preflight: 'Runtime preflight',
  agent_network: 'Agent network',
  terminal_resume: 'Terminal resume',
  resident_start: 'Agent CLI start',
  prompt_admission: 'Prompt admission',
};

/** One line naming the continuity rung, which is what makes a total readable. */
function rung(telemetry: AgentAttemptTelemetryApi): string {
  if (telemetry.context_status === 'new')
    return 'New chat: sandbox created and repository imported';
  if (telemetry.continuity_mode === 'hot') return 'Follow-up turn on the live Agent CLI process';
  if (telemetry.context_status === 'restored') return 'Follow-up turn: Agent CLI process restarted';
  return 'Continuity was not recorded for this execution';
}

export function Activation({
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

  const activation = telemetry?.activation ?? null;

  return (
    <section aria-labelledby="activation-heading" className="mt-8">
      <div className="flex items-center justify-between gap-4">
        <h2
          id="activation-heading"
          className="text-lg font-semibold text-gray-900 dark:text-gray-50"
        >
          Sandbox activation
        </h2>
        <button
          type="button"
          className={BUTTON}
          disabled={loading}
          onClick={() => setRevision((value) => value + 1)}
        >
          Refresh activation
        </button>
      </div>
      {error && <ErrorNotice>{error}</ErrorNotice>}
      {loading ? (
        <p role="status" className="mt-4 text-sm text-gray-500 dark:text-gray-400">
          Loading activation…
        </p>
      ) : telemetry && activation ? (
        <ActivationReadings telemetry={telemetry} activation={activation} />
      ) : telemetry ? (
        <p className={`${CARD} mt-4 p-5 text-sm text-gray-500 dark:text-gray-400`}>
          This execution has no activation measurement. It ran before the host recorded one, or the
          host never reached the report.
        </p>
      ) : null}
    </section>
  );
}

function ActivationReadings({
  telemetry,
  activation,
}: {
  telemetry: AgentAttemptTelemetryApi;
  activation: NonNullable<AgentAttemptTelemetryApi['activation']>;
}) {
  // Heaviest first: the question this panel answers is which stage to look at,
  // and the answer is the top row. Stages that did not run are absent rather
  // than zero, so this list is never padded with rows that say nothing.
  const rows = Object.entries(activation.stages).sort(([, left], [, right]) => right - left);
  const total = activation.total_ms;

  return (
    <div className={`${CARD} mt-4 overflow-hidden`}>
      <div className="space-y-2 border-b border-gray-100 p-5 text-xs text-gray-500 dark:border-white/5 dark:text-gray-400">
        <p>{rung(telemetry)}</p>
        <p>
          Measured by the host that waited, from claiming the turn until the Agent CLI accepted the
          prompt. Model time and tool time are excluded.
        </p>
        {activation.failed_stage && (
          <p className="text-amber-700 dark:text-amber-400">
            The activation failed during {label(activation.failed_stage)}, so these figures cover
            only the part that ran.
          </p>
        )}
      </div>
      <div className="flex items-baseline justify-between gap-5 border-b border-gray-100 px-5 py-3 text-sm dark:border-white/5">
        <span className="text-gray-600 dark:text-gray-400">Total activation</span>
        <span className="shrink-0 tabular-nums font-medium text-gray-900 dark:text-gray-100">
          {formatDuration(total) ?? 'Unavailable'}
        </span>
      </div>
      {rows.length ? (
        <div className="overflow-x-auto">
          <table className="min-w-full text-left text-xs">
            <thead className="text-gray-500 dark:text-gray-400">
              <tr>
                {['Stage', 'Duration', 'Share'].map((heading) => (
                  <th key={heading} className="whitespace-nowrap px-4 py-3 font-medium">
                    {heading}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 dark:divide-white/5">
              {rows.map(([stage, ms]) => (
                <tr key={stage}>
                  <td className="whitespace-nowrap px-4 py-2.5 text-gray-800 dark:text-gray-200">
                    {label(stage)}
                  </td>
                  <td className="whitespace-nowrap px-4 py-2.5 tabular-nums text-gray-600 dark:text-gray-300">
                    {formatDuration(ms) ?? 'Unavailable'}
                  </td>
                  <td className="whitespace-nowrap px-4 py-2.5 tabular-nums text-gray-500 dark:text-gray-400">
                    {total > 0 ? `${Math.round((ms / total) * 100)}%` : 'Unavailable'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="p-5 text-sm text-gray-500 dark:text-gray-400">
          The total was recorded without a per-stage breakdown.
        </p>
      )}
    </div>
  );
}

function label(stage: string): string {
  return STAGE_LABELS[stage] ?? stage;
}

import { useEffect, useMemo, useRef, useState } from 'react';

import {
  getAgentJob,
  getAgentAttemptTelemetry,
  type AgentAttemptTelemetryApi,
  type AgentJobApi,
} from '@/lib/api/agents';
import { formatDuration } from './TurnFooter';

interface AttemptTiming {
  key: string;
  turnNo: number;
  attemptNo: number | null;
  durationMs: number | null;
  toolsMs: number | null;
  otherMs: number | null;
  approximate: boolean;
}

function measured(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

/** Resource totals belong only to the attempt explicitly named by the job. */
function attemptTimings(
  jobs: AgentJobApi[],
  telemetry: Map<string, AgentAttemptTelemetryApi>,
): AttemptTiming[] {
  return jobs.flatMap((job) => {
    const attempts: Array<{
      attempt_id: number;
      attempt_no: number | null;
      duration_ms: number | null;
    }> = [...(job.attempt_telemetry ?? [])];
    const selected = job.telemetry_attempt_id;
    if (selected != null && !attempts.some((attempt) => attempt.attempt_id === selected)) {
      attempts.push({
        attempt_id: selected,
        attempt_no: null,
        duration_ms: job.duration_ms ?? null,
      });
    }
    if (attempts.length === 0 && job.state !== 'queued') {
      // A turn whose timing read failed must not disappear from coverage.
      attempts.push({ attempt_id: 0, attempt_no: null, duration_ms: null });
    }
    return attempts.map((attempt) => {
      const loaded = telemetry.get(`${job.id}:${attempt.attempt_id}`);
      const resources =
        (attempt.attempt_id === selected ? job.resources : null) ?? loaded?.resources;
      const durationMs = measured(loaded?.duration_ms ?? attempt.duration_ms);
      const toolsMs = resources?.tool_calls_truncated ? null : measured(resources?.tool_time_ms);
      return {
        key: `${job.id}:${attempt.attempt_id}`,
        turnNo: job.turn_no ?? 1,
        attemptNo: attempt.attempt_no,
        durationMs,
        toolsMs,
        approximate:
          toolsMs !== null &&
          toolsMs > 0 &&
          !(
            resources?.tool_calls?.length &&
            resources.tool_calls.every((call) => call.timing_source === 'exact')
          ),
        // Conflicting measurements cannot prove a remainder.
        otherMs:
          durationMs !== null && toolsMs !== null && toolsMs <= durationMs
            ? durationMs - toolsMs
            : null,
      };
    });
  });
}

function total(attempts: AttemptTiming[], field: 'durationMs' | 'toolsMs' | 'otherMs'): string {
  const values = attempts.flatMap((attempt) => (attempt[field] === null ? [] : [attempt[field]]));
  if (values.length === 0 || (field === 'otherMs' && values.length !== attempts.length)) return '—';
  const duration = formatDuration(values.reduce((sum, value) => sum + value, 0));
  const approximate =
    field !== 'durationMs' &&
    attempts.some((attempt) => attempt[field] !== null && attempt.approximate);
  return `${values.length === attempts.length ? '' : '≥ '}${approximate ? '≈ ' : ''}${duration}`;
}

export function TrajectoryTimeBreakdown({ jobs }: { jobs: AgentJobApi[] }) {
  const jobCache = useRef(new Map<string, AgentJobApi>());
  const telemetryCache = useRef(new Map<string, AgentAttemptTelemetryApi>());
  const [loaded, setLoaded] = useState<{
    source: AgentJobApi[];
    jobs: AgentJobApi[];
    telemetry: Map<string, AgentAttemptTelemetryApi>;
  } | null>(null);

  useEffect(() => {
    if (jobs.length === 0) return;
    const controller = new AbortController();
    async function load() {
      const detailedJobs = await Promise.all(
        jobs.map(async (job) => {
          if (job.state === 'queued' && job.current_attempt_id === null) return job;
          const cacheKey = `${job.id}:${job.revision ?? job.state}:${job.current_attempt_id}`;
          let detailed = job;
          if (!job.attempt_telemetry?.length) {
            try {
              detailed = jobCache.current.get(cacheKey) ?? (await getAgentJob(job.id));
              if (!detailed || detailed.id !== job.id) return job;
              if (!controller.signal.aborted) jobCache.current.set(cacheKey, detailed);
            } catch {
              return job;
            }
          }
          if (controller.signal.aborted) return job;
          await Promise.all(
            (detailed.attempt_telemetry ?? []).map(async (attempt) => {
              const key = `${job.id}:${attempt.attempt_id}`;
              const cached = telemetryCache.current.get(key);
              if (cached?.duration_ms != null && cached.resources?.tool_time_ms != null) return;
              // The selected resource row can already be present on older APIs.
              if (attempt.attempt_id === job.telemetry_attempt_id && job.resources) return;
              try {
                const data = await getAgentAttemptTelemetry(
                  job.id,
                  attempt.attempt_id,
                  controller.signal,
                );
                if (!controller.signal.aborted && data.attempt_id === attempt.attempt_id) {
                  telemetryCache.current.set(key, data);
                }
              } catch {
                // The available summaries remain useful if detailed timing is unavailable.
              }
            }),
          );
          return detailed;
        }),
      );
      if (!controller.signal.aborted) {
        setLoaded({ source: jobs, jobs: detailedJobs, telemetry: new Map(telemetryCache.current) });
      }
    }
    void load();
    return () => controller.abort();
  }, [jobs]);

  const attempts = useMemo(
    () =>
      attemptTimings(loaded?.source === jobs ? loaded.jobs : jobs, loaded?.telemetry ?? new Map()),
    [jobs, loaded],
  );
  const complete = attempts.filter((attempt) => attempt.otherMs !== null).length;
  const approximate = attempts.some((attempt) => attempt.approximate);

  return (
    <section
      aria-label="Time breakdown"
      className="border-t border-gray-100 px-4 py-3 dark:border-white/5"
    >
      <h3 className="text-[11px] font-medium text-gray-600 dark:text-gray-300">Time breakdown</h3>
      {attempts.length === 0 ? (
        <p className="mt-1 text-[11px] text-gray-400 dark:text-gray-500">
          Timing not recorded yet.
        </p>
      ) : (
        <>
          <dl className="mt-2 grid grid-cols-3 gap-3 text-[11px]">
            {[
              { label: 'Recorded execution', value: total(attempts, 'durationMs'), tone: '' },
              {
                label: 'Using tools',
                value: total(attempts, 'toolsMs'),
                tone: 'text-amber-600 dark:text-amber-400',
              },
              {
                label: 'Other work',
                value: total(attempts, 'otherMs'),
                tone: 'text-violet-600 dark:text-violet-400',
              },
            ].map(({ label, value, tone }) => (
              <div key={label}>
                <dt className="text-gray-500 dark:text-gray-400">{label}</dt>
                <dd
                  className={`mt-0.5 text-[13px] font-medium tabular-nums ${tone || 'text-gray-800 dark:text-gray-200'}`}
                >
                  {value}
                </dd>
              </div>
            ))}
          </dl>
          <p className="mt-2 text-[10px] leading-relaxed text-gray-400 dark:text-gray-500">
            Tool time counts overlapping calls once. Other work includes model work and overhead.
            {approximate ? ' ≈ uses message timing, including event-processing delay.' : ''}
            {complete < attempts.length ? ' ≥ marks a partial total; — means unavailable.' : ''}
          </p>
          {attempts.length > 1 ? (
            <details className="mt-2 text-[11px] text-gray-500 dark:text-gray-400">
              <summary className="cursor-pointer">By turn and attempt ({attempts.length})</summary>
              <div className="mt-2 max-h-40 overflow-auto">
                <table className="w-full whitespace-nowrap text-left tabular-nums">
                  <thead>
                    <tr>
                      <th scope="col" className="pb-1 font-medium">
                        Turn / attempt
                      </th>
                      <th scope="col" className="pb-1 pr-3 text-right font-medium">
                        Execution
                      </th>
                      <th scope="col" className="pb-1 pr-3 text-right font-medium">
                        Tools
                      </th>
                      <th scope="col" className="pb-1 text-right font-medium">
                        Other
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {attempts.map((attempt) => (
                      <tr key={attempt.key}>
                        <th scope="row" className="py-0.5 font-normal">
                          {attempt.turnNo} / {attempt.attemptNo ?? '—'}
                        </th>
                        <td className="pr-3 text-right">
                          {formatDuration(attempt.durationMs) ?? '—'}
                        </td>
                        <td className="pr-3 text-right">
                          {attempt.approximate ? '≈ ' : ''}
                          {formatDuration(attempt.toolsMs) ?? '—'}
                        </td>
                        <td className="text-right">
                          {attempt.approximate && attempt.otherMs !== null ? '≈ ' : ''}
                          {formatDuration(attempt.otherMs) ?? '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          ) : null}
        </>
      )}
    </section>
  );
}

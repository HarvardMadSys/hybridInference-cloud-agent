'use client';

import { useEffect, useState } from 'react';

import { getAgentAttemptTelemetry, type AgentAttemptTelemetryApi } from '@/lib/api/agents';

import { toAttemptTelemetry } from './adapt';
import { TurnFooter } from './TurnFooter';
import type { AgentJob } from './types';

/** Load only the selected retry's heavy tool/process rows, not every retry on each poll. */
export function AttemptTurnFooter({ job, attemptNo }: { job: AgentJob; attemptNo: number }) {
  const attemptId = job.attempts.find((attempt) => attempt.no === attemptNo)?.id;
  const enabled = job.attemptTelemetryAvailable === true && attemptId !== undefined;
  const [retry, setRetry] = useState(0);
  const key = enabled ? `${job.id}:${attemptId}:${job.state}:${retry}` : null;
  const [loaded, setLoaded] = useState<{
    key: string;
    data?: AgentAttemptTelemetryApi;
    failed?: boolean;
  } | null>(null);

  useEffect(() => {
    if (key === null || attemptId === undefined) return;
    const controller = new AbortController();
    getAgentAttemptTelemetry(job.id, attemptId, controller.signal)
      .then((data) => {
        if (data.attempt_id !== attemptId) throw new Error('Attempt identity mismatch');
        if (!controller.signal.aborted) setLoaded({ key, data });
      })
      .catch(() => {
        if (!controller.signal.aborted) setLoaded({ key, failed: true });
      });
    return () => controller.abort();
  }, [key, job.id, attemptId]);

  // Matching the key during render also prevents one frame of stale data when
  // switching attempts, before the old effect's cleanup has run.
  const result = loaded?.key === key ? loaded : null;
  const data = result?.data;
  let selectedJob = job;
  if (data) {
    selectedJob = {
      ...job,
      attempts: job.attempts.map((attempt) =>
        attempt.id === attemptId ? { ...attempt, telemetry: toAttemptTelemetry(data) } : attempt,
      ),
    };
  }

  return (
    <>
      <TurnFooter job={selectedJob} attemptNo={attemptNo} />
      {enabled && !result ? (
        <p role="status" className="text-xs text-zinc-500 dark:text-zinc-400">
          Loading attempt measurements…
        </p>
      ) : null}
      {result?.failed ? (
        <p role="status" className="text-xs text-zinc-500 dark:text-zinc-400">
          Attempt measurements unavailable.{' '}
          <button type="button" onClick={() => setRetry((value) => value + 1)}>
            Retry
          </button>
        </p>
      ) : null}
    </>
  );
}

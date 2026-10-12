'use client';

import { useEffect, useState } from 'react';
import type { AgentJob } from './types';

function ActivityTimer({ startedAt }: { startedAt?: string }) {
  const [observedAt] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  const recordedAt = Date.parse(startedAt ?? '');
  const seconds = Math.max(
    0,
    Math.floor((now - (Number.isFinite(recordedAt) ? recordedAt : observedAt)) / 1000),
  );
  const minutes = Math.floor(seconds / 60);
  const elapsed = minutes ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;

  return (
    <span
      role="timer"
      aria-label="Elapsed activity time"
      aria-live="off"
      className="shrink-0 whitespace-nowrap tabular-nums"
    >
      {elapsed}
    </span>
  );
}

export function LiveAgentStatus({ job }: { job: AgentJob }) {
  if (job.state !== 'running' || !job.liveNote) return null;

  return (
    <div className="mb-2 flex items-start gap-2.5 px-2 text-[13px] leading-5 text-gray-600 dark:text-gray-300">
      <span
        aria-hidden="true"
        className="mt-1.5 h-2 w-2 shrink-0 animate-pulse rounded-full bg-blue-500 motion-reduce:animate-none"
      />
      <span
        role="status"
        aria-label="Agent status"
        aria-live="polite"
        aria-atomic="true"
        title={job.liveNote}
        className="min-w-0 flex-1 [overflow-wrap:anywhere]"
      >
        {job.liveNote}
      </span>
      <ActivityTimer
        key={`${job.id}:${job.currentAttemptId ?? ''}:${job.liveNote}:${job.liveNoteStartedAt ?? ''}`}
        startedAt={job.liveNoteStartedAt}
      />
    </div>
  );
}

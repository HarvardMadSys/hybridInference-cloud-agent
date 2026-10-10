import type { AgentJob } from './types';

export function LiveAgentStatus({ job }: { job: AgentJob }) {
  if (job.state !== 'running' || !job.liveNote) return null;

  return (
    <div
      role="status"
      aria-label="Agent status"
      aria-live="polite"
      aria-atomic="true"
      className="mb-2 flex items-center gap-2.5 px-2 text-[13px] text-gray-500 dark:text-gray-400"
    >
      <span
        aria-hidden="true"
        className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-blue-500 motion-reduce:animate-none"
      />
      {job.liveNote}
    </div>
  );
}

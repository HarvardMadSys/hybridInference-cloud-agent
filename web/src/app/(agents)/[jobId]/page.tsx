'use client';

import Link from 'next/link';
import { useEffect } from 'react';
import { useAuth } from '@/components/providers';
import { markConversationRead } from '@/components/features/agents/useConversationReadState';
import { usePathname } from 'next/navigation';
import { JobDetail } from '@/components/features/agents/JobDetail';
import { useAgentJob } from '@/components/features/agents/useAgentJobs';

export default function AgentJobPage() {
  // Follow-ups update browser history within this mounted conversation. Next
  // synchronizes usePathname with that URL; useParams still describes the
  // original route segment until a full router navigation happens.
  const pathname = usePathname();
  const jobId = pathname?.split('/').filter(Boolean).at(-1) ?? '';
  const { state } = useAuth();
  const userId = state.user?.id;
  return <AgentJobContent key={userId ?? 'signed-out'} jobId={jobId} userId={userId} />;
}

function AgentJobContent({ jobId, userId }: { jobId: string; userId?: string }) {
  const { job, events, threadJobs, loading, error, reload } = useAgentJob(jobId);
  useEffect(() => {
    if (!userId || !job || job.id !== jobId || loading || error) return;
    markConversationRead(
      { key: job.threadId ?? job.id, job, jobIds: [job.id], archived: false, lastActivityAt: null },
      userId,
    );
  }, [job, jobId, loading, error, userId]);

  // Keep the conversation visible while fetching its next turn. This also
  // requires keeping the route mounted when submitting a follow-up.
  if (loading && !job) {
    return (
      <div className="flex h-full items-center justify-center">
        <p className="text-sm text-gray-500 dark:text-gray-400">Loading job…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
          Could not load this job
        </h1>
        <p className="text-sm text-gray-500 dark:text-gray-400">{error}</p>
        <Link
          href="/"
          className="text-sm font-medium text-crimson dark:text-crimson-light hover:underline"
        >
          Back to Agents
        </Link>
      </div>
    );
  }

  if (!job) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">Job not found</h1>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          It may have been pruned, or the link is stale.
        </p>
        <Link
          href="/"
          className="text-sm font-medium text-crimson dark:text-crimson-light hover:underline"
        >
          Back to Agents
        </Link>
      </div>
    );
  }

  // Keyed on the *conversation*, not the turn. The per-turn state this used
  // to reset by remounting is reset by an effect in JobDetail instead, so
  // moving to the next turn of a thread no longer throws away the view the
  // reader is looking at — including which tab they had open.
  return (
    <JobDetail
      key={job.threadId ?? job.id}
      job={job}
      userId={userId}
      events={events}
      threadJobs={threadJobs}
      onReload={reload}
    />
  );
}

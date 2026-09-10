'use client';

import Link from 'next/link';
import { useEffect } from 'react';
import { useAuth } from '@/components/providers';
import { markConversationRead } from '@/components/features/agents/useConversationReadState';
import { useParams } from 'next/navigation';
import { JobDetail } from '@/components/features/agents/JobDetail';
import { useAgentJob } from '@/components/features/agents/useAgentJobs';

export default function AgentJobPage() {
  const params = useParams<{ jobId: string }>();
  const jobId = params?.jobId ?? '';
  const { state } = useAuth();
  const userId = state.user?.id;
  return <AgentJobContent key={userId ?? 'signed-out'} jobId={jobId} userId={userId} />;
}

function AgentJobContent({ jobId, userId }: { jobId: string; userId?: string }) {
  const { job, events, loading, error, reload } = useAgentJob(jobId);
  useEffect(() => {
    if (!userId || !job || job.id !== jobId || loading || error) return;
    markConversationRead(
      { key: job.threadId ?? job.id, job, jobIds: [job.id], archived: false, lastActivityAt: null },
      userId,
    );
  }, [job, jobId, loading, error, userId]);

  // Only the first load blanks the page. Sending a follow-up navigates to a
  // new turn of the same conversation, and replacing everything with a
  // centred "Loading…" for that round trip is why sending felt like a page
  // refresh — the thread the reader was in the middle of vanished and came
  // back. The hook keeps the previous job until the next one arrives, so
  // there is something to show throughout.
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
  return <JobDetail key={job.threadId ?? job.id} job={job} events={events} onReload={reload} />;
}

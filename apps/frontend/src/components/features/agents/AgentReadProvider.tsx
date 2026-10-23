'use client';

import { createContext, useContext, useEffect, useMemo, type ReactNode } from 'react';
import { getAgentJob, getAgentJobThread } from '@/lib/api/agents';
import { createAgentReadScope, type AgentReadScope } from '@/lib/api/agentReadScope';
import {
  createQuestionClocks,
  createQuestionReadScope,
  type QuestionReadScope,
} from '@/lib/api/questionReadScope';
import { listThreadQuestions } from '@/lib/api/questions';
import { onAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { onAgentRecordsChanged, type AgentRecordsChange } from './agentPush';
import { clearWorkspaceGitCache } from './workspaceGitCache';

const AgentReadContext = createContext<AgentReadScope>({
  getAgentJob: (jobId) => getAgentJob(jobId),
  getAgentJobThread: (jobId, options) =>
    options ? getAgentJobThread(jobId, options) : getAgentJobThread(jobId),
  // Outside the provider there is no signed-in owner to hold snapshots for, so
  // the fallback keeps none: every read goes to the API.
  getUsage: () => undefined,
  setUsage: () => {},
  invalidate: () => {},
});
const QuestionReadContext = createContext<QuestionReadScope>({
  listThreadQuestions: (threadId, signal) => listThreadQuestions(threadId, signal),
  invalidate: () => {},
  // Outside the provider there is no shell-wide clock to join either: each
  // consumer keeps its own.
  watch: (threadId, pace, read) => createQuestionClocks().watch(threadId, pace, read),
});

/**
 * The conversations whose question reads `change` can have made stale, or
 * `null` for all of them.
 *
 * Questions belong to a conversation's running turn, so only a change to one
 * of its turns, its deletion, or a gap in what the tab was told counts. A new
 * message moves the conversation and a publication moves its workspace, and
 * neither is a reason to cancel a read the host has already answered.
 */
function staleQuestionThreads(change: AgentRecordsChange): string[] | null {
  if (change.resync) return null;
  const threads = change.threads.filter((thread) => thread.deleted).map((thread) => thread.id);
  for (const job of change.jobs) {
    if (job.thread_id) threads.push(job.thread_id);
    // A deleted job is sent without its conversation, so it cannot be placed.
    else if (job.deleted) return null;
  }
  return threads;
}

/** The sidebar and page join reads within this owner's mounted shell. */
export function AgentReadProvider({ userId, children }: { userId?: string; children: ReactNode }) {
  const reads = useMemo(() => ({ userId, scope: createAgentReadScope() }), [userId]).scope;
  const questions = useMemo(() => ({ userId, scope: createQuestionReadScope() }), [userId]).scope;

  useEffect(() => {
    const sessions = onAgentSessionsChanged((change) => {
      reads.invalidate();
      for (const threadId of change.threadIds) questions.invalidate(threadId);
    });
    const records = onAgentRecordsChanged((change) => {
      if (
        change.resync ||
        change.jobs.length > 0 ||
        change.threads.length > 0 ||
        change.publications.length > 0
      ) {
        reads.invalidate();
      }
      const threads = staleQuestionThreads(change);
      if (threads === null) questions.invalidate();
      else for (const threadId of new Set(threads)) questions.invalidate(threadId);
      for (const threadId of change.questionThreads) questions.invalidate(threadId, true);
    });
    return () => {
      sessions();
      records();
      reads.invalidate();
      questions.invalidate();
      clearWorkspaceGitCache();
    };
  }, [reads, questions]);

  return (
    <AgentReadContext.Provider value={reads}>
      <QuestionReadContext.Provider value={questions}>{children}</QuestionReadContext.Provider>
    </AgentReadContext.Provider>
  );
}

export function useAgentReads(): AgentReadScope {
  return useContext(AgentReadContext);
}

export function useQuestionReads(): QuestionReadScope {
  return useContext(QuestionReadContext);
}

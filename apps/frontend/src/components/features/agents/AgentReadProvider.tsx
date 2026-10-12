'use client';

import { createContext, useContext, useEffect, useMemo, type ReactNode } from 'react';
import { getAgentJob, getAgentJobThread } from '@/lib/api/agents';
import { createAgentReadScope, type AgentReadScope } from '@/lib/api/agentReadScope';
import { createQuestionReadScope, type QuestionReadScope } from '@/lib/api/questionReadScope';
import { listThreadQuestions } from '@/lib/api/questions';
import { onAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { onAgentRecordsChanged } from './agentPush';

const AgentReadContext = createContext<AgentReadScope>({
  getAgentJob: (jobId) => getAgentJob(jobId),
  getAgentJobThread: (jobId) => getAgentJobThread(jobId),
  invalidate: () => {},
});
const QuestionReadContext = createContext<QuestionReadScope>({
  listThreadQuestions: (threadId, signal) => listThreadQuestions(threadId, signal),
  invalidate: () => {},
});

/** The sidebar and page join reads within this owner's mounted shell. */
export function AgentReadProvider({ userId, children }: { userId?: string; children: ReactNode }) {
  const reads = useMemo(() => ({ userId, scope: createAgentReadScope() }), [userId]).scope;
  const questions = useMemo(() => ({ userId, scope: createQuestionReadScope() }), [userId]).scope;

  useEffect(() => {
    const invalidate = () => {
      reads.invalidate();
      questions.invalidate();
    };
    const sessions = onAgentSessionsChanged(invalidate);
    const records = onAgentRecordsChanged((change) => {
      if (
        change.resync ||
        change.jobs.length > 0 ||
        change.threads.length > 0 ||
        change.publications.length > 0
      ) {
        invalidate();
      }
    });
    return () => {
      sessions();
      records();
      invalidate();
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

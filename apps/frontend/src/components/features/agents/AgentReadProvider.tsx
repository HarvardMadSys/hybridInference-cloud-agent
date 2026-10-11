'use client';

import { createContext, useContext, useEffect, useMemo, type ReactNode } from 'react';
import { getAgentJob, getAgentJobThread } from '@/lib/api/agents';
import { createAgentReadScope, type AgentReadScope } from '@/lib/api/agentReadScope';
import { onAgentSessionsChanged } from '@/lib/api/agentSessionEvents';
import { onAgentRecordsChanged } from './agentPush';

const AgentReadContext = createContext<AgentReadScope>({
  getAgentJob: (jobId) => getAgentJob(jobId),
  getAgentJobThread: (jobId) => getAgentJobThread(jobId),
  invalidate: () => {},
});

/** The sidebar and page join reads within this owner's mounted shell. */
export function AgentReadProvider({ userId, children }: { userId?: string; children: ReactNode }) {
  const reads = useMemo(() => ({ userId, scope: createAgentReadScope() }), [userId]).scope;

  useEffect(() => {
    const sessions = onAgentSessionsChanged(() => reads.invalidate());
    const records = onAgentRecordsChanged((change) => {
      if (
        change.resync ||
        change.jobs.length > 0 ||
        change.threads.length > 0 ||
        change.publications.length > 0
      ) {
        reads.invalidate();
      }
    });
    return () => {
      sessions();
      records();
      reads.invalidate();
    };
  }, [reads]);

  return <AgentReadContext.Provider value={reads}>{children}</AgentReadContext.Provider>;
}

export function useAgentReads(): AgentReadScope {
  return useContext(AgentReadContext);
}

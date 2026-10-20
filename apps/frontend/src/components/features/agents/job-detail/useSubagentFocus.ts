import { useCallback, useEffect, useMemo } from 'react';

import type { AgentJobApi, AgentJobEventApi, AgentThreadEventApi } from '@/lib/api/agents';

import {
  subagentStartedBy,
  turnActivityOverview,
  type OverviewSubagent,
} from '../activityOverview';
import { ownedBySubagent, subagentDisplayEvents } from '../adapt';
import type { SubagentOfCall } from '../ChatMessages';
import type { ConversationTurnDescription } from '../conversationTurnNavigation';
import type { JobDetailPanels, SubagentTarget } from './useJobDetailPanels';

/**
 * The subagent a reader opened, and what each view shows of it.
 *
 * A subagent is named by the turn that started it and its key in that turn's
 * activity. The name resolves against the turns on screen, and is dropped once
 * none of them has it. From it come the subagent's own conversation, shown in
 * place of the chat; the link from each call to the subagent it started; and
 * the trajectory, narrowed to the subagent's steps.
 */
export function useSubagentFocus({
  panels,
  currentTurn,
  historyTurns,
  events,
  threadJobs,
}: {
  panels: JobDetailPanels;
  currentTurn: ConversationTurnDescription;
  historyTurns: ConversationTurnDescription[];
  events: (AgentJobEventApi | AgentThreadEventApi)[];
  threadJobs: AgentJobApi[];
}) {
  const { tab, setTab, subagent, setSubagent, trajectorySteps } = panels;
  // A subagent is named by the turn that started it, so any turn's can be
  // opened. One that is no longer shown — another attempt was selected —
  // returns to the Chat it replaced.
  const turnOf = useCallback(
    (jobId: string) =>
      jobId === currentTurn.jobId ? currentTurn : historyTurns.find((turn) => turn.jobId === jobId),
    [currentTurn, historyTurns],
  );
  const subagentAt = useCallback(
    (target: SubagentTarget | null) => {
      const turn = target === null ? undefined : turnOf(target.jobId);
      return turn === undefined
        ? undefined
        : turnActivityOverview(turn.events).subagents.find((agent) => agent.key === target?.key);
    },
    [turnOf],
  );
  // Codex threads outlive a turn; read only the turn and attempt that started one.
  const subagentScope = useCallback(
    (jobId: string, agent: OverviewSubagent) => {
      const source = events.find((event) => event.id === agent.sourceEvent.sourceEventId);
      return {
        inScope: (event: AgentJobEventApi | AgentThreadEventApi) =>
          (!('job_id' in event) || event.job_id === jobId) &&
          (!source || event.attempt_id === source.attempt_id),
        owner: { callId: agent.sourceEvent.id, threads: agent.activity.targets },
      };
    },
    [events],
  );
  const openSubagent = subagentAt(subagent);
  const openSubagentJobId = openSubagent ? subagent?.jobId : undefined;
  useEffect(() => {
    if (subagent !== null && !openSubagent) setSubagent(null);
  }, [openSubagent, setSubagent, subagent]);
  const subagentEvents = useMemo(() => {
    if (!openSubagent || !openSubagentJobId) return [];
    const { inScope, owner } = subagentScope(openSubagentJobId, openSubagent);
    return subagentDisplayEvents(
      events.filter(inScope),
      owner,
      threadJobs.find((turn) => turn.id === openSubagentJobId),
    );
  }, [events, openSubagent, openSubagentJobId, subagentScope, threadJobs]);
  const showingSubagent = tab === 'chat' && openSubagent !== undefined;
  const subagentOfCall = useCallback<SubagentOfCall>(
    (jobId, event) => {
      const turn = turnOf(jobId);
      const agent = turn && subagentStartedBy(turnActivityOverview(turn.events), event);
      if (!agent) return undefined;
      return {
        title: agent.activity.description ?? agent.activity.title,
        open: () => {
          setTab('chat');
          setSubagent({ jobId, key: agent.key });
        },
      };
    },
    [setSubagent, setTab, turnOf],
  );
  // Every turn's subagents, for narrowing the trajectory to one of them.
  const trajectorySubagents = useMemo(() => {
    if (tab !== 'trajectory') return [];
    return [...historyTurns, currentTurn].flatMap((turn) =>
      turnActivityOverview(turn.events).subagents.map((agent) => ({
        value: JSON.stringify([turn.jobId, agent.key]),
        label: `Turn ${turn.displayNo} · ${agent.activity.description ?? agent.activity.title}`,
      })),
    );
  }, [currentTurn, historyTurns, tab]);
  // The subagent being read narrows the trajectory to its own steps.
  const shownTrajectorySteps = useMemo(() => {
    if (!openSubagent || !openSubagentJobId) return trajectorySteps;
    const { inScope, owner } = subagentScope(openSubagentJobId, openSubagent);
    // The call that started the subagent, then what the subagent recorded.
    return trajectorySteps.filter(
      (step) =>
        step.sourceEvents.some(inScope) &&
        ((step.callId !== undefined && step.callId === owner.callId) ||
          step.sourceEvents.some((event) => ownedBySubagent(event.payload, owner))),
    );
  }, [openSubagent, openSubagentJobId, subagentScope, trajectorySteps]);
  return {
    openSubagent,
    openSubagentJobId,
    subagentEvents,
    showingSubagent,
    subagentOfCall,
    trajectorySubagents,
    shownTrajectorySteps,
  };
}

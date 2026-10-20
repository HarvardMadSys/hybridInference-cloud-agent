// @vitest-environment jsdom
import { cleanup, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { turnActivityOverview } from '../activityOverview';
import type { ConversationTurnDescription } from '../conversationTurnNavigation';
import type { AgentJob } from '../types';
import type { JobDetailPanels, SubagentTarget } from './useJobDetailPanels';
import { useSubagentFocus } from './useSubagentFocus';

afterEach(cleanup);

/** One turn whose activity started one subagent. */
function turnWithSubagent(jobId: string): ConversationTurnDescription {
  const events: AgentJob['events'] = [
    {
      kind: 'tool_use',
      tool: 'spawn_agent',
      id: 'spawn-review-1',
      detail: 'Review window interactions',
      input: { task_name: 'Review window interactions', message: 'Check focus restoration.' },
      output: [
        JSON.stringify({
          agent_id: 'agent-ui-review-1',
          agents_states: { 'agent-ui-review-1': { status: 'running' } },
        }),
      ],
      resultState: 'received',
      attemptNo: 1,
      sourceEventId: 12,
    },
  ];
  return { key: jobId, jobId, displayNo: 1, events } as unknown as ConversationTurnDescription;
}

function emptyTurn(jobId: string): ConversationTurnDescription {
  return { key: jobId, jobId, displayNo: 1, events: [] } as unknown as ConversationTurnDescription;
}

function panels(subagent: SubagentTarget | null, setSubagent = vi.fn()): JobDetailPanels {
  return {
    tab: 'chat',
    setTab: vi.fn(),
    subagent,
    setSubagent,
    trajectorySteps: [],
  } as unknown as JobDetailPanels;
}

describe('subagent focus', () => {
  it('opens a subagent its turn started, in place of the chat', () => {
    const turn = turnWithSubagent('ajob_1');
    const [agent] = turnActivityOverview(turn.events).subagents;
    const setSubagent = vi.fn();

    const { result } = renderHook(() =>
      useSubagentFocus({
        panels: panels({ jobId: 'ajob_1', key: agent.key }, setSubagent),
        currentTurn: turn,
        historyTurns: [],
        events: [],
        threadJobs: [],
      }),
    );

    expect(result.current.openSubagent?.key).toBe(agent.key);
    expect(result.current.showingSubagent).toBe(true);
    expect(setSubagent).not.toHaveBeenCalled();
  });

  it('lets go of a subagent no shown turn has, so it does not come back on its own', () => {
    const turn = turnWithSubagent('ajob_1');
    const [agent] = turnActivityOverview(turn.events).subagents;
    const setSubagent = vi.fn();
    const target = { jobId: 'ajob_1', key: agent.key };

    const { result, rerender } = renderHook(
      ({ currentTurn }) =>
        useSubagentFocus({
          panels: panels(target, setSubagent),
          currentTurn,
          historyTurns: [],
          events: [],
          threadJobs: [],
        }),
      { initialProps: { currentTurn: turn } },
    );
    expect(setSubagent).not.toHaveBeenCalled();

    // Another attempt is shown, and it never started this subagent.
    rerender({ currentTurn: emptyTurn('ajob_1') });

    expect(result.current.openSubagent).toBeUndefined();
    expect(result.current.showingSubagent).toBe(false);
    expect(setSubagent).toHaveBeenCalledWith(null);
  });
});

// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentJobApi, AgentThreadEventApi } from '@/lib/api/agents';
import { toDisplayJob } from './adapt';
import { EventRow } from './ChatMessages';
import { eventBlockKey } from './conversationTurnNavigation';
import { ToolStepDetails } from './ToolStepDetails';
import { buildTrajectorySteps } from './trajectorySteps';

afterEach(cleanup);

const job = {
  id: 'job',
  repo: 'owner/repo',
  task_prompt: 'Fix the tests',
  runtime: 'claude-code',
  model: 'model',
  base_sha: 'abc',
  state: 'running',
  cancel_requested: false,
  current_attempt_id: 1,
  published_pr_url: null,
  detail: null,
  metadata: null,
  created_at: null,
  updated_at: null,
  spent_usd: null,
  tokens_in: null,
  tokens_out: null,
  model_calls: null,
} satisfies AgentJobApi;

function event(
  id: number,
  event_type: string,
  payload: Record<string, unknown>,
  overrides: Partial<AgentThreadEventApi> = {},
): AgentThreadEventApi {
  return {
    id,
    event_type,
    payload,
    attempt_id: 1,
    seq: id,
    created_at: null,
    job_id: 'job',
    turn_no: 1,
    ...overrides,
  };
}

describe('task and delegation activity in the conversation', () => {
  it('renders a recorded native plan as a checklist without waiting for a tool result', () => {
    const events = [
      event(1, 'raw', {
        protocol: 'codex-app-server',
        notification: {
          method: 'turn/plan/updated',
          params: {
            threadId: 'thread',
            turnId: 'turn',
            plan: [
              { step: 'Inspect failures', status: 'completed' },
              { step: 'Repair tests', status: 'inProgress' },
            ],
          },
        },
      }),
    ];
    const [activity] = toDisplayJob(job, { events }).events;
    render(<EventRow event={activity} blockKey="job:event:1" />);
    expect(screen.getByText('Inspect failures')).toBeInTheDocument();
    expect(screen.getByText('Repair tests')).toBeInTheDocument();
    expect(screen.getByText('1 of 2 marked completed')).toBeInTheDocument();
    expect(screen.getByText('Result received')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Task activity' })).toHaveAttribute(
      'data-block-key',
      'job:event:1',
    );
  });

  it('keeps the launch result distinct from a background agent’s state', () => {
    const events = [
      event(1, 'tool_use', {
        id: 'delegate',
        name: 'Task',
        input: {
          description: 'Review tests',
          subagent_type: 'Explore',
          run_in_background: true,
          prompt: 'Review test coverage without changing files.',
        },
      }),
      event(2, 'tool_result', { tool_use_id: 'delegate', content: 'Started in background.' }),
    ];
    const [activity] = toDisplayJob(job, { events }).events;
    render(<EventRow event={activity} />);
    expect(screen.getByText('Review tests')).toBeInTheDocument();
    expect(screen.getByText('Background requested')).toBeInTheDocument();
    expect(screen.getByText('Result received')).toBeInTheDocument();
    expect(screen.queryByText('Completed')).not.toBeInTheDocument();
    const prompt = screen.getByText('Review test coverage without changing files.');
    expect(prompt.closest('details')).not.toHaveAttribute('open');
  });

  it('shows missing results as waiting only for the current active attempt', () => {
    const events = [
      event(1, 'tool_use', { id: 'task', name: 'Task', input: { description: 'Review' } }),
    ];
    expect(toDisplayJob(job, { events }).events[0]).toMatchObject({ resultState: 'waiting' });
    expect(toDisplayJob({ ...job, current_attempt_id: 2 }, { events }).events[0]).toMatchObject({
      resultState: 'not_recorded',
    });
    expect(toDisplayJob({ ...job, state: 'succeeded' }, { events }).events[0]).toMatchObject({
      resultState: 'not_recorded',
    });
  });

  it('labels delegated tool calls and leaves child replies in Trajectory', () => {
    const events = [
      event(1, 'tool_use', {
        name: 'Bash',
        id: 'child-tool',
        input: { command: 'npm test' },
        parent_tool_use_id: 'delegate',
      }),
      event(2, 'message', { text: 'Child findings', parent_tool_use_id: 'delegate' }),
    ];
    const rows = toDisplayJob(job, { events }).events;
    expect(rows).toHaveLength(1);
    render(<EventRow event={rows[0]} blockKey="child-anchor" />);
    expect(screen.getByText('Subagent tool')).toBeInTheDocument();
    expect(screen.getByText('Subagent tool').closest('[data-block-key]')).toHaveAttribute(
      'data-block-key',
      'child-anchor',
    );
    expect(buildTrajectorySteps(events).some((step) => step.detail === 'Child findings')).toBe(
      true,
    );
  });
});

describe('recorded delegation hierarchy', () => {
  it('keeps a native activity anchor stable when its start backfills before the completion', () => {
    const item = {
      type: 'collabAgentToolCall',
      id: 'native-call',
      tool: 'spawnAgent',
      status: 'completed',
      receiverThreadIds: ['child'],
      prompt: 'Review',
      agentsStates: {},
    };
    const completed = event(2, 'raw', {
      protocol: 'codex-app-server',
      notification: {
        method: 'item/completed',
        params: { item },
      },
    });
    const started = event(1, 'raw', {
      protocol: 'codex-app-server',
      notification: {
        method: 'item/started',
        params: { item: { ...item, status: 'inProgress' } },
      },
    });
    const initial = toDisplayJob(job, { events: [completed] }).events[0];
    const backfilled = toDisplayJob(job, { events: [started, completed] }).events[0];
    expect(initial.sourceEventId).not.toBe(backfilled.sourceEventId);
    expect(eventBlockKey(job.id, initial, 0)).toBe(eventBlockKey(job.id, backfilled, 0));
  });

  it('never pairs an explicit unmatched child result with a neighbouring parent', () => {
    const events = [
      event(1, 'tool_use', { id: 'delegate', name: 'Task', input: { description: 'Review' } }),
      event(2, 'tool_result', {
        tool_use_id: 'missing-child',
        parent_tool_use_id: 'delegate',
        content: 'Child output',
      }),
    ];
    const rows = toDisplayJob(job, { events }).events;
    expect(rows).toHaveLength(2);
    expect(rows[0]).toMatchObject({ kind: 'tool_use', resultState: 'waiting' });
    expect(rows[0]).not.toHaveProperty('output');
    expect(rows[1]).toMatchObject({ kind: 'tool_result', text: 'Child output' });
  });

  it('preserves ambiguous results instead of presenting a verified pairing', () => {
    const events = [
      event(1, 'tool_use', { id: 'delegate', name: 'Task', input: { description: 'First' } }),
      event(2, 'tool_use', { id: 'delegate', name: 'Task', input: { description: 'Second' } }),
      event(3, 'tool_result', { tool_use_id: 'delegate', content: 'Some result' }),
    ];
    const rows = toDisplayJob(job, { events }).events;
    expect(rows).toHaveLength(3);
    expect(
      rows
        .slice(0, 2)
        .every((row) => row.kind === 'tool_use' && row.resultState === 'unknown' && !row.output),
    ).toBe(true);
  });

  it('shows an interrupted native collaboration call as interrupted in Chat', () => {
    const events = [
      event(1, 'raw', {
        protocol: 'codex-app-server',
        notification: {
          method: 'item/completed',
          params: {
            threadId: 'thread',
            turnId: 'turn',
            item: {
              type: 'collabAgentToolCall',
              id: 'wait-call',
              tool: 'wait',
              status: 'interrupted',
              receiverThreadIds: ['child'],
              agentsStates: { child: { status: 'running' } },
            },
          },
        },
      }),
    ];
    const [activity] = toDisplayJob(job, { events }).events;
    render(<EventRow event={activity} />);
    expect(screen.getByText('Call interrupted')).toBeInTheDocument();
    expect(screen.getByText('Running')).toBeInTheDocument();
    expect(screen.queryByText('Completed')).not.toBeInTheDocument();
  });

  it('links backfilled child activity independently of model-message ownership', () => {
    const events = [
      event(1, 'tool_use', {
        name: 'Bash',
        id: 'child',
        input: { command: 'npm test' },
        usage_id: 'child-message',
        parent_tool_use_id: 'delegate',
      }),
      event(2, 'tool_use', {
        name: 'Agent',
        id: 'delegate',
        input: { description: 'Review' },
        usage_id: 'main-message',
      }),
      event(3, 'tool_result', {
        tool_use_id: 'child',
        content: 'Tests passed',
        parent_tool_use_id: 'delegate',
      }),
    ];
    const steps = buildTrajectorySteps(events);
    const parent = steps.find((step) => step.callId === 'delegate')!;
    const child = steps.find((step) => step.callId === 'child')!;
    expect(child.delegationParentKey).toBe(parent.key);
    expect(child.parentKey).not.toBe(parent.key);
    expect(parent.delegatedKeys).toContain(child.key);
    const onSelectStep = vi.fn();
    render(
      <ToolStepDetails
        step={parent}
        tab="summary"
        delegatedSteps={[child]}
        onSelectStep={onSelectStep}
        onSelectTab={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: /Bash/ }));
    expect(onSelectStep).toHaveBeenCalledWith(child.key);
  });

  it('does not link activity across attempts or jobs, or to ambiguous calls', () => {
    const parent = event(1, 'tool_use', {
      name: 'Task',
      id: 'delegate',
      input: { description: 'Review' },
    });
    const childPayload = { name: 'Bash', id: 'child', parent_tool_use_id: 'delegate' };
    const steps = buildTrajectorySteps([
      parent,
      event(2, 'tool_use', childPayload, { attempt_id: 2 }),
      event(3, 'tool_use', childPayload, { job_id: 'other', turn_no: 2 }),
    ]);
    expect(
      steps.filter((step) => step.callId === 'child').every((step) => !step.delegationParentKey),
    ).toBe(true);
    const ambiguous = buildTrajectorySteps([
      parent,
      { ...parent, id: 4, seq: 4 },
      event(5, 'tool_use', childPayload),
    ]);
    expect(ambiguous.find((step) => step.callId === 'child')?.delegationParentKey).toBeUndefined();
  });
});

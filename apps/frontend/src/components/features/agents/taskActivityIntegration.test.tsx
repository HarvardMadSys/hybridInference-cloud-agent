// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentJobApi, AgentThreadEventApi } from '@/lib/api/agents';
import { ActivityWindow } from './ActivityWindow';
import { buildActivityOverview } from './activityOverview';
import { toDisplayJob } from './adapt';
import { EventRow } from './ChatMessages';
import { eventBlockKey } from './conversationTurnNavigation';
import { ToolActivityDetails } from './ToolActivityDetails';
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
  it.each(['flat text', 'serialized text block', 'structured output'] as const)(
    'renders three Claude tasks by joining %s create results to later status updates',
    (resultFormat) => {
      function createResult(id: string, subject: string): Record<string, unknown> {
        if (resultFormat === 'structured output') {
          return {
            content: 'Task created.',
            structured_output: { task: { id, subject } },
          };
        }
        const acknowledgement = `Task #${id} created successfully: ${subject}`;
        return {
          content:
            resultFormat === 'serialized text block'
              ? JSON.stringify([{ type: 'text', text: acknowledgement }])
              : acknowledgement,
        };
      }

      const tasks = [
        {
          subject: 'Create notification helper script',
          description: 'Write the notification helper script.',
          status: 'completed',
          label: 'Completed',
        },
        {
          subject: 'Configure global hooks for Stop and Elicitation',
          description: 'Configure the two hooks in the global settings.',
          status: 'in_progress',
          label: 'In progress',
        },
        {
          subject: 'Validate settings JSON and test',
          description: 'Validate the settings file and test notifications.',
          status: 'completed',
          label: 'Completed',
        },
      ];
      const creates = tasks.flatMap(({ subject, description }, index) => [
        event(index * 2 + 1, 'tool_use', {
          id: `create-${index + 1}`,
          name: 'TaskCreate',
          input: { subject, description },
        }),
        event(index * 2 + 2, 'tool_result', {
          tool_use_id: `create-${index + 1}`,
          ...createResult(String(index + 1), subject),
        }),
      ]);
      const updates = tasks.flatMap(({ status }, index) => [
        event(index * 2 + 7, 'tool_use', {
          id: `update-${index + 1}`,
          name: 'TaskUpdate',
          input: { taskId: String(index + 1), status },
        }),
        event(index * 2 + 8, 'tool_result', {
          tool_use_id: `update-${index + 1}`,
          content: `Updated task #${index + 1} status`,
        }),
      ]);
      const overview = buildActivityOverview(
        toDisplayJob(job, { events: [...creates, ...updates] }).events,
      );

      expect(overview.taskActivity?.tasks).toHaveLength(3);
      expect(overview.taskActivity?.tasks).toMatchObject(
        tasks.map(({ subject, description, status }, index) => ({
          id: String(index + 1),
          subject,
          description,
          status,
        })),
      );
      render(<ToolActivityDetails activity={overview.taskActivity!} />);
      expect(screen.getByText('2 of 3 marked completed')).toBeInTheDocument();
      const rows = screen.getAllByRole('listitem');
      expect(rows).toHaveLength(3);
      tasks.forEach(({ subject, description, label }, index) => {
        const row = within(rows[index]);
        expect(row.getByText(`#${index + 1}`)).toBeInTheDocument();
        expect(row.getByText(subject)).toBeInTheDocument();
        expect(row.getByText(label)).toBeInTheDocument();
        expect(row.getByText(description).closest('details')).not.toHaveAttribute('open');
      });
      expect(screen.queryByText(/^Task #\d+$/)).not.toBeInTheDocument();
      expect(screen.queryByText('Status not recorded')).not.toBeInTheDocument();
    },
  );

  it('reconciles a waiting create when its text result backfills before a status update', () => {
    const create = event(1, 'tool_use', {
      id: 'create',
      name: 'TaskCreate',
      input: { subject: 'Review notifications', description: 'Check the hook behavior.' },
    });
    const initial = buildActivityOverview(toDisplayJob(job, { events: [create] }).events);
    expect(initial.taskActivity?.tasks).toHaveLength(1);
    expect(initial.taskActivity?.tasks[0]).toMatchObject({
      subject: 'Review notifications',
      description: 'Check the hook behavior.',
    });
    expect(initial.taskActivity?.tasks[0].id).toBeUndefined();
    const { rerender } = render(<ToolActivityDetails activity={initial.taskActivity!} />);

    const backfilled = buildActivityOverview(
      toDisplayJob(job, {
        events: [
          create,
          event(2, 'tool_result', {
            tool_use_id: 'create',
            content: 'Task #1 created successfully: Review notifications',
          }),
          event(3, 'tool_use', {
            id: 'update',
            name: 'TaskUpdate',
            input: { taskId: '1', status: 'completed' },
          }),
          event(4, 'tool_result', {
            tool_use_id: 'update',
            content: 'Updated task #1 status',
          }),
        ],
      }).events,
    );
    expect(backfilled.taskActivity?.tasks).toHaveLength(1);
    expect(backfilled.taskActivity?.tasks[0]).toMatchObject({
      id: '1',
      subject: 'Review notifications',
      description: 'Check the hook behavior.',
      status: 'completed',
    });
    rerender(<ToolActivityDetails activity={backfilled.taskActivity!} />);
    expect(screen.getAllByRole('listitem')).toHaveLength(1);
    expect(screen.getByText('1 of 1 marked completed')).toBeInTheDocument();
    expect(screen.queryByText('Task #1')).not.toBeInTheDocument();
  });

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

  describe('a Claude subagent card, from records shaped like the pinned CLI’s', () => {
    function delegation(background: boolean, content: unknown[], structured: object) {
      return [
        event(1, 'tool_use', {
          id: 'toolu_delegate',
          name: 'Agent',
          input: {
            description: 'Inspect the workspace',
            prompt: 'Reply with the word inspected.',
            subagent_type: 'Explore',
            run_in_background: background,
          },
        }),
        event(2, 'tool_result', {
          tool_use_id: 'toolu_delegate',
          is_error: false,
          content: JSON.stringify(content),
          structured_output: structured,
        }),
      ];
    }

    function renderCards(events: AgentThreadEventApi[]) {
      render(
        <ActivityWindow
          overview={buildActivityOverview(toDisplayJob(job, { events }).events)}
          turnNo={1}
          attemptNo={1}
          onClose={() => {}}
          onOpenSubagent={() => {}}
        />,
      );
      return screen.getByRole('region', { name: 'Subagents overview' });
    }

    it('shows the answer the Agent call returned to the main agent', () => {
      const blocks = [{ type: 'text', text: '## Findings\n\n- inspected' }];
      const cards = renderCards(
        delegation(false, blocks, {
          status: 'completed',
          prompt: 'Reply with the word inspected.',
          agentId: 'aa9397b543b5f2ff7',
          agentType: 'Explore',
          content: blocks,
          totalDurationMs: 11,
          totalTokens: 2,
          totalToolUseCount: 0,
        }),
      );
      const reply = within(cards).getByRole('region', { name: 'Subagent reply' });
      expect(within(reply).getByText('Completed')).toBeInTheDocument();
      expect(within(reply).getByRole('heading', { name: 'Findings' })).toBeInTheDocument();
      expect(within(reply).getByRole('listitem')).toHaveTextContent('inspected');
      expect(cards).not.toHaveTextContent('"type"');
      expect(within(cards).queryByText('Agent state not recorded')).not.toBeInTheDocument();
    });

    it('does not show a background launch notice as the answer', () => {
      const notice =
        'Async agent launched successfully.\nagentId: a0bc416394db49101\nThe agent is working in the background.';
      const cards = renderCards(
        delegation(true, [{ type: 'text', text: notice }], {
          isAsync: true,
          status: 'async_launched',
          agentId: 'a0bc416394db49101',
          description: 'Inspect the workspace',
          prompt: 'Reply with the word inspected.',
          canReadOutputFile: true,
        }),
      );
      expect(
        within(cards).getByText(
          "Started in the background. This call returned only a launch notice, not the agent's reply.",
        ),
      ).toBeInTheDocument();
      expect(cards).not.toHaveTextContent('Async agent launched successfully');
      expect(within(cards).queryByText('Completed')).not.toBeInTheDocument();
    });

    it('shows the unpacked answer in the inspector, not the stored content blocks', () => {
      const blocks = [{ type: 'text', text: '## Findings\n\n- inspected' }];
      const steps = buildTrajectorySteps(
        delegation(false, blocks, { status: 'completed', agentId: 'aa9397b543b5f2ff7' }),
      );
      render(
        <ToolStepDetails
          step={steps.find((step) => step.callId === 'toolu_delegate')!}
          tab="summary"
          onSelectTab={vi.fn()}
        />,
      );
      const reply = screen.getByRole('region', { name: 'Subagent reply' });
      expect(within(reply).getByText('Completed')).toBeInTheDocument();
      expect(within(reply).getByRole('listitem')).toHaveTextContent('inspected');
      expect(reply).not.toHaveTextContent('"type"');
    });

    it('keeps saying a state was not recorded for a launch that reports neither', () => {
      const cards = renderCards([
        event(1, 'tool_use', {
          id: 'spawn',
          name: 'spawn_agent',
          input: { task_name: 'Review', message: 'Review the change.' },
        }),
        event(2, 'tool_result', {
          tool_use_id: 'spawn',
          content: JSON.stringify({ agent_id: 'reviewer' }),
        }),
      ]);
      expect(within(cards).getByText('Agent state not recorded')).toBeInTheDocument();
      expect(
        within(cards).queryByRole('region', { name: 'Subagent reply' }),
      ).not.toBeInTheDocument();
    });
  });

  describe('subagent replies from the other runtimes’ records', () => {
    function renderCards(events: AgentThreadEventApi[]) {
      render(
        <ActivityWindow
          overview={buildActivityOverview(toDisplayJob(job, { events }).events)}
          turnNo={1}
          attemptNo={1}
          onClose={() => {}}
          onOpenSubagent={() => {}}
        />,
      );
      return screen.getByRole('region', { name: 'Subagents overview' });
    }

    function task(content: string) {
      return [
        event(1, 'tool_use', {
          id: 'call-task',
          name: 'task',
          input: { description: 'Review the change', prompt: 'Review.', subagent_type: 'general' },
        }),
        event(2, 'tool_result', { tool_use_id: 'call-task', is_error: false, content }),
      ];
    }

    it('reads an OpenCode or Kilo task’s answer out of its envelope', () => {
      const cards = renderCards(
        task(
          '<task id="ses_child" state="completed">\n<task_result>\n## Review\n\n- No issues.\n</task_result>\n</task>',
        ),
      );
      const reply = within(cards).getByRole('region', { name: 'Subagent reply' });
      expect(within(reply).getByRole('heading', { name: 'Review' })).toBeInTheDocument();
      expect(within(reply).getByRole('listitem')).toHaveTextContent('No issues.');
      expect(cards).not.toHaveTextContent('task_result');
      expect(within(cards).queryByRole('note')).not.toBeInTheDocument();
    });

    it('says when an earlier host stored only the start of a task’s answer', () => {
      const clipped = `<task id="ses_child" state="completed">\n<task_result>\n${'finding '.repeat(600)}`;
      const cards = renderCards(task(clipped.slice(0, 4000)));
      expect(within(cards).getByRole('note')).toHaveTextContent(
        'The stored reply was cut short; this is not the whole answer.',
      );
    });

    it('shows a Codex agent’s answer from the state its wait call returned', () => {
      function collab(id: number, method: string, item: Record<string, unknown>) {
        return event(id, 'raw', {
          protocol: 'codex-app-server',
          notification: { method, params: { threadId: 'main', turnId: 'turn', item } },
        });
      }
      const spawn = {
        type: 'collabAgentToolCall',
        id: 'call-spawn',
        tool: 'spawnAgent',
        prompt: 'Review the change.',
        receiverThreadIds: ['worker'],
      };
      const wait = {
        type: 'collabAgentToolCall',
        id: 'call-wait',
        tool: 'wait',
        receiverThreadIds: ['worker'],
      };
      const cards = renderCards([
        collab(1, 'item/started', { ...spawn, status: 'inProgress' }),
        collab(2, 'item/completed', {
          ...spawn,
          status: 'completed',
          agentsStates: { worker: { status: 'running', message: null } },
        }),
        collab(3, 'item/started', { ...wait, status: 'inProgress' }),
        collab(4, 'item/completed', {
          ...wait,
          status: 'completed',
          agentsStates: { worker: { status: 'completed', message: '**No issues** found.' } },
        }),
      ]);
      const reply = within(cards).getByRole('region', { name: 'Subagent reply' });
      expect(reply).toHaveTextContent('No issues found.');
      expect(within(reply).getByText('No issues').tagName).toBe('STRONG');
      expect(within(cards).getAllByText('Completed')).toHaveLength(1);
    });
  });

  it('shows a Codex MCP tool named like a delegation as an ordinary call', () => {
    // As the Codex driver records one: the tool's own name, and the server it went to.
    const events = [
      event(1, 'tool_use', {
        id: 'mcp-1',
        name: 'review__task',
        input: { description: 'Review the change', prompt: 'Review.' },
        mcp_server: 'session',
      }),
      event(2, 'tool_result', { tool_use_id: 'mcp-1', is_error: false, content: 'Looks good.' }),
    ];
    const rows = toDisplayJob(job, { events }).events;
    render(<EventRow event={rows[0]} />);
    expect(screen.queryByRole('region', { name: 'Subagent activity' })).not.toBeInTheDocument();
    expect(screen.getByText('review__task: Review the change')).toBeInTheDocument();
    expect(buildActivityOverview(rows).subagents).toEqual([]);
    cleanup();

    const step = buildTrajectorySteps(events).find((item) => item.callId === 'mcp-1')!;
    render(<ToolStepDetails step={step} tab="summary" onSelectTab={vi.fn()} />);
    expect(screen.queryByRole('region', { name: 'Subagent reply' })).not.toBeInTheDocument();
    expect(screen.queryByText(/The agent.s reply was not recorded/)).not.toBeInTheDocument();
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

  it('links a Codex subagent thread’s records under the spawn that started it', () => {
    const owned = { agent_thread_id: 'child-t', parent_tool_use_id: 'call-spawn' };
    const spawn = {
      type: 'collabAgentToolCall',
      id: 'call-spawn',
      tool: 'spawnAgent',
      prompt: 'CHILD-TASK',
      receiverThreadIds: ['child-t'],
    };
    function notification(id: number, thread: string, method: string, extra: object) {
      return event(id, 'raw', {
        protocol: 'codex-app-server',
        notification: { method, params: { threadId: thread, ...extra } },
        ...(thread === 'main' ? {} : { agent_thread_id: thread }),
      });
    }
    const steps = buildTrajectorySteps(
      [
        notification(1, 'main', 'item/started', { item: { ...spawn, status: 'inProgress' } }),
        notification(2, 'child-t', 'thread/status/changed', { status: { type: 'active' } }),
        notification(3, 'main', 'item/completed', { item: { ...spawn, status: 'completed' } }),
        {
          ...notification(4, 'child-t', 'item/completed', {
            item: {
              type: 'userMessage',
              id: 'child-in',
              content: [{ type: 'text', text: 'CHILD-TASK' }],
            },
          }),
          payload: {
            ...notification(4, 'child-t', 'item/completed', {
              item: {
                type: 'userMessage',
                id: 'child-in',
                content: [{ type: 'text', text: 'CHILD-TASK' }],
              },
            }).payload,
            ...owned,
          },
        },
        event(5, 'message', { text: 'CHILD-ANSWER', native_item_id: 'child-msg', ...owned }),
        event(6, 'message', { text: 'PARENT-DONE', native_item_id: 'main-msg' }),
      ],
      {
        isAttemptActive: () => false,
        inputs: [{ jobId: 'job', turnNo: 1, text: 'delegate', at: null }],
      },
    );
    const parent = steps.find((step) => step.callId === 'call-spawn')!;
    const child = steps.find((step) => step.detail === 'CHILD-ANSWER')!;
    expect(child.delegationParentKey).toBe(parent.key);
    expect(child.label).toBe('Subagent message');
    expect(parent.delegatedKeys).toEqual([child.key]);
    const user = steps.filter((step) => step.kind === 'user');
    expect(user.map((step) => step.inputText)).toEqual(['delegate']);
    expect(user[0].sourceEvents).toHaveLength(0);
    expect(steps.filter((step) => step.delegationParentKey).map((step) => step.key)).toEqual([
      child.key,
    ]);
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

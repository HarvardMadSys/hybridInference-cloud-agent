import { describe, expect, it } from 'vitest';

import { buildActivityOverview, hasActivityOverview } from './activityOverview';
import { EMPTY_TOOL_OUTPUT } from './adapt';
import type { AgentEvent } from './types';

type ToolUseEvent = Extract<AgentEvent, { kind: 'tool_use' }>;

function tool(name: string, input: unknown, overrides: Partial<ToolUseEvent> = {}): ToolUseEvent {
  return { kind: 'tool_use', tool: name, detail: name, input, attemptNo: 1, ...overrides };
}

function result(value: unknown): Pick<ToolUseEvent, 'resultState' | 'output'> {
  return { resultState: 'received', output: [JSON.stringify(value)] };
}

describe('task overview', () => {
  it('merges recorded task mutations by ID while preserving omitted fields', () => {
    const overview = buildActivityOverview([
      tool('TaskCreate', {
        id: '1',
        subject: 'Implement the UI',
        description: 'Add accessible details.',
        owner: 'builder',
        blockedBy: ['2'],
        blocks: ['3'],
      }),
      tool('TaskUpdate', { taskId: '1', status: 'in_progress', addBlockedBy: ['4', '2'] }),
      tool('TaskUpdate', { taskId: '1', addBlocks: ['5'] }),
      tool('TaskGet', { taskId: '1' }, result({ task: { id: '1', status: 'completed' } })),
    ]);

    expect(overview.taskActivity).toEqual({
      kind: 'tasks',
      title: 'Tasks',
      tasks: [
        {
          id: '1',
          subject: 'Implement the UI',
          description: 'Add accessible details.',
          owner: 'builder',
          status: 'completed',
          blockedBy: ['2', '4'],
          blocks: ['3', '5'],
        },
      ],
    });
  });

  it('uses a structured create result ID without adding an anonymous duplicate', () => {
    const overview = buildActivityOverview([
      tool('TaskCreate', { subject: 'Review the change' }, result({ task: { id: 7 } })),
      tool('TaskUpdate', { taskId: '7', status: 'in_progress' }),
    ]);

    expect(overview.taskActivity?.tasks).toMatchObject([
      { id: '7', subject: 'Review the change', status: 'in_progress' },
    ]);
    expect(overview.taskActivity?.tasks).toHaveLength(1);
  });

  it('reads a single structured task result and preserves the requested subject', () => {
    expect(
      buildActivityOverview([
        tool('TaskCreate', { subject: 'Run checks' }, result({ id: '9', status: 'pending' })),
      ]).taskActivity?.tasks,
    ).toMatchObject([{ id: '9', subject: 'Run checks', status: 'pending' }]);
  });

  it('links a native text create acknowledgement to later task updates by its explicit ID', () => {
    const overview = buildActivityOverview([
      tool(
        'TaskCreate',
        { subject: 'Run checks', description: 'Validate the settings JSON.' },
        { resultState: 'received', output: ['Task #9 created successfully: Run checks'] },
      ),
      tool('TaskUpdate', { taskId: '9', status: 'completed' }),
    ]);

    expect(overview.taskActivity?.tasks).toMatchObject([
      {
        id: '9',
        subject: 'Run checks',
        description: 'Validate the settings JSON.',
        status: 'completed',
      },
    ]);
    expect(overview.taskActivity?.tasks).toHaveLength(1);
  });

  it('does not derive task completion from a native creation acknowledgement', () => {
    expect(
      buildActivityOverview([
        tool(
          'TaskCreate',
          { subject: 'Run checks' },
          { resultState: 'received', output: ['Task #9 created successfully: Run checks'] },
        ),
      ]).taskActivity?.tasks,
    ).toMatchObject([{ id: '9', subject: 'Run checks', status: undefined }]);
  });

  it('prefers the recorded structured result over a legacy text acknowledgement', () => {
    expect(
      buildActivityOverview([
        tool(
          'TaskCreate',
          { subject: 'Run checks' },
          {
            resultState: 'received',
            structuredOutput: { task: { id: '7', status: 'pending' } },
            output: ['Task #9 created successfully: Run checks'],
          },
        ),
        tool('TaskUpdate', { taskId: '7', status: 'completed' }),
      ]).taskActivity?.tasks,
    ).toMatchObject([{ id: '7', subject: 'Run checks', status: 'completed' }]);
  });

  it('keeps identically named creations distinct by their acknowledged task IDs', () => {
    expect(
      buildActivityOverview([
        tool(
          'TaskCreate',
          { subject: 'Run checks' },
          { output: ['Task #7 created successfully: Run checks'] },
        ),
        tool(
          'TaskCreate',
          { subject: 'Run checks' },
          { output: ['Task #9 created successfully: Run checks'] },
        ),
        tool('TaskUpdate', { taskId: '9', status: 'completed' }),
      ]).taskActivity?.tasks,
    ).toMatchObject([
      { id: '7', subject: 'Run checks', status: undefined },
      { id: '9', subject: 'Run checks', status: 'completed' },
    ]);
  });

  it.each([
    'Task created successfully.',
    'Task #9 created successfully: Another task',
    'Log output\nTask #9 created successfully: Run checks',
    'Task #9 created successfully: Run checks\nAll tasks completed',
    'Task #9 [completed] Run checks',
  ])('does not guess task identity from an unrecognized acknowledgement: %s', (output) => {
    const overview = buildActivityOverview([
      tool('TaskCreate', { subject: 'Run checks' }, { resultState: 'received', output: [output] }),
      tool('TaskUpdate', { taskId: '9', status: 'completed' }),
    ]);
    expect(overview.taskActivity?.tasks).toMatchObject([
      { subject: 'Run checks', status: undefined },
      { id: '9', subject: 'Task #9', status: 'completed' },
    ]);
    expect(overview.taskActivity?.tasks[0].id).toBeUndefined();
  });

  it.each(['waiting', 'not_recorded'] as const)(
    'does not consume a text creation result while the call is %s',
    (resultState) => {
      const overview = buildActivityOverview([
        tool(
          'TaskCreate',
          { subject: 'Run checks' },
          {
            resultState,
            output: ['Task #9 created successfully: Run checks'],
            structuredOutput: { task: { id: '9', status: 'pending' } },
          },
        ),
      ]);
      expect(overview.taskActivity?.tasks).toMatchObject([{ subject: 'Run checks' }]);
      expect(overview.taskActivity?.tasks[0].id).toBeUndefined();
    },
  );

  it('replaces earlier full snapshots and clears a recorded empty snapshot', () => {
    const events = [
      tool('TodoWrite', { todos: [{ content: 'Old work', status: 'completed' }] }),
      tool('TaskCreate', { id: '1', subject: 'Extra work' }),
      tool('functions.update_plan', {
        explanation: 'Focus on the UI.',
        plan: [{ step: 'Build overview', status: 'in_progress' }],
      }),
    ];
    expect(buildActivityOverview(events).taskActivity).toMatchObject({
      title: 'Plan',
      explanation: 'Focus on the UI.',
      tasks: [{ subject: 'Build overview', status: 'in_progress' }],
    });
    expect(buildActivityOverview([...events, tool('TaskList', {}, result({ tasks: [] }))])).toEqual(
      { subagents: [] },
    );
  });

  it('uses recorded TaskList results as a full replacement', () => {
    expect(
      buildActivityOverview([
        tool('TaskCreate', { id: '1', subject: 'Old' }),
        tool('TaskList', {}, result([{ id: '2', subject: 'Current', status: 'needs_review' }])),
      ]).taskActivity?.tasks,
    ).toMatchObject([{ id: '2', subject: 'Current', status: 'needs_review' }]);
  });

  it('applies explicit clears and deletes without inferring a completed status', () => {
    const overview = buildActivityOverview([
      tool('TaskCreate', { id: '1', subject: 'First', owner: 'builder', blockedBy: ['2'] }),
      tool('TaskCreate', { id: '2', subject: 'Second' }),
      tool('TaskUpdate', { taskId: '1', owner: '', blockedBy: [] }),
      tool('TaskUpdate', { taskId: '2', status: 'deleted' }),
    ]);
    expect(overview.taskActivity?.tasks).toEqual([
      {
        id: '1',
        subject: 'First',
        status: undefined,
        description: undefined,
        owner: undefined,
        blockedBy: [],
        blocks: [],
      },
    ]);
  });

  it('preserves a deletion when the same call result acknowledges only the task ID', () => {
    expect(
      buildActivityOverview([
        tool('TaskCreate', { id: '1', subject: 'Remove me' }),
        tool(
          'TaskUpdate',
          { taskId: '1', status: 'deleted' },
          result({
            success: true,
            taskId: '1',
          }),
        ),
      ]).taskActivity,
    ).toBeUndefined();
  });

  it.each(['failed', 'cancelled', 'unknown'] as const)(
    'ignores %s task updates and replacements',
    (resultState) => {
      const overview = buildActivityOverview([
        tool('TaskCreate', { id: '1', subject: 'Keep', status: 'pending' }),
        tool('TaskUpdate', { taskId: '1', status: 'completed' }, { resultState }),
        tool('TodoWrite', { todos: [] }, { resultState }),
      ]);
      expect(overview.taskActivity?.tasks).toMatchObject([
        { id: '1', subject: 'Keep', status: 'pending' },
      ]);
    },
  );

  it('ignores errored output but displays waiting requests without consuming their output', () => {
    const overview = buildActivityOverview([
      tool(
        'TodoWrite',
        { todos: [{ content: 'Request', status: 'pending' }] },
        {
          resultState: 'waiting',
          output: [JSON.stringify({ todos: [] })],
        },
      ),
      tool('TaskUpdate', { taskId: '1', status: 'completed' }, { outputIsError: true }),
    ]);
    expect(overview.taskActivity?.tasks).toMatchObject([{ subject: 'Request', status: 'pending' }]);
  });

  it('does not promote child plans or invent a list from opaque and malformed data', () => {
    const overview = buildActivityOverview([
      tool('TodoWrite', { todos: [{ content: 'Main task' }] }),
      tool('TodoWrite', { todos: [{ content: 'Child task' }] }, { parentToolUseId: 'delegate' }),
      tool('TaskList', {}, { ...result('All tasks completed') }),
      tool('update_plan', { plan: 'Nothing left' }),
      tool('TaskGet', {}, result({ tasks: [null, { status: 'completed' }] })),
      tool('TaskUpdate', { subject: 'An unidentified update' }),
    ]);
    expect(overview.taskActivity?.tasks).toMatchObject([{ subject: 'Main task' }]);
    expect(overview.taskActivity?.tasks).toHaveLength(1);
  });

  it('leaves the overview empty for updates that do not identify a task', () => {
    expect(buildActivityOverview([tool('TaskUpdate', { subject: 'Missing ID' })])).toEqual({
      subagents: [],
    });
  });

  it('keeps reused task IDs from separate attempts distinct', () => {
    const overview = buildActivityOverview([
      tool('TaskCreate', { id: '1', subject: 'First attempt' }),
      tool('TaskUpdate', { taskId: '1', status: 'in_progress' }, { attemptNo: 2 }),
    ]);
    expect(overview.taskActivity?.tasks).toMatchObject([
      { id: '1', subject: 'First attempt', status: undefined },
      { id: '1', subject: 'Task #1', status: 'in_progress' },
    ]);
  });
});

describe('subagent overview', () => {
  it('consolidates launch and later state reports by explicit agent ID', () => {
    const launch = tool(
      'spawn_agent',
      {
        task_name: 'Review the API',
        model: 'review-model',
        prompt: 'Inspect the request schema.',
      },
      { sourceEventId: 10, ...result({ agent_id: 'reviewer' }) },
    );
    const wait = tool(
      'wait',
      { ids: ['reviewer'] },
      {
        sourceEventId: 11,
        ...result({ status: { reviewer: { completed: 'No issues found.' } } }),
      },
    );
    const overview = buildActivityOverview([launch, wait]);
    expect(overview.subagents).toHaveLength(1);
    expect(overview.subagents[0]).toMatchObject({
      activity: {
        title: 'Start subagent',
        description: 'Review the API',
        model: 'review-model',
        prompt: 'Inspect the request schema.',
        targets: ['reviewer'],
        states: [{ id: 'reviewer', status: 'completed', message: 'No issues found.' }],
      },
    });
    expect(overview.subagents[0].sourceEvent).toBe(launch);
    expect(overview.subagents[0].lastEvent).toBe(wait);
  });

  it('uses the latest recorded state for each targeted agent without mixing their work', () => {
    const overview = buildActivityOverview([
      tool('spawn_agent', { prompt: 'Review' }, result({ agent_id: 'a' })),
      tool('spawn_agent', { prompt: 'Implement' }, result({ agent_id: 'b' })),
      tool(
        'wait',
        {
          receiverThreadIds: ['a', 'b'],
          agentsStates: { a: { status: 'running' }, b: { status: 'pending_init' } },
        },
        result({ agentsStates: { a: { status: 'paused' } } }),
      ),
    ]);
    expect(overview.subagents.map((row) => row.activity)).toMatchObject([
      { prompt: 'Review', targets: ['a'], states: [{ id: 'a', status: 'paused' }] },
      { prompt: 'Implement', targets: ['b'], states: [{ id: 'b', status: 'pending_init' }] },
    ]);
  });

  it('never derives agent completion from a successful launch or wait call', () => {
    const overview = buildActivityOverview([
      tool('spawn_agent', { prompt: 'Review' }, result({ agent_id: 'a' })),
      tool('wait', { ids: ['a'] }, { resultState: 'received', output: ['Success'] }),
    ]);
    expect(overview.subagents).toHaveLength(1);
    expect(overview.subagents[0].activity.states).toEqual([]);
  });

  it('preserves earlier agent states when a later operation records no new state', () => {
    const overview = buildActivityOverview([
      tool('spawn_agent', {
        receiverThreadIds: ['a'],
        agentsStates: { a: { status: 'running' } },
      }),
      tool('send_input', { id: 'a', message: 'Also check the tests.' }, { resultState: 'waiting' }),
    ]);
    expect(overview.subagents[0].activity).toMatchObject({
      prompt: 'Also check the tests.',
      promptLabel: 'Message',
      states: [{ id: 'a', status: 'running' }],
    });
  });

  it.each(['failed', 'cancelled', 'unknown'] as const)(
    'ignores %s subagent updates',
    (resultState) => {
      const overview = buildActivityOverview([
        tool('spawn_agent', {
          receiverThreadIds: ['a'],
          agentsStates: { a: { status: 'running' } },
        }),
        tool(
          'close_agent',
          { id: 'a' },
          {
            resultState,
            output: [JSON.stringify({ status: { a: 'shutdown' } })],
          },
        ),
      ]);
      expect(overview.subagents).toHaveLength(1);
      expect(overview.subagents[0].activity.states).toEqual([{ id: 'a', status: 'running' }]);
    },
  );

  it.each(['failed', 'cancelled'] as const)(
    'lists a %s launch on its own without the identity its output claims',
    (resultState) => {
      const overview = buildActivityOverview([
        tool('spawn_agent', {
          receiverThreadIds: ['a'],
          agentsStates: { a: { status: 'running' } },
        }),
        tool(
          'spawn_agent',
          { prompt: 'Failed launch', agentsStates: { b: { status: 'running' } } },
          {
            sourceEventId: 2,
            resultState,
            output: [JSON.stringify({ agent_id: 'b' })],
          },
        ),
      ]);
      expect(overview.subagents).toHaveLength(2);
      expect(overview.subagents[0].activity.states).toEqual([{ id: 'a', status: 'running' }]);
      expect(overview.subagents[1]).toMatchObject({
        activity: { prompt: 'Failed launch', targets: [], states: [] },
        reply: { state: resultState },
      });
    },
  );

  it('ignores a launch whose records disagree', () => {
    const overview = buildActivityOverview([
      tool('Agent', { prompt: 'Review' }, { resultState: 'unknown', output: ['Reviewed'] }),
    ]);
    expect(overview.subagents).toEqual([]);
  });

  it('reads a delegated answer as the text of the content blocks its call returned', () => {
    const overview = buildActivityOverview([
      tool(
        'Agent',
        { description: 'Inspect the workspace', subagent_type: 'Explore', prompt: 'Inspect.' },
        {
          resultState: 'received',
          output: [
            JSON.stringify([
              { type: 'text', text: '## Findings' },
              { type: 'image', source: {} },
              { type: 'text', text: '- inspected' },
            ]),
          ],
          structuredOutput: { status: 'completed', agentId: 'aa9397b543b5f2ff7' },
        },
      ),
    ]);
    expect(overview.subagents).toHaveLength(1);
    expect(overview.subagents[0].reply).toEqual({
      state: 'received',
      status: 'completed',
      text: '## Findings\n\n- inspected',
    });
    expect(overview.subagents[0].activity).toMatchObject({ targets: [], states: [] });
  });

  it('keeps a plain result verbatim and claims no status the runtime did not record', () => {
    const overview = buildActivityOverview([
      tool(
        'task',
        { description: 'Review', prompt: 'Review the change.' },
        { resultState: 'received', output: ['First line', '[not, blocks]'] },
      ),
    ]);
    expect(overview.subagents[0].reply).toEqual({
      state: 'received',
      text: 'First line\n[not, blocks]',
    });
  });

  it('does not present a background launch notice as the reply', () => {
    const notice = JSON.stringify([{ type: 'text', text: 'Async agent launched successfully.' }]);
    const overview = buildActivityOverview([
      tool(
        'Agent',
        { description: 'Requested', prompt: 'Review.', run_in_background: true },
        { sourceEventId: 1, resultState: 'received', output: [notice] },
      ),
      tool(
        'Agent',
        { description: 'Reported', prompt: 'Review.' },
        {
          sourceEventId: 2,
          resultState: 'received',
          output: [notice],
          structuredOutput: { isAsync: true, status: 'async_launched' },
        },
      ),
    ]);
    expect(overview.subagents.map((row) => row.reply)).toEqual([
      { state: 'background' },
      { state: 'background', status: 'async_launched' },
    ]);
  });

  it('says when a reply is still pending or was never recorded', () => {
    const overview = buildActivityOverview([
      tool('Agent', { prompt: 'Live' }, { sourceEventId: 1, resultState: 'waiting' }),
      tool('Agent', { prompt: 'Ended' }, { sourceEventId: 2, resultState: 'not_recorded' }),
      tool('Agent', { prompt: 'Unpaired' }, { sourceEventId: 3 }),
    ]);
    expect(overview.subagents.map((row) => row.reply)).toEqual([
      { state: 'waiting' },
      { state: 'not_recorded' },
      { state: 'not_recorded' },
    ]);
  });

  it('keeps the error a failed delegation returned and reads no text from an empty result', () => {
    const overview = buildActivityOverview([
      tool(
        'Agent',
        { prompt: 'Review' },
        {
          sourceEventId: 1,
          resultState: 'failed',
          outputIsError: true,
          output: ['Agent type not found'],
        },
      ),
      tool(
        'Agent',
        { prompt: 'Quiet' },
        { sourceEventId: 2, resultState: 'received', output: [EMPTY_TOOL_OUTPUT] },
      ),
      tool('Agent', { prompt: 'Blank' }, { sourceEventId: 3, ...result([]) }),
    ]);
    expect(overview.subagents.map((row) => row.reply)).toEqual([
      { state: 'failed', text: 'Agent type not found' },
      { state: 'received' },
      { state: 'received' },
    ]);
  });

  it('reads no reply from a launch whose result only acknowledges it', () => {
    const overview = buildActivityOverview([
      tool('spawn_agent', { prompt: 'Review' }, result({ agent_id: 'a' })),
      tool('wait', { ids: ['a'] }, result({ status: { a: { completed: 'No issues found.' } } })),
    ]);
    expect(overview.subagents).toHaveLength(1);
    expect(overview.subagents[0].reply).toBeUndefined();
  });

  it('keeps unidentified launches separate rather than merging equal descriptions', () => {
    const overview = buildActivityOverview([
      tool('Agent', { description: 'Review', prompt: 'Check the change.' }, { sourceEventId: 1 }),
      tool('Agent', { description: 'Review', prompt: 'Check the change.' }, { sourceEventId: 2 }),
    ]);
    expect(overview.subagents).toHaveLength(2);
    expect(new Set(overview.subagents.map((row) => row.key)).size).toBe(2);
    expect(overview.subagents.map((row) => row.activity.targets)).toEqual([[], []]);
  });

  it('scopes reused agent IDs to their recorded attempts', () => {
    const overview = buildActivityOverview([
      tool('spawn_agent', { prompt: 'First' }, result({ agent_id: 'same' })),
      tool('spawn_agent', { prompt: 'Second' }, { attemptNo: 2, ...result({ agent_id: 'same' }) }),
      tool(
        'wait',
        { ids: ['same'] },
        { attemptNo: 2, ...result({ status: { same: 'completed' } }) },
      ),
    ]);
    expect(overview.subagents).toHaveLength(2);
    expect(overview.subagents.map((row) => row.activity)).toMatchObject([
      { prompt: 'First', states: [] },
      { prompt: 'Second', states: [{ id: 'same', status: 'completed' }] },
    ]);
    expect(overview.subagents[0].key).not.toBe(overview.subagents[1].key);
  });

  it('reads explicit states even without a target list, preserving unfamiliar statuses', () => {
    const overview = buildActivityOverview([
      tool('list_agents', {}, result({ agentsStates: { a: { status: 'needs_review' } } })),
    ]);
    expect(overview.subagents[0].activity).toMatchObject({
      targets: ['a'],
      states: [{ id: 'a', status: 'needs_review' }],
    });
  });

  it('does not turn an unidentified control call into a subagent', () => {
    expect(
      buildActivityOverview([
        tool('list_agents', {}, result({ agentsStates: {} })),
        tool('list_agents', {}, result({ agentsStates: { '': 'running', ' ': 'completed' } })),
        tool('close_agent', {}),
        tool('resume_agent', {}),
      ]),
    ).toEqual({ subagents: [] });
  });

  it('does not turn a blank launch call into a subagent', () => {
    expect(buildActivityOverview([tool('spawn_agent', {})])).toEqual({ subagents: [] });
  });

  it('links to the first recorded launch when an earlier state report introduced the ID', () => {
    const launch = tool('spawn_agent', { prompt: 'Review' }, result({ agent_id: 'a' }));
    const overview = buildActivityOverview([
      tool('list_agents', {}, result({ agentsStates: { a: { status: 'running' } } })),
      launch,
    ]);
    expect(overview.subagents[0].sourceEvent).toBe(launch);
    expect(overview.subagents[0].activity).toMatchObject({
      title: 'Start subagent',
      prompt: 'Review',
      states: [{ id: 'a', status: 'running' }],
    });
  });

  it('does not mutate recorded inputs, events or outputs', () => {
    const events = [
      tool('TaskCreate', { subject: 'Build' }, result({ task: { id: '1' } })),
      tool('spawn_agent', { prompt: 'Review' }, result({ agent_id: 'a' })),
      tool('wait', { ids: ['a'] }, result({ agentsStates: { a: { status: 'completed' } } })),
    ];
    const original = JSON.stringify(events);
    buildActivityOverview(events);
    expect(JSON.stringify(events)).toBe(original);
  });

  it('ignores unrelated events and opaque output rather than interpreting prose as IDs', () => {
    expect(buildActivityOverview([{ kind: 'message', text: 'Agent a completed.' }])).toEqual({
      subagents: [],
    });
    const overview = buildActivityOverview([
      tool('Agent', { description: 'Review' }, { output: ['Agent a completed.'] }),
      tool('Bash', { command: 'echo tasks' }),
      tool('functions.wait', { cell_id: '17' }),
    ]);
    expect(overview.subagents).toHaveLength(1);
    expect(overview.subagents[0].activity).toMatchObject({ targets: [], states: [] });
  });
});

describe('overview visibility', () => {
  it('stays empty for empty task and subagent snapshots', () => {
    const overview = buildActivityOverview([
      tool('update_plan', { plan: [] }),
      tool('list_agents', {}, result({ agentsStates: {} })),
    ]);

    expect(overview).toEqual({ subagents: [] });
    expect(hasActivityOverview(overview)).toBe(false);
  });

  it('becomes nonempty when structured output identifies a subagent', () => {
    const overview = buildActivityOverview([
      tool('spawn_agent', {}, result({ agent_id: 'reviewer' })),
    ]);

    expect(overview.subagents).toHaveLength(1);
    expect(hasActivityOverview(overview)).toBe(true);
  });
});

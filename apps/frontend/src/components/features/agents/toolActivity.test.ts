import { describe, expect, it } from 'vitest';
import {
  parseToolActivity,
  recordedAgentStates,
  recordedDelegationReply,
  recordedTasks,
} from './toolActivity';

describe('task tool arguments', () => {
  it('reads namespaced plans and preserves missing and unfamiliar statuses', () => {
    expect(
      parseToolActivity(
        'functions.update_plan',
        JSON.stringify({
          explanation: 'Check the API before implementing the UI.',
          plan: [
            { step: 'Inspect API', status: 'completed' },
            { step: 'Implement UI', status: 'in_progress' },
            { step: 'Review' },
            { step: 'Ship', status: 'needs_review' },
            { status: 'completed' },
            null,
          ],
        }),
      ),
    ).toEqual({
      kind: 'tasks',
      title: 'Plan',
      explanation: 'Check the API before implementing the UI.',
      tasks: [
        expect.objectContaining({ subject: 'Inspect API', status: 'completed' }),
        expect.objectContaining({ subject: 'Implement UI', status: 'in_progress' }),
        expect.objectContaining({ subject: 'Review', status: undefined }),
        expect.objectContaining({ subject: 'Ship', status: 'needs_review' }),
      ],
    });
  });

  it('reads Claude checklists and task mutations without inventing a status', () => {
    expect(
      parseToolActivity('mcp__tasks__TodoWrite', {
        todos: [{ content: 'Read the code', status: 'pending' }],
      }),
    ).toMatchObject({ kind: 'tasks', tasks: [{ subject: 'Read the code', status: 'pending' }] });
    expect(
      parseToolActivity('TaskCreate', {
        subject: 'Review the change',
        description: 'Check accessibility.',
      }),
    ).toMatchObject({
      kind: 'tasks',
      title: 'Create task',
      tasks: [{ subject: 'Review the change', status: undefined }],
    });
    expect(
      parseToolActivity('TaskUpdate', {
        taskId: '7',
        status: 'in_progress',
        owner: 'reviewer',
        addBlockedBy: ['2', 3, '2'],
        addBlocks: ['9'],
      }),
    ).toMatchObject({
      kind: 'tasks',
      title: 'Update task',
      tasks: [
        {
          id: '7',
          subject: 'Task #7',
          status: 'in_progress',
          owner: 'reviewer',
          blockedBy: ['2', '3'],
          blocks: ['9'],
        },
      ],
    });
  });

  it('reads structured task lists while ignoring unstructured result text', () => {
    expect(parseToolActivity('TaskList', {})).toMatchObject({ kind: 'tasks', tasks: [] });
    expect(
      recordedTasks(
        JSON.stringify({
          tasks: [{ id: 1, subject: 'Test', status: 'completed', blockedBy: ['3'] }],
        }),
      ),
    ).toMatchObject([{ id: '1', subject: 'Test', status: 'completed', blockedBy: ['3'] }]);
    expect(recordedTasks('#1 [completed] Test')).toEqual([]);
    expect(recordedTasks({ tasks: 'All tasks completed' })).toEqual([]);
  });
});

describe('delegation tool arguments', () => {
  it('reads recorded Claude delegation instructions, including false background flags', () => {
    expect(
      parseToolActivity('Agent', {
        description: 'Review the API',
        subagent_type: 'Explore',
        model: 'sonnet',
        prompt: 'Find the request schema.',
        run_in_background: false,
      }),
    ).toMatchObject({
      kind: 'delegation',
      description: 'Review the API',
      agentType: 'Explore',
      model: 'sonnet',
      prompt: 'Find the request schema.',
      background: false,
      targets: [],
      states: [],
    });
    expect(parseToolActivity('Task', { resume: 'agent_1', run_in_background: true })).toMatchObject(
      { kind: 'delegation', targets: ['agent_1'], background: true },
    );
  });

  it('recognizes native and namespaced collaboration actions without guessing completion', () => {
    expect(
      parseToolActivity('collaboration.spawn_agent', {
        task_name: 'review',
        message: 'Read the diff.',
      }),
    ).toMatchObject({
      kind: 'delegation',
      description: 'review',
      prompt: 'Read the diff.',
      states: [],
    });
    expect(
      parseToolActivity('functions.send_input', { id: 'agent_1', message: 'Check the tests.' }),
    ).toMatchObject({ kind: 'delegation', promptLabel: 'Message', targets: ['agent_1'] });
    expect(
      parseToolActivity('functions.send_message', {
        target: '/root/review',
        message: 'Check tests.',
      }),
    ).toMatchObject({ kind: 'delegation', targets: ['/root/review'] });
    expect(parseToolActivity('wait', { receiver_thread_ids: ['a', 'b', 'a'] })).toMatchObject({
      kind: 'delegation',
      targets: ['a', 'b'],
      states: [],
    });
    for (const name of ['close_agent', 'resume_agent']) {
      expect(parseToolActivity(name, { id: 'a' })).toMatchObject({
        kind: 'delegation',
        targets: ['a'],
        states: [],
      });
    }
  });

  it.each([
    'task',
    'agent',
    'spawn_agent',
    'send_message',
    'send_input',
    'wait',
    'close_agent',
    'resume_agent',
    'followup_task',
    'interrupt_agent',
    'list_agents',
  ])('never treats an MCP tool ending in %s as native delegation', (suffix) => {
    expect(
      parseToolActivity(`mcp__review__${suffix}`, {
        id: 'worker_1',
        target: 'worker_1',
        agent_id: 'worker_1',
        receiver_thread_ids: ['worker_1'],
        description: 'Review this change.',
        message: 'Review this change.',
      }),
    ).toBeNull();
  });

  it('reads explicit native agent state maps and retains unfamiliar recorded statuses', () => {
    expect(
      recordedAgentStates({
        agents_states: {
          a: { status: 'running' },
          b: { status: 'completed', message: 'Tests pass.' },
          c: { status: 'paused' },
          d: { message: 'No status' },
        },
      }),
    ).toEqual([
      { id: 'a', status: 'running' },
      { id: 'b', status: 'completed', message: 'Tests pass.' },
      { id: 'c', status: 'paused' },
    ]);
    expect(
      recordedAgentStates(
        JSON.stringify({ status: { a: { completed: 'Finished.' }, b: 'shutdown' } }),
      ),
    ).toEqual([
      { id: 'a', status: 'completed', message: 'Finished.' },
      { id: 'b', status: 'shutdown' },
    ]);
    expect(recordedAgentStates('Agent completed successfully')).toEqual([]);
    expect(recordedAgentStates({ success: true })).toEqual([]);
  });

  it('leaves unrelated tools alone and tolerates partial or malformed arguments', () => {
    expect(parseToolActivity('Bash', { command: 'echo Task' })).toBeNull();
    expect(parseToolActivity('functions.wait', { cell_id: '17' })).toBeNull();
    expect(parseToolActivity('wait', { session_id: '17' })).toBeNull();
    expect(parseToolActivity('plan', { query: 'SELECT * FROM tasks' })).toBeNull();
    expect(parseToolActivity('tasks', {})).toBeNull();
    expect(parseToolActivity('mcp__slack__send_message', { target: 'channel' })).toBeNull();
    expect(parseToolActivity('constructor', {})).toBeNull();
    expect(parseToolActivity('__proto__', {})).toBeNull();
    expect(parseToolActivity('ReadTaskFile', {})).toBeNull();
    expect(parseToolActivity('spawn_agent', '{broken')).toMatchObject({
      kind: 'delegation',
      targets: [],
      states: [],
    });
    expect(parseToolActivity('TaskUpdate', null)).toMatchObject({ kind: 'tasks', tasks: [] });
  });
});

describe('a delegated agent’s recorded reply', () => {
  // As the pinned OpenCode 1.18.9 and Kilo 7.4.17 render a task's result.
  function envelope(state: string, body: string, tag = 'task_result', summary?: string): string {
    return [
      `<task id="ses_child" state="${state}">`,
      ...(summary ? [`<summary>${summary}</summary>`] : []),
      `<${tag}>`,
      body,
      `</${tag}>`,
      '</task>',
    ].join('\n');
  }

  it('reads the answer inside an OpenCode or Kilo task envelope', () => {
    expect(
      recordedDelegationReply({
        tool: 'task',
        input: { description: 'Review', prompt: 'Review.', subagent_type: 'general' },
        resultState: 'received',
        output: envelope('completed', '## Findings\n\n- one'),
      }),
    ).toEqual({ state: 'received', text: '## Findings\n\n- one' });
  });

  it('says a task envelope is cut short when its closing tags were not stored', () => {
    const whole = envelope('completed', 'Line one\nLine two');
    expect(
      recordedDelegationReply({
        tool: 'task',
        input: {},
        resultState: 'received',
        output: whole.slice(0, whole.indexOf('Line two') + 4),
      }),
    ).toEqual({ state: 'received', text: 'Line one\nLine', truncated: true });
  });

  it('treats a running envelope as a background notice and an error envelope as a failure', () => {
    expect(
      recordedDelegationReply({
        tool: 'task',
        input: { background: true },
        resultState: 'received',
        output: envelope('running', 'Working in the background.', 'task_result', 'Started'),
      }),
    ).toEqual({ state: 'background' });
    expect(
      recordedDelegationReply({
        tool: 'task',
        input: {},
        resultState: 'received',
        output: envelope('error', 'Provider failed.', 'task_error'),
      }),
    ).toEqual({ state: 'failed', text: 'Provider failed.' });
  });

  it('does not call a Claude reply cut short when only another field of its event was', () => {
    const blocks = JSON.stringify([{ type: 'text', text: 'Complete answer' }]);
    const notice = '\n\n[Content truncated: event payload exceeded 256 KiB.]';
    expect(
      recordedDelegationReply({
        tool: 'Agent',
        input: {},
        resultState: 'received',
        output: blocks + notice,
        outputTruncated: true,
      }),
    ).toEqual({ state: 'received', text: 'Complete answer' });
    expect(
      recordedDelegationReply({
        tool: 'Agent',
        input: {},
        resultState: 'received',
        output: blocks.slice(0, 20) + notice,
        outputTruncated: true,
      }),
    ).toMatchObject({ state: 'received', truncated: true });
  });

  it('claims nothing for calls whose result is not an answer, or whose records disagree', () => {
    expect(
      recordedDelegationReply({
        tool: 'spawn_agent',
        input: {},
        resultState: 'received',
        output: '{}',
      }),
    ).toBeUndefined();
    expect(
      recordedDelegationReply({ tool: 'Agent', input: {}, resultState: 'unknown', output: 'x' }),
    ).toBeUndefined();
  });
});

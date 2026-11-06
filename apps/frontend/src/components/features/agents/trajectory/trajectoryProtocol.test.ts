import { describe, expect, it } from 'vitest';
import type { AgentThreadEventApi } from '@/lib/api/agents';
import { buildTrajectorySteps, turnContextFromJobs } from './trajectorySteps';
import { toSessionLog } from './trajectory';
import { projectProtocolEvents, record } from './trajectoryProtocol';

function event(
  id: number,
  event_type: string,
  payload: Record<string, unknown>,
  attempt_id = 1,
): AgentThreadEventApi {
  return {
    id,
    seq: id,
    job_id: 'job',
    turn_no: 1,
    attempt_id,
    event_type,
    payload,
    created_at: `2026-09-17T00:00:${String(id).padStart(2, '0')}.000Z`,
  };
}
function notification(id: number, method: string, params: Record<string, unknown>, attempt = 1) {
  const value = { method, params };
  return event(
    id,
    'raw',
    {
      text: JSON.stringify(value).slice(0, 4000),
      protocol: 'codex-app-server',
      notification: value,
    },
    attempt,
  );
}
const active = turnContextFromJobs([{ id: 'job', state: 'running', current_attempt_id: 1 }]);

function collabItem(overrides: Record<string, unknown> = {}) {
  return {
    type: 'collabAgentToolCall',
    id: 'delegate',
    tool: 'spawnAgent',
    status: 'inProgress',
    senderThreadId: 'main',
    receiverThreadIds: [],
    prompt: 'Inspect the parser',
    model: 'gpt-6',
    reasoningEffort: 'high',
    agentsStates: {},
    ...overrides,
  };
}

describe('Codex task and subagent evidence', () => {
  it('projects each committed plan update with stable identity and preserves raw exports', () => {
    const records = [
      notification(1, 'turn/plan/updated', {
        threadId: 'main',
        turnId: 'turn',
        explanation: 'Read before editing',
        plan: [
          { step: 'Read the parser', status: 'inProgress' },
          { step: 'Update rendering', status: 'pending' },
        ],
      }),
      notification(2, 'turn/plan/updated', {
        threadId: 'main',
        turnId: 'turn',
        explanation: null,
        plan: [{ step: 'Read the parser', status: 'completed' }],
      }),
    ];
    const projected = projectProtocolEvents(records);
    expect(projected.map((record) => record.event_type)).toEqual(['tool_use', 'tool_use']);
    expect(projected[0].payload).toMatchObject({
      id: 'codex-plan:turn:1',
      name: 'update_plan',
      native_tool_status: 'completed',
      native_tool_request: true,
      input: {
        explanation: 'Read before editing',
        plan: [
          { step: 'Read the parser', status: 'in_progress' },
          { step: 'Update rendering', status: 'pending' },
        ],
      },
    });
    const steps = buildTrajectorySteps(records, active);
    expect(steps.map((step) => step.status)).toEqual(['completed', 'completed']);
    expect(steps[0].key).toBe(buildTrajectorySteps(records.slice(0, 1), active)[0].key);
    expect(steps[0].parentKey).toBeUndefined();
    expect(steps[0].result).toBeUndefined();
    expect(steps[0].timing.durationMs).toBeNull();
    expect(steps.flatMap((step) => step.sourceEvents)).toEqual(records);
    expect(
      toSessionLog(records)
        .split('\n')
        .map((line) => JSON.parse(line)),
    ).toEqual(records);
  });

  it('keeps malformed and unsupported plan notices inspectable', () => {
    const records = [
      notification(1, 'turn/plan/updated', { plan: [{ step: 'Read', status: 'pending' }] }),
      notification(2, 'turn/plan/updated', { turnId: 'turn', plan: 'invalid' }),
      notification(3, 'turn/plan/updated', {
        turnId: 'turn',
        plan: [{ step: 'Read', status: 'futureStatus' }],
      }),
    ];
    expect(projectProtocolEvents(records)).toEqual(records);
  });

  it('pairs only native completion with a delegation and retains recorded target snapshots', () => {
    const started = notification(1, 'item/started', { item: collabItem() });
    const updated = notification(2, 'item/updated', {
      item: collabItem({
        receiverThreadIds: ['child'],
        agentsStates: { child: { status: 'running' } },
      }),
    });
    const partial = buildTrajectorySteps([started, updated], active);
    const tool = partial.find((step) => step.kind === 'tool')!;
    expect(tool.status).toBe('waiting');
    expect(tool.result).toBeUndefined();
    expect(tool.payloadValue).toMatchObject({
      prompt: 'Inspect the parser',
      receiverThreadIds: ['child'],
      agentsStates: { child: { status: 'running' } },
    });
    expect(partial.filter((step) => step.kind === 'tool')).toHaveLength(1);
    expect(partial.find((step) => step.diagnostic)?.sourceEvents).toEqual([updated]);

    const completed = notification(3, 'item/completed', {
      item: collabItem({
        status: 'completed',
        receiverThreadIds: ['child'],
        agentsStates: { child: { status: 'running', message: null } },
      }),
    });
    const records = [started, updated, completed];
    const result = buildTrajectorySteps(records, active).find((step) => step.kind === 'tool')!;
    expect(result.key).toBe(tool.key);
    expect(result.status).toBe('completed');
    expect(result.sourceEvents).toEqual([started, completed]);
    expect(JSON.parse(result.result!.text)).toEqual({
      status: 'completed',
      receiverThreadIds: ['child'],
      agentsStates: { child: { status: 'running', message: null } },
    });
    expect(result.parentKey).toBeUndefined();
    expect(records[0]).toEqual(started);
    expect(recordedItem(started).receiverThreadIds).toEqual([]);
    expect(
      toSessionLog(records)
        .split('\n')
        .map((line) => JSON.parse(line)),
    ).toEqual(records);
  });

  it.each([
    ['spawnAgent', 'spawn_agent'],
    ['sendInput', 'send_input'],
    ['resumeAgent', 'resume_agent'],
    ['wait', 'wait'],
    ['closeAgent', 'close_agent'],
    ['sendMessage', 'send_message'],
    ['followupTask', 'followup_task'],
    ['interruptAgent', 'interrupt_agent'],
    ['listAgents', 'list_agents'],
  ])('projects native %s with its recorded arguments', (tool, name) => {
    const [projected] = projectProtocolEvents([
      notification(1, 'item/started', { item: collabItem({ tool }) }),
    ]);
    expect(projected.event_type).toBe('tool_use');
    expect(projected.payload).toMatchObject({ name, input: { tool, model: 'gpt-6' } });
  });

  it.each([
    ['completed', 'completed', false],
    ['failed', 'failed', true],
    ['interrupted', 'cancelled', false],
  ])(
    'keeps completion-only %s evidence without inventing a call window',
    (status, expected, isError) => {
      const record = notification(1, 'item/completed', {
        item: collabItem({ status, receiverThreadIds: ['child'] }),
      });
      const [projected] = projectProtocolEvents([record]);
      expect(projected.event_type).toBe('tool_use');
      expect(projected.payload).toMatchObject({ native_tool_status: expected, input: { status } });
      const [step] = buildTrajectorySteps([record], active);
      expect(step.status).toBe(expected);
      expect(JSON.parse(step.result!.text)).toMatchObject({ status, receiverThreadIds: ['child'] });
      expect(step.result!.eventIds).toEqual([record.id]);
      expect(step.result!.isError).toBe(isError);
      expect(step.timing.durationMs).toBeNull();
      expect(step.timing.startedAt).toBeNull();
      expect(step.timing.finishedAt).toBeNull();
      expect(step.sourceEvents).toEqual([record]);
    },
  );

  it('preserves native delegation failure and interruption outcomes', () => {
    for (const [status, expected] of [
      ['failed', 'failed'],
      ['interrupted', 'cancelled'],
    ]) {
      const records = [
        notification(1, 'item/started', { item: collabItem() }),
        notification(2, 'item/completed', { item: collabItem({ status }) }),
      ];
      const [call, result] = projectProtocolEvents(records);
      expect(call.event_type).toBe('tool_use');
      expect(result.event_type).toBe('tool_result');
      expect(record(result.payload).is_error).toBe(status === 'failed');
      expect(buildTrajectorySteps(records, active)[0].status).toBe(expected);
    }
  });

  it('scopes snapshot merging and call pairing to the recorded job and attempt', () => {
    const records = [
      notification(1, 'item/started', { item: collabItem() }),
      notification(
        2,
        'item/updated',
        { item: collabItem({ receiverThreadIds: ['retry-child'] }) },
        2,
      ),
      {
        ...notification(3, 'item/completed', {
          item: collabItem({ status: 'completed', receiverThreadIds: ['other-job-child'] }),
        }),
        job_id: 'other-job',
      },
    ];
    const projected = projectProtocolEvents(records);
    expect(projected.map((record) => record.event_type)).toEqual([
      'tool_use',
      'tool_use',
      'tool_use',
    ]);
    expect(record(projected[0].payload).input).toMatchObject({ receiverThreadIds: [] });
    expect(record(projected[1].payload).input).toMatchObject({
      receiverThreadIds: ['retry-child'],
    });
    expect(record(projected[2].payload).input).toMatchObject({
      receiverThreadIds: ['other-job-child'],
    });
    const steps = buildTrajectorySteps(records, active);
    expect(steps.filter((step) => step.kind === 'tool')).toHaveLength(3);
    expect(steps[0].result).toBeUndefined();
    expect(steps[0].status).toBe('waiting');
  });

  it('does not infer delegation from unknown tools, missing identities or arbitrary raw strings', () => {
    const records = [
      notification(1, 'item/started', { item: collabItem({ tool: 'futureTool' }) }),
      notification(2, 'item/started', { item: collabItem({ id: '' }) }),
      notification(3, 'item/started', {
        item: { type: 'futureItem', id: 'a', tool: 'spawnAgent' },
      }),
      event(4, 'raw', { text: 'spawn_agent Inspect the parser' }),
    ];
    expect(projectProtocolEvents(records)).toEqual(records);
    expect(buildTrajectorySteps(records).every((step) => !step.diagnostic)).toBe(true);
  });
});

function recordedItem(event: AgentThreadEventApi): Record<string, unknown> {
  return record(record(record(record(event.payload).notification).params).item);
}

describe('Claude runtime telemetry', () => {
  const counter = {
    type: 'system',
    subtype: 'thinking_tokens',
    estimated_tokens: 1,
    estimated_tokens_delta: 1,
  };

  it.each(['text', 'line'])(
    'keeps historical %s counters in logs without adding steps or token usage',
    (field) => {
      const records = [
        event(1, 'raw', {
          [field]: JSON.stringify(counter),
          reason: "unclassified system subtype 'thinking_tokens'",
        }),
        event(2, 'raw', { [field]: JSON.stringify({ ...counter, estimated_tokens: 2 }) }),
        event(3, 'raw', { [field]: JSON.stringify(counter) }, 2),
        event(4, 'message', { text: 'Inspecting the repo', usage_id: 'm' }),
      ];
      const steps = buildTrajectorySteps(records);
      const visible = steps.filter((step) => !step.diagnostic);
      expect(visible.map((step) => step.actor)).toEqual(['assistant']);
      expect(visible[0].stepNo).toBe(1);
      expect(visible[0].usage?.reasoningTokens).toBeNull();
      const logs = steps.filter((step) => step.diagnostic);
      expect(logs.map((step) => step.sourceEvents.map((source) => source.id))).toEqual([
        [1, 2],
        [3],
      ]);
      expect(logs.every((step) => step.stepNo === 0 && step.label === 'Runtime records')).toBe(
        true,
      );
      expect(logs.flatMap((step) => step.sourceEvents)).toEqual(records.slice(0, 3));
      expect(
        toSessionLog(records)
          .split('\n')
          .map((line) => JSON.parse(line)),
      ).toEqual(records);
    },
  );

  it.each([
    { error: 'Counter failed' },
    { is_error: true },
    { status: 'failed' },
    { status: 'error' },
  ])('does not hide explicit failures on a recognized counter: %j', (failure) => {
    const steps = buildTrajectorySteps([
      event(1, 'raw', { text: JSON.stringify({ ...counter, ...failure }) }),
      event(2, 'raw', { text: JSON.stringify(counter), ...failure }),
    ]);
    expect(steps.every((step) => !step.diagnostic && step.actor === 'runtime')).toBe(true);
  });

  it('does not mistake unknown system records, invalid JSON or reasoning content for counters', () => {
    const steps = buildTrajectorySteps([
      event(1, 'raw', { text: JSON.stringify({ type: 'system', subtype: 'new_subtype' }) }),
      event(2, 'raw', { text: '{"type":"system","subtype":"thinking_tokens"' }),
      event(3, 'thinking', { text: JSON.stringify(counter) }),
    ]);
    expect(steps.every((step) => !step.diagnostic)).toBe(true);
    expect(steps[2].actor).toBe('assistant');
  });
});

describe('Codex trajectory projection', () => {
  it('updates one assistant row through start, deltas and final snapshot without changing its key or raw records', () => {
    const start = notification(1, 'item/started', {
      item: { type: 'agentMessage', id: 'm', text: '' },
    });
    const first = notification(2, 'item/agentMessage/delta', { itemId: 'm', delta: 'Hello ' });
    const second = notification(3, 'item/agentMessage/delta', { itemId: 'm', delta: 'world' });
    const final = event(4, 'message', { native_item_id: 'm', text: 'Hello world!' });
    const stages = [[start], [start, first], [start, first, second], [start, first, second, final]];
    const views = stages.map((events) => buildTrajectorySteps(events, active));
    expect(views.map((steps) => steps.length)).toEqual([1, 1, 1, 1]);
    expect(new Set(views.map(([step]) => step.key)).size).toBe(1);
    expect(views.map(([step]) => step.blocks?.[0].text)).toEqual([
      '',
      'Hello ',
      'Hello world',
      'Hello world!',
    ]);
    expect(views.map(([step]) => step.status)).toEqual([
      'waiting',
      'waiting',
      'waiting',
      'completed',
    ]);
    expect(views[3][0].sourceEvents).toEqual(stages[3]);
    expect(
      toSessionLog(stages[3])
        .split('\n')
        .map((line) => JSON.parse(line)),
    ).toEqual(stages[3]);
  });

  it('preserves interrupted partial output and keeps native IDs scoped to attempts', () => {
    const events = [
      notification(1, 'item/agentMessage/delta', { itemId: 'm', delta: 'partial' }),
      event(2, 'message', { native_item_id: 'm', text: 'retry reply' }, 2),
    ];
    const steps = buildTrajectorySteps(events);
    expect(steps).toHaveLength(2);
    expect(steps[0].blocks?.[0].text).toBe('partial');
    expect(steps[0].status).toBe('recorded');
    expect(steps[0].streaming).toBe(false);
    expect(steps[1].blocks?.[0].text).toBe('retry reply');
    expect(steps[1].status).toBe('completed');
    const [empty] = buildTrajectorySteps([
      notification(3, 'item/started', { item: { type: 'agentMessage', id: 'empty', text: '' } }),
    ]);
    expect(empty.streaming).toBe(false);
    expect(empty.diagnostic).toBe(true);
    expect(empty.sourceEvents).toHaveLength(1);
  });

  it('settles streamed reasoning and preserves separate summary and full-text channels', () => {
    const steps = buildTrajectorySteps(
      [
        notification(1, 'item/reasoning/summaryTextDelta', { itemId: 'r', delta: 'Summary ' }),
        notification(2, 'item/reasoning/textDelta', { itemId: 'r', delta: 'Recorded reasoning' }),
        event(3, 'thinking', { native_item_id: 'r', text: 'Summary complete' }),
      ],
      active,
    );
    expect(steps).toHaveLength(1);
    expect(steps[0].status).toBe('recorded');
    expect(steps[0].streaming).toBe(false);
    expect(steps[0].blocks?.map((block) => block.text)).toEqual([
      'Recorded reasoning',
      'Summary complete',
    ]);
  });

  it('joins historical fragments only when completion recorded the matching native ID', () => {
    const delta = event(1, 'raw', {
      text: JSON.stringify({
        method: 'item/agentMessage/delta',
        params: { itemId: 'm', delta: 'Hello' },
      }),
    });
    const legacy = buildTrajectorySteps([delta, event(2, 'message', { text: 'Hello' })]);
    expect(legacy.filter((step) => !step.diagnostic)).toHaveLength(1);
    expect(legacy[0].diagnostic).toBe(true);
    const identified = buildTrajectorySteps([
      delta,
      event(2, 'message', { text: 'Hello', native_item_id: 'm' }),
    ]);
    expect(identified).toHaveLength(1);
    expect(identified[0].sourceEvents.map((event) => event.id)).toEqual([1, 2]);
  });

  it('uses structured notification content when the legacy raw preview was cut', () => {
    const text = 'x'.repeat(5000);
    const [step] = buildTrajectorySteps(
      [notification(1, 'item/agentMessage/delta', { itemId: 'm', delta: text })],
      active,
    );
    expect(step.blocks?.[0].text).toBe(text);
  });

  it('renders the saved user prompt once and keeps echoed native input out of runtime logs', () => {
    const context = turnContextFromJobs([
      {
        id: 'job',
        state: 'running',
        current_attempt_id: 1,
        task_prompt: 'Explain this repo',
        turn_no: 1,
      },
    ]);
    const item = {
      id: 'u',
      type: 'userMessage',
      content: [{ type: 'text', text: 'Explain this repo' }],
    };
    const events = [
      notification(1, 'item/started', { item }),
      notification(2, 'item/completed', { item }),
      event(3, 'message', { text: 'I will read it' }),
    ];
    const steps = buildTrajectorySteps(events, context);
    expect(steps.map((step) => step.kind)).toEqual(['user', 'assistant']);
    expect(steps[0].inputText).toBe('Explain this repo');
    expect(steps[0].sourceEvents).toEqual(events.slice(0, 2));
    expect(steps[0].actor).toBe('user');
    expect(steps[0].lane).toBe('input');
    expect(steps[0].key).toBe(buildTrajectorySteps([], context)[0].key);
    expect(buildTrajectorySteps(events)[0].inputText).toBe('Explain this repo');
  });

  it('does not duplicate the user prompt on retries and supports job-scoped event reads', () => {
    const context = turnContextFromJobs([
      { id: 'job', state: 'running', current_attempt_id: 2, task_prompt: 'One prompt', turn_no: 2 },
    ]);
    const events = [
      event(1, 'message', { text: 'first' }),
      event(2, 'message', { text: 'retry' }, 2),
    ];
    const steps = buildTrajectorySteps(events, context);
    expect(steps.filter((step) => step.kind === 'user')).toHaveLength(1);
    const jobEvents = events.map(({ job_id: _job, turn_no: _turn, ...event }) => event);
    expect(buildTrajectorySteps(jobEvents, context).map((step) => step.kind)).toEqual([
      'user',
      'assistant',
      'assistant',
    ]);
  });

  it('keeps an unowned native tool as one tool record without inventing an assistant message', () => {
    const events = [
      event(1, 'message', { text: 'Earlier prose', native_item_id: 'm' }),
      event(2, 'tool_use', {
        id: 'c',
        name: 'commandExecution',
        input: 'ls',
        native_tool_request: true,
      }),
      notification(3, 'item/commandExecution/outputDelta', { itemId: 'c', delta: 'README' }),
    ];
    const partial = buildTrajectorySteps(events, active);
    expect(partial.map((step) => step.kind)).toEqual(['assistant', 'tool']);
    expect(partial[1].parentKey).toBeUndefined();
    expect(partial[0].toolKeys).toEqual([]);
    expect(partial[1].result).toBeUndefined();
    expect(partial[1].outputPreview).toBe('README');
    expect(partial[1].status).toBe('waiting');
    const done = buildTrajectorySteps([
      ...events,
      event(4, 'tool_result', { tool_use_id: 'c', content: 'README.md', exit_code: 0 }),
    ]);
    expect(done[1].key).toBe(partial[1].key);
    expect(done[1].result?.text).toBe('README.md');
    expect(done[1].sourceEvents.map((event) => event.id)).toEqual([2, 3, 4]);
  });

  it('separates normal runtime bookkeeping from errors and empty assistant records', () => {
    const steps = buildTrajectorySteps([
      event(1, 'lifecycle', { phase: 'workspace_ready' }),
      notification(2, 'account/rateLimits/updated', { rateLimits: {} }),
      notification(3, 'warning', { message: 'Model metadata unavailable' }),
      event(4, 'message', { text: '' }),
      event(5, 'error', { text: 'Host failed' }),
      event(6, 'lifecycle', { phase: 'result', is_error: true }),
      event(7, 'lifecycle', { phase: 'setup', status: 'error' }),
    ]);
    expect(steps.filter((step) => !step.diagnostic).map((step) => step.label)).toEqual([
      'warning',
      'error',
      'result',
      'setup',
    ]);
    expect(steps.filter((step) => !step.diagnostic).every((step) => step.actor === 'runtime')).toBe(
      true,
    );
    expect(steps.at(-1)?.status).toBe('failed');
    expect(steps.filter((step) => step.diagnostic).every((step) => step.stepNo === 0)).toBe(true);
  });
});

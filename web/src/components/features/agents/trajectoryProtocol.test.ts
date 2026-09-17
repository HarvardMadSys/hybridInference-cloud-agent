import { describe, expect, it } from 'vitest';
import type { AgentThreadEventApi } from '@/lib/api/agents';
import { buildTrajectorySteps, turnContextFromJobs } from './trajectorySteps';
import { toSessionLog } from './trajectory';

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

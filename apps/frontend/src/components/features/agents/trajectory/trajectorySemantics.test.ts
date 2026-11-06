import { describe, expect, it } from 'vitest';
import type { AgentThreadEventApi } from '@/lib/api/agents';
import { buildTrajectorySteps } from './trajectorySteps';
import { deriveTimeline } from './trajectoryTimeline';
import { toSessionLog } from './trajectory';

function event(
  id: number,
  event_type: string,
  payload: Record<string, unknown>,
): AgentThreadEventApi {
  return {
    id,
    seq: id,
    job_id: 'job',
    turn_no: 1,
    attempt_id: 1,
    event_type,
    payload,
    created_at: null,
  };
}
const input = {
  isAttemptActive: () => false,
  inputs: [{ jobId: 'job', turnNo: 1, text: 'Inspect the repo', at: null }],
};

// DSH's business records: one model message can contain several tool-call
// blocks. Context is input content; usage and initialization are not messages.
describe('trajectory conversation semantics', () => {
  it('keeps a pure tool-calling message as one Assistant owning multiple Tools', () => {
    const events = [
      event(1, 'tool_use', {
        usage_id: 'model-message',
        id: 'read',
        name: 'Read',
        input: 'README.md',
        usage: { output_tokens: 10 },
      }),
      event(2, 'tool_use', {
        usage_id: 'model-message',
        id: 'list',
        name: 'Bash',
        input: 'ls',
        usage: { output_tokens: 10 },
      }),
      event(3, 'tool_result', { tool_use_id: 'list', content: 'README.md' }),
      event(4, 'tool_result', { tool_use_id: 'read', content: 'Project documentation' }),
      event(5, 'usage', { usage_id: 'model-message', usage: { output_tokens: 20 } }),
      event(6, 'message', { usage_id: 'answer', text: 'Here is the summary' }),
    ];
    const steps = buildTrajectorySteps(events, input);
    expect(steps.map((step) => step.actor)).toEqual([
      'user',
      'assistant',
      'tool',
      'tool',
      'assistant',
    ]);
    const [, assistant, read, list] = steps;
    expect(assistant.nativeMessageId).toBe('model-message');
    expect(assistant.blocks).toEqual([]);
    expect(assistant.toolKeys).toEqual([read.key, list.key]);
    expect(read.parentKey).toBe(assistant.key);
    expect(list.parentKey).toBe(assistant.key);
    expect(read.result?.text).toBe('Project documentation');
    expect(list.result?.text).toBe('README.md');
    expect(assistant.sourceEvents.map((source) => source.id)).toEqual([1, 2, 5]);
    expect(assistant.usage?.outputTokens).toBe(20);
    expect(
      deriveTimeline(steps, 'steps').spans.filter((span) => span.lane === 'model'),
    ).toHaveLength(2);
    expect(
      toSessionLog(events)
        .split('\n')
        .map((line) => JSON.parse(line)),
    ).toEqual(events);
  });

  it('combines text, reasoning and tool-call blocks only when their recorded message identity matches', () => {
    const steps = buildTrajectorySteps([
      event(1, 'thinking', { usage_id: 'm', text: 'Inspect first' }),
      event(2, 'message', {
        usage_id: 'm',
        text: 'I will inspect the repo',
        usage: { output_tokens: 12 },
      }),
      event(3, 'tool_use', { usage_id: 'm', id: 'c1', name: 'Read', usage: { output_tokens: 18 } }),
      event(4, 'tool_use', { usage_id: 'm', id: 'c2', name: 'Bash', usage: { output_tokens: 18 } }),
    ]);
    expect(steps.map((step) => step.kind)).toEqual(['assistant', 'tool', 'tool']);
    expect(steps[0].blocks?.map((block) => block.text)).toEqual([
      'Inspect first',
      'I will inspect the repo',
    ]);
    expect(steps[0].sourceEvents.map((source) => source.id)).toEqual([1, 2, 3, 4]);
    expect(steps[0].usage?.outputTokens).toBe(18);
  });

  it('does not manufacture one model message per unowned Codex tool call', () => {
    const steps = buildTrajectorySteps([
      event(1, 'message', { native_item_id: 'native-text', text: 'Inspecting' }),
      event(2, 'tool_use', { id: 'c1', name: 'commandExecution', native_tool_request: true }),
      event(3, 'tool_use', { id: 'c2', name: 'commandExecution', native_tool_request: true }),
    ]);
    expect(steps.map((step) => step.actor)).toEqual(['assistant', 'tool', 'tool']);
    expect(steps[0].toolKeys).toEqual([]);
    expect(steps.slice(1).every((step) => step.parentKey === undefined)).toBe(true);
    expect(
      deriveTimeline(steps, 'steps').spans.filter((span) => span.lane === 'model'),
    ).toHaveLength(1);
  });

  it('reserves Context and System for recorded input and keeps accounting and runtime configuration in logs', () => {
    const events = [
      event(1, 'system', { text: 'You are a coding assistant' }),
      event(2, 'context', { text: 'Repository instructions', source: { kind: 'repository' } }),
      event(3, 'lifecycle', { phase: 'init', harness: { tools: ['Read'] } }),
      event(4, 'usage', { input_tokens: 100, output_tokens: 20 }),
      event(5, 'message', { text: 'Done', usage_id: 'm', usage: { output_tokens: 20 } }),
      event(6, 'usage', { usage_id: 'm', usage_scope: 'attempt', usage: { output_tokens: 900 } }),
      event(7, 'usage', { usage_id: 'missing-message', usage: { output_tokens: 50 } }),
    ];
    const steps = buildTrajectorySteps(events);
    const visible = steps.filter((step) => !step.diagnostic);
    expect(visible.map((step) => step.actor)).toEqual(['system', 'context', 'assistant']);
    expect(visible.map((step) => step.lane)).toEqual(['input', 'input', 'model']);
    expect(visible[2].usage?.outputTokens).toBe(20);
    const logs = steps.filter((step) => step.diagnostic);
    expect(logs.every((step) => step.actor === 'runtime' && step.stepNo === 0)).toBe(true);
    expect(logs.flatMap((step) => step.sourceEvents.map((source) => source.id))).toEqual([
      3, 4, 6, 7,
    ]);
    expect(logs[0].harness?.tools).toEqual(['Read']);
    expect(
      new Set(steps.flatMap((step) => step.sourceEvents.map((source) => source.id))).size,
    ).toBe(events.length);
  });
});

import { describe, expect, it } from 'vitest';

import type { AgentThreadEventApi } from '@/lib/api/agents';
import { buildTrajectorySteps, turnContextFromJobs } from './trajectorySteps';

function event(
  over: Partial<AgentThreadEventApi> & { id: number; job_id: string; turn_no: number },
): AgentThreadEventApi {
  return {
    attempt_id: 1,
    seq: over.id,
    event_type: 'message',
    payload: {},
    created_at: '2026-08-19T10:00:00.000Z',
    ...over,
  } as AgentThreadEventApi;
}

const idle = { isAttemptActive: () => false };

describe('buildTrajectorySteps', () => {
  it('groups one native message into one assistant step and keeps its tools separate', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'thinking',
          payload: { text: 'weighing options', usage_id: 'msg_1' },
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't1', name: 'Bash', input: { command: 'ls' }, usage_id: 'msg_1' },
        }),
        event({
          id: 3,
          job_id: 'a',
          turn_no: 1,
          event_type: 'message',
          payload: { text: 'listing the tree', usage_id: 'msg_1' },
        }),
      ],
      idle,
    );

    // The tool call is its own step, and it is presented after the message it
    // belongs to even though its record arrived first.
    expect(steps.map((step) => step.kind)).toEqual(['assistant', 'tool']);
    const [assistant, tool] = steps;
    expect(assistant.nativeMessageId).toBe('msg_1');
    expect(assistant.blocks?.map((block) => block.type)).toEqual(['thinking', 'text']);
    expect(tool.parentKey).toBe(assistant.key);
    expect(assistant.toolKeys).toEqual([tool.key]);
  });

  it('keeps a message with no recorded identity as its own step', () => {
    const steps = buildTrajectorySteps(
      [
        event({ id: 1, job_id: 'a', turn_no: 1, payload: { text: 'one' } }),
        event({ id: 2, job_id: 'a', turn_no: 1, payload: { text: 'two' } }),
      ],
      idle,
    );

    expect(steps).toHaveLength(2);
    expect(steps.every((step) => step.nativeMessageId === undefined)).toBe(true);
  });

  it('never attributes a tool to a neighbouring message without a recorded identity', () => {
    const steps = buildTrajectorySteps(
      [
        event({ id: 1, job_id: 'a', turn_no: 1, payload: { text: 'before' } }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't1', name: 'Bash' },
        }),
      ],
      idle,
    );

    expect(steps[1].parentKey).toBeUndefined();
  });

  it('pairs a result inside its own attempt and not with a retry of the same call', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          attempt_id: 1,
          event_type: 'tool_use',
          payload: { id: 'dup', name: 'Bash' },
          created_at: '2026-08-19T10:00:00.000Z',
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          attempt_id: 2,
          event_type: 'tool_use',
          payload: { id: 'dup', name: 'Bash' },
          created_at: '2026-08-19T10:00:10.000Z',
        }),
        event({
          id: 3,
          job_id: 'a',
          turn_no: 1,
          attempt_id: 2,
          event_type: 'tool_result',
          payload: { tool_use_id: 'dup', content: 'second attempt' },
          created_at: '2026-08-19T10:00:12.000Z',
        }),
      ],
      idle,
    );

    expect(steps).toHaveLength(2);
    expect(steps[0].result).toBeUndefined();
    expect(steps[1].result?.text).toBe('second attempt');
    expect(steps[1].timing.durationMs).toBe(2000);
    expect(steps[1].timing.source).toBe('session_timestamps');
  });

  it('keeps two attempts of one call id apart even in the same job', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          attempt_id: 7,
          event_type: 'tool_use',
          payload: { id: 'x', name: 'Read' },
        }),
        event({
          id: 2,
          job_id: 'b',
          turn_no: 2,
          attempt_id: 7,
          event_type: 'tool_use',
          payload: { id: 'x', name: 'Read' },
        }),
      ],
      idle,
    );

    expect(steps[0].key).not.toBe(steps[1].key);
    expect(steps.map((step) => step.jobId)).toEqual(['a', 'b']);
  });

  it('counts a repeated usage once, because one message repeats it per block', () => {
    const usage = { input_tokens: 10, output_tokens: 100 };
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'thinking',
          payload: { text: 'hmm', usage_id: 'msg_1', usage },
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          payload: { text: 'done', usage_id: 'msg_1', usage },
        }),
      ],
      idle,
    );

    expect(steps).toHaveLength(1);
    expect(steps[0].usage?.outputTokens).toBe(100);
    expect(steps[0].usage?.inputTokens).toBe(10);
  });

  it('leaves unrecorded usage null rather than zero', () => {
    const [step] = buildTrajectorySteps(
      [event({ id: 1, job_id: 'a', turn_no: 1, payload: { text: 'hi' } })],
      idle,
    );

    expect(step.usage?.outputTokens).toBeNull();
    expect(step.usage?.reasoningTokens).toBeNull();
  });

  it('reads reasoning only when a runtime states it, never by subtraction', () => {
    const [step] = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          payload: {
            text: 'hi',
            usage_id: 'm',
            usage: { output_tokens: 90, reasoning_tokens: 30 },
          },
        }),
      ],
      idle,
    );

    expect(step.usage?.reasoningTokens).toBe(30);
    expect(step.usage?.contentTokens).toBeNull();
  });

  it('reports a failed call from the error flag or a non-zero exit code', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't1', name: 'Bash' },
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_result',
          payload: { tool_use_id: 't1', content: 'boom', exit_code: 2 },
        }),
      ],
      idle,
    );

    expect(steps[0].status).toBe('failed');
    expect(steps[0].isError).toBe(false);
    expect(steps[0].result?.isError).toBe(true);
    expect(steps[0].result?.exitCode).toBe(2);
  });

  it('separates an empty result from a missing one', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't1', name: 'Bash' },
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_result',
          payload: { tool_use_id: 't1', content: '' },
        }),
        event({
          id: 3,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't2', name: 'Bash' },
        }),
      ],
      idle,
    );

    expect(steps[0].status).toBe('completed');
    expect(steps[0].result?.text).toBe('');
    expect(steps[1].result).toBeUndefined();
    expect(steps[1].status).toBe('not_recorded');
  });

  it('waits for a result only while the matched attempt is still running', () => {
    const events = [
      event({
        id: 1,
        job_id: 'a',
        turn_no: 1,
        attempt_id: 4,
        event_type: 'tool_use',
        payload: { id: 't1', name: 'Bash' },
      }),
    ];
    const context = turnContextFromJobs([{ id: 'a', state: 'running', current_attempt_id: 4 }]);

    expect(buildTrajectorySteps(events, context)[0].status).toBe('waiting');
    expect(buildTrajectorySteps(events, idle)[0].status).toBe('not_recorded');
    // A later retry being live does not make this attempt's call still pending.
    expect(
      buildTrajectorySteps(
        events,
        turnContextFromJobs([{ id: 'a', state: 'running', current_attempt_id: 9 }]),
      )[0].status,
    ).toBe('not_recorded');
  });

  it('does not keep a call waiting while the turn sits between attempts', () => {
    const events = [
      event({
        id: 1,
        job_id: 'a',
        turn_no: 1,
        attempt_id: 4,
        event_type: 'tool_use',
        payload: { id: 't1', name: 'Bash' },
      }),
    ];

    // Superseded and not yet replaced: the row names no current attempt, so
    // nothing is in flight and the call cannot still be answered.
    const requeued = turnContextFromJobs([{ id: 'a', state: 'queued', current_attempt_id: null }]);
    expect(buildTrajectorySteps(events, requeued)[0].status).toBe('not_recorded');

    // A row that does not carry the field at all cannot rule the attempt out.
    const notStated = turnContextFromJobs([{ id: 'a', state: 'running' }]);
    expect(buildTrajectorySteps(events, notStated)[0].status).toBe('waiting');
  });

  it('keeps the answering result reachable from the call step', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't1', name: 'Bash' },
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_result',
          payload: { tool_use_id: 't1', content: 'ok', truncated: true },
        }),
      ],
      idle,
    );

    expect(steps).toHaveLength(1);
    expect(steps[0].sourceEvents.map((item) => item.id)).toEqual([1, 2]);
    expect(steps[0].truncated).toBe(true);
  });

  it('keeps an orphan result instead of dropping it', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_result',
          payload: { tool_use_id: 'gone', content: 'output with no call' },
        }),
      ],
      idle,
    );

    expect(steps).toHaveLength(1);
    expect(steps[0].kind).toBe('tool');
    expect(steps[0].label).toBe('Tool result');
    expect(steps[0].result?.text).toBe('output with no call');
    expect(steps[0].parentKey).toBeUndefined();
  });

  it('flags a repeated call id instead of silently overwriting it', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't1', name: 'Bash', input: 'first' },
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't1', name: 'Bash', input: 'second' },
        }),
      ],
      idle,
    );

    expect(steps).toHaveLength(1);
    expect(steps[0].ambiguity).toBe('duplicate_call');
    expect(steps[0].sourceEvents.map((item) => item.id)).toEqual([1, 2]);
    // Which record is "the" call is unverified, so no status is claimed.
    expect(steps[0].status).toBe('unknown');
  });

  it('flags two results for one call rather than picking the last', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't1', name: 'Bash' },
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_result',
          payload: { tool_use_id: 't1', content: 'first' },
        }),
        event({
          id: 3,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_result',
          payload: { tool_use_id: 't1', content: 'second' },
        }),
      ],
      idle,
    );

    expect(steps).toHaveLength(1);
    expect(steps[0].ambiguity).toBe('duplicate_result');
    expect(steps[0].result?.eventIds).toEqual([2, 3]);
  });

  it('keeps falsy argument values, which are legal calls', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't1', name: 'X', input: false },
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't2', name: 'Y', input: 0 },
        }),
        event({
          id: 3,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't3', name: 'Z', input: {} },
        }),
        event({
          id: 4,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't4', name: 'W' },
        }),
      ],
      idle,
    );

    expect(steps[0].payloadValue).toBe(false);
    expect(steps[1].payloadValue).toBe(0);
    expect(steps[2].payloadValue).toEqual({});
    expect(steps[3].payloadValue).toBeUndefined();
  });

  it('treats a bare detail as a preview, not as the recorded arguments', () => {
    const [step] = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't1', name: 'Bash', detail: 'cargo build' },
        }),
      ],
      idle,
    );

    expect(step.payloadValue).toBeUndefined();
    expect(step.payloadPreview).toBe('cargo build');
  });

  it('distinguishes a real zero duration from an unreadable interval', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 'same', name: 'Bash' },
          created_at: '2026-08-19T10:00:00.000Z',
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_result',
          payload: { tool_use_id: 'same', content: 'ok' },
          created_at: '2026-08-19T10:00:00.000Z',
        }),
        event({
          id: 3,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 'back', name: 'Bash' },
          created_at: '2026-08-19T10:00:05.000Z',
        }),
        event({
          id: 4,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_result',
          payload: { tool_use_id: 'back', content: 'ok' },
          created_at: '2026-08-19T10:00:01.000Z',
        }),
      ],
      idle,
    );

    expect(steps[0].timing.durationMs).toBe(0);
    expect(steps[1].timing.durationMs).toBeNull();
  });

  it('carries the truncation marker from any source payload', () => {
    const [step] = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_result',
          payload: {
            tool_use_id: 't1',
            content: 'head',
            truncated: true,
            original_payload_bytes: 900000,
          },
        }),
      ],
      idle,
    );

    expect(step.truncated).toBe(true);
  });

  it('keeps unknown event types as selectable steps', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'lifecycle',
          payload: { phase: 'workspace_ready' },
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'raw',
          payload: { line: '{"weird":true}' },
        }),
        event({ id: 3, job_id: 'a', turn_no: 1, event_type: 'mystery', payload: { x: 1 } }),
      ],
      idle,
    );

    expect(steps.map((step) => step.kind)).toEqual(['event', 'event', 'event']);
    expect(steps[0].label).toBe('workspace_ready');
    expect(steps[2].label).toBe('mystery');
  });

  it('numbers steps inside a turn and restarts for the next one', () => {
    const steps = buildTrajectorySteps(
      [
        event({ id: 1, job_id: 'a', turn_no: 1, payload: { text: 'first' } }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't1', name: 'Bash' },
        }),
        event({ id: 3, job_id: 'b', turn_no: 2, payload: { text: 'second' } }),
      ],
      idle,
    );

    expect(steps.map((step) => step.stepNo)).toEqual([1, 2, 1]);
  });

  it('leaves an ordinary call unflagged and keeps its recorded span', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't1', name: 'Bash', input: { command: 'pwd' } },
          created_at: '2026-08-19T10:00:00.000Z',
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_result',
          payload: { tool_use_id: 't1', content: '/repo' },
          created_at: '2026-08-19T10:00:05.000Z',
        }),
      ],
      idle,
    );

    expect(steps).toHaveLength(1);
    // A call id seen once is the ordinary case, not an ambiguity.
    expect(steps[0].ambiguity).toBeUndefined();
    expect(steps[0].status).toBe('completed');
    expect(steps[0].timing.durationMs).toBe(5_000);
  });

  it('keeps calls with no recorded id as steps of their own', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: null, name: 'Bash', input: 'pwd' },
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: null, name: 'Read', input: 'README.md' },
        }),
      ],
      idle,
    );

    // With no id to share, the record's own identity is all there is: both
    // calls stay readable instead of the second folding into the first.
    expect(steps.map((step) => step.label)).toEqual(['Bash', 'Read']);
    expect(steps.map((step) => step.payloadValue)).toEqual(['pwd', 'README.md']);
    expect(steps.map((step) => step.ambiguity)).toEqual([undefined, undefined]);
    expect(new Set(steps.map((step) => step.key)).size).toBe(2);
  });

  it('claims no status or duration when one call was answered twice', () => {
    const steps = buildTrajectorySteps(
      [
        event({
          id: 1,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_use',
          payload: { id: 't1', name: 'Bash' },
          created_at: '2026-08-19T10:00:00.000Z',
        }),
        event({
          id: 2,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_result',
          payload: { tool_use_id: 't1', content: 'ok', is_error: false },
          created_at: '2026-08-19T10:00:01.000Z',
        }),
        event({
          id: 3,
          job_id: 'a',
          turn_no: 1,
          event_type: 'tool_result',
          payload: { tool_use_id: 't1', content: 'failed', is_error: true },
          created_at: '2026-08-19T10:00:02.000Z',
        }),
      ],
      idle,
    );

    expect(steps).toHaveLength(1);
    expect(steps[0].ambiguity).toBe('duplicate_result');
    // Every record is kept, but the pairing is unverified, so the step reports
    // no outcome and no span rather than the first result's.
    expect(steps[0].status).toBe('unknown');
    expect(steps[0].timing.durationMs).toBeNull();
    expect(steps[0].timing.source).toBeNull();
    expect(steps[0].result?.eventIds).toEqual([2, 3]);
    expect(steps[0].result?.isError).toBe(true);
  });
});

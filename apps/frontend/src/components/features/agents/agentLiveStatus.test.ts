import { describe, expect, it } from 'vitest';
import type { AgentJobApi, AgentJobEventApi } from '@/lib/api/agents';
import { toDisplayJob } from './adapt';
import { agentLiveNote, agentLiveStatus } from './agentLiveStatus';

const job = { id: 'job', state: 'running', current_attempt_id: 2 } as AgentJobApi;
function event(
  id: number,
  event_type: string,
  payload: Record<string, unknown> = {},
  attempt_id = 2,
): AgentJobEventApi {
  return { id, seq: id, attempt_id, event_type, payload, created_at: null };
}

function timedEvent(
  id: number,
  event_type: string,
  created_at: string | null,
  payload: Record<string, unknown> = {},
  attempt_id = 2,
): AgentJobEventApi {
  return { ...event(id, event_type, payload, attempt_id), created_at };
}

const start = '2026-10-10T12:00:00Z';
const later = '2026-10-10T12:00:05Z';
const end = '2026-10-10T12:00:10Z';

describe('live agent status', () => {
  it('keeps workspace setup quiet, then waits once the prompt is dispatched', () => {
    const events = [
      event(1, 'lifecycle', { phase: 'started' }),
      event(2, 'lifecycle', { phase: 'workspace_preparing' }),
      event(3, 'lifecycle', { phase: 'checked_out' }),
    ];
    expect(agentLiveNote(job, events)).toBeUndefined();
    events.push(event(4, 'lifecycle', { phase: 'turn_started' }));
    expect(agentLiveNote(job, events)).toBe('Waiting for LLM response');
  });

  it('switches between tool activity and model waiting, even after a tool error', () => {
    const events = [event(1, 'lifecycle', { phase: 'init' })];
    expect(agentLiveNote(job, events)).toBe('Waiting for LLM response');
    events.push(event(2, 'tool_use', { id: 'a', name: 'Bash' }));
    expect(agentLiveNote(job, events)).toBe('Running tool call');
    events.push(event(3, 'tool_result', { tool_use_id: 'a', content: '', is_error: true }));
    expect(agentLiveNote(job, events)).toBe('Waiting for LLM response');
    events.push(event(4, 'tool_use', { id: 'b', name: 'Read' }));
    expect(agentLiveNote(job, events)).toBe('Running tool call');
  });

  it('keeps parallel calls active until every matching result arrives', () => {
    const events = [
      event(1, 'tool_use', { id: 'a' }),
      event(2, 'tool_use', { id: 'b' }),
      event(3, 'tool_result', { tool_use_id: 'a', content: 'done' }),
      event(4, 'usage', { output_tokens: 10 }),
    ];
    expect(agentLiveNote(job, events)).toBe('Running tool call');
    events.push(event(5, 'tool_result', { tool_use_id: 'b', content: 'failed', is_error: true }));
    expect(agentLiveNote(job, events)).toBe('Waiting for LLM response');
  });

  it('does not settle a call from an unrelated result ID', () => {
    const events = [
      event(1, 'tool_use', { id: 'a' }),
      event(2, 'tool_result', { tool_use_id: 'other' }),
    ];
    expect(agentLiveNote(job, events)).toBe('Running tool call');
  });

  it('settles an id-less result only when one call is pending', () => {
    const events = [event(1, 'tool_use'), event(2, 'tool_result')];
    expect(agentLiveNote(job, events)).toBe('Waiting for LLM response');
    expect(
      agentLiveNote(job, [event(1, 'tool_use'), event(2, 'tool_use'), event(3, 'tool_result')]),
    ).toBe('Running tool call');
  });

  it('recognizes a tool whose completion was recorded inline', () => {
    expect(agentLiveNote(job, [event(1, 'tool_use', { id: 'a', output: '', exit_code: 0 })])).toBe(
      'Waiting for LLM response',
    );
    expect(
      agentLiveNote(job, [
        event(1, 'tool_use', { id: 'a' }),
        event(2, 'tool_use', { id: 'a', status: 'failed' }),
      ]),
    ).toBe('Waiting for LLM response');
  });

  it('keeps a committed native plan update from appearing as a pending tool', () => {
    const plan = event(2, 'raw', {
      protocol: 'codex-app-server',
      notification: {
        method: 'turn/plan/updated',
        params: { turnId: 'turn', plan: [{ step: 'Inspect the parser', status: 'inProgress' }] },
      },
    });
    expect(agentLiveNote(job, [plan])).toBe('Waiting for LLM response');
    expect(agentLiveNote(job, [event(1, 'tool_use', { id: 'shell', name: 'Bash' }), plan])).toBe(
      'Running tool call',
    );
  });

  it.each(['completed', 'failed', 'interrupted'])(
    'recognizes a completion-only native subagent call with status %s',
    (status) => {
      expect(
        agentLiveNote(job, [
          event(1, 'raw', {
            protocol: 'codex-app-server',
            notification: {
              method: 'item/completed',
              params: {
                item: {
                  id: 'delegate',
                  type: 'collabAgentToolCall',
                  tool: 'spawnAgent',
                  status,
                  receiverThreadIds: ['reviewer'],
                  agentsStates: { reviewer: { status: 'running' } },
                },
              },
            },
          }),
        ]),
      ).toBe('Waiting for LLM response');
    },
  );

  it('shows response streaming from reply previews and returns to waiting at a message boundary', () => {
    const events = [
      event(1, 'raw', { protocol: 'reply-preview', preview_id: 'reply', delta: 'Hello' }),
    ];
    expect(agentLiveNote(job, events)).toBe('Receiving LLM response');
    events.push(
      event(2, 'raw', {
        protocol: 'reply-preview',
        preview_id: 'reply',
        delta: 'Revised reply',
        reset: true,
      }),
    );
    expect(agentLiveNote(job, events)).toBe('Receiving LLM response');
    events.push(event(3, 'message', { preview_id: 'reply', text: 'Revised reply' }));
    expect(agentLiveNote(job, events)).toBe('Waiting for LLM response');
  });

  it('keeps a parallel tool active while an assistant streams', () => {
    expect(
      agentLiveNote(job, [
        event(1, 'tool_use', { id: 'a' }),
        event(2, 'raw', { protocol: 'reply-preview', preview_id: 'reply', delta: 'Progress' }),
      ]),
    ).toBe('Running tool call');
  });

  it('returns to model waiting on a retry after partial output while retaining pending tools', () => {
    const events = [
      event(1, 'raw', { protocol: 'reply-preview', preview_id: 'reply', delta: 'Partial reply' }),
    ];
    expect(agentLiveNote(job, events)).toBe('Receiving LLM response');
    events.push(event(2, 'lifecycle', { phase: 'api_retry', retry_delay_ms: 5000 }));
    expect(agentLiveNote(job, events)).toBe('Waiting for LLM response');
    expect(agentLiveNote(job, [event(0, 'tool_use', { id: 'parallel' }), ...events])).toBe(
      'Running tool call',
    );
    events.push(
      event(3, 'raw', { protocol: 'reply-preview', preview_id: 'reply', delta: 'Resumed' }),
    );
    expect(agentLiveNote(job, events)).toBe('Receiving LLM response');
  });

  it('reads Codex turn and response notifications without showing raw content', () => {
    const notification = (method: string, params = {}) => ({
      protocol: 'codex-app-server',
      notification: { method, params },
    });
    const events = [event(1, 'raw', notification('turn/started'))];
    expect(agentLiveNote(job, events)).toBe('Waiting for LLM response');
    events.push(
      event(2, 'raw', notification('item/agentMessage/delta', { itemId: 'r', delta: 'Hello' })),
    );
    expect(agentLiveNote(job, events)).toBe('Receiving LLM response');
    events.push(event(3, 'raw', notification('turn/completed')));
    expect(agentLiveNote(job, events)).toBeUndefined();
  });

  it('ignores a superseded attempt, even when its tool ID is reused', () => {
    const events = [
      event(1, 'tool_use', { id: 'a' }, 1),
      event(2, 'lifecycle', { phase: 'turn_started' }),
      event(3, 'tool_use', { id: 'a' }),
      event(4, 'tool_result', { tool_use_id: 'a' }, 1),
    ];
    expect(agentLiveNote(job, events)).toBe('Running tool call');
    events.push(event(5, 'tool_result', { tool_use_id: 'a' }));
    expect(agentLiveNote(job, events)).toBe('Waiting for LLM response');
    expect(agentLiveNote({ ...job, current_attempt_id: 3 }, events)).toBeUndefined();
  });

  it('ignores another turn when given a thread event stream', () => {
    expect(
      agentLiveNote(job, [
        { ...event(1, 'tool_use', { id: 'a' }), job_id: 'other' } as AgentJobEventApi,
        event(2, 'lifecycle', { phase: 'turn_started' }),
      ]),
    ).toBe('Waiting for LLM response');
  });

  it.each(['queued', 'succeeded', 'publishing', 'failed', 'cancelled'] as const)(
    'clears status for a %s job',
    (state) => {
      expect(agentLiveNote({ ...job, state }, [event(1, 'tool_use', { id: 'a' })])).toBeUndefined();
    },
  );

  it('clears activity when there is no active attempt', () => {
    expect(
      agentLiveNote({ ...job, current_attempt_id: null }, [event(1, 'tool_use', { id: 'a' })]),
    ).toBeUndefined();
  });

  it.each(['result', 'workspace_finalizing', 'shared_workspace_settled', 'cancelled_by_owner'])(
    'clears pending activity at %s before the job row catches up',
    (phase) => {
      expect(
        agentLiveNote(job, [
          event(1, 'tool_use', { id: 'a' }),
          event(2, 'lifecycle', { phase }),
          event(3, 'tool_result', { tool_use_id: 'a' }),
          event(4, 'raw', { protocol: 'reply-preview', preview_id: 'late', delta: 'Late output' }),
        ]),
      ).toBeUndefined();
    },
  );

  it.each(['attempt_superseded', 'error'])('clears activity on %s', (event_type) => {
    expect(
      agentLiveNote(job, [event(1, 'tool_use', { id: 'a' }), event(2, event_type)]),
    ).toBeUndefined();
  });

  it('wires the live note and its start through the API adapter', () => {
    const api = { ...job, task_prompt: 'Fix tests', repo: 'owner/repo', runtime: 'pi' };
    expect(
      toDisplayJob(api, {
        events: [timedEvent(1, 'lifecycle', start, { phase: 'turn_started' })],
      }),
    ).toMatchObject({ liveNote: 'Waiting for LLM response', liveNoteStartedAt: start });
  });

  it('anchors model waiting to its first visible boundary, including after a reload', () => {
    const events = [
      timedEvent(1, 'lifecycle', start, { phase: 'turn_started' }),
      timedEvent(2, 'lifecycle', later, { phase: 'init' }),
      timedEvent(3, 'thinking', later, { text: 'Working' }),
      timedEvent(4, 'usage', end, { output_tokens: 10 }),
    ];

    expect(agentLiveStatus(job, events)).toEqual({
      note: 'Waiting for LLM response',
      startedAt: start,
    });
    expect(agentLiveStatus(job, [...events])).toEqual(agentLiveStatus(job, events));
  });

  it('keeps one tool interval across parallel calls and starts waiting when the last returns', () => {
    const events = [
      timedEvent(1, 'lifecycle', start, { phase: 'turn_started' }),
      timedEvent(2, 'tool_use', later, { id: 'a' }),
      timedEvent(3, 'tool_use', end, { id: 'b' }),
      timedEvent(4, 'tool_result', end, { tool_use_id: 'a' }),
      timedEvent(5, 'tool_result', end, { tool_use_id: 'unrelated' }),
      timedEvent(6, 'raw', end, {
        protocol: 'reply-preview',
        preview_id: 'reply',
        delta: 'Progress',
      }),
    ];
    expect(agentLiveStatus(job, events)).toEqual({
      note: 'Running tool call',
      startedAt: later,
    });

    events.push(timedEvent(7, 'tool_result', end, { tool_use_id: 'b', is_error: true }));
    expect(agentLiveStatus(job, events)).toEqual({
      note: 'Waiting for LLM response',
      startedAt: end,
    });
  });

  it('keeps the first response delta as its start and resets at message and retry boundaries', () => {
    const preview = { protocol: 'reply-preview', preview_id: 'reply', delta: 'Hello' };
    const events = [
      timedEvent(1, 'lifecycle', start, { phase: 'turn_started' }),
      timedEvent(2, 'raw', later, preview),
      timedEvent(3, 'raw', end, { ...preview, reset: true }),
    ];
    expect(agentLiveStatus(job, events)).toEqual({
      note: 'Receiving LLM response',
      startedAt: later,
    });

    events.push(timedEvent(4, 'message', end, { preview_id: 'reply', text: 'Hello' }));
    expect(agentLiveStatus(job, events)).toEqual({
      note: 'Waiting for LLM response',
      startedAt: end,
    });
    events.push(timedEvent(5, 'raw', '2026-10-10T12:00:15Z', preview));
    events.push(timedEvent(6, 'lifecycle', '2026-10-10T12:00:20Z', { phase: 'api_retry' }));
    expect(agentLiveStatus(job, events)).toEqual({
      note: 'Waiting for LLM response',
      startedAt: '2026-10-10T12:00:20Z',
    });
  });

  it('uses recorded timestamps through native protocol projections', () => {
    const events = [
      timedEvent(1, 'raw', start, {
        protocol: 'codex-app-server',
        notification: { method: 'turn/started', params: {} },
      }),
      timedEvent(2, 'raw', later, {
        protocol: 'codex-app-server',
        notification: {
          method: 'item/agentMessage/delta',
          params: { itemId: 'reply', delta: 'Hello' },
        },
      }),
      timedEvent(3, 'raw', end, {
        protocol: 'codex-app-server',
        notification: {
          method: 'item/agentMessage/delta',
          params: { itemId: 'reply', delta: ' again' },
        },
      }),
    ];
    expect(agentLiveStatus(job, events)).toEqual({
      note: 'Receiving LLM response',
      startedAt: later,
    });
  });

  it.each([null, 'invalid timestamp'])(
    'does not invent a start when the boundary time is %s',
    (at) => {
      const events = [
        timedEvent(1, 'lifecycle', at, { phase: 'turn_started' }),
        timedEvent(2, 'thinking', later),
      ];
      expect(agentLiveStatus(job, events)).toEqual({ note: 'Waiting for LLM response' });

      events.push(timedEvent(3, 'tool_use', end, { id: 'a' }));
      expect(agentLiveStatus(job, events)).toEqual({ note: 'Running tool call', startedAt: end });
    },
  );

  it('ignores timestamp boundaries belonging to another attempt or conversation turn', () => {
    expect(
      agentLiveStatus(job, [
        timedEvent(1, 'lifecycle', start, { phase: 'turn_started' }, 1),
        {
          ...timedEvent(2, 'tool_use', start, { id: 'a' }),
          job_id: 'other',
        } as AgentJobEventApi,
        timedEvent(3, 'lifecycle', later, { phase: 'turn_started' }),
      ]),
    ).toEqual({ note: 'Waiting for LLM response', startedAt: later });
  });

  it('clears settled timing and ignores late activity before the job state catches up', () => {
    const events = [
      timedEvent(1, 'tool_use', start, { id: 'a' }),
      timedEvent(2, 'lifecycle', later, { phase: 'cancelled_by_owner' }),
      timedEvent(3, 'tool_result', end, { tool_use_id: 'a' }),
      timedEvent(4, 'lifecycle', end, { phase: 'turn_started' }),
    ];
    expect(agentLiveStatus(job, events)).toBeUndefined();
    expect(agentLiveStatus({ ...job, state: 'succeeded' }, events.slice(0, 1))).toBeUndefined();
  });
});

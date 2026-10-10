import { describe, expect, it } from 'vitest';
import type { AgentJobApi, AgentJobEventApi } from '@/lib/api/agents';
import { toDisplayJob } from './adapt';
import { agentLiveNote } from './agentLiveStatus';

const job = { id: 'job', state: 'running', current_attempt_id: 2 } as AgentJobApi;
function event(
  id: number,
  event_type: string,
  payload: Record<string, unknown> = {},
  attempt_id = 2,
): AgentJobEventApi {
  return { id, seq: id, attempt_id, event_type, payload, created_at: null };
}

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

  it('wires the live note through the API adapter', () => {
    const api = { ...job, task_prompt: 'Fix tests', repo: 'owner/repo', runtime: 'pi' };
    expect(
      toDisplayJob(api, { events: [event(1, 'lifecycle', { phase: 'turn_started' })] }).liveNote,
    ).toBe('Waiting for LLM response');
  });
});

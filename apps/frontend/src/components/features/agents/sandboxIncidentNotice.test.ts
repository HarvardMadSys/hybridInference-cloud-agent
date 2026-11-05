import { describe, expect, it } from 'vitest';

import type { AgentJobEventApi } from '@/lib/api/agents';
import { toDisplayEvent } from './adapt';
import { sandboxIncidentMessage } from './sandboxIncidentNotice';

function incident(payload: Record<string, unknown>): AgentJobEventApi {
  const fields = { phase: 'sandbox_incident', ...payload };
  return {
    id: 9,
    attempt_id: 1,
    seq: 9,
    event_type: 'lifecycle',
    payload: fields,
    created_at: null,
  };
}

describe('sandbox incident lines', () => {
  it('say what broke the turn in plain words', () => {
    expect(sandboxIncidentMessage({ incident: 'out_of_memory' })).toBe(
      'The environment ran out of memory, and some processes were stopped.',
    );
    expect(sandboxIncidentMessage({ incident: 'container_exited' })).toContain(
      'starts again with your next message',
    );
    expect(
      sandboxIncidentMessage({
        incident: 'mcp_server_failed',
        alias: 'docs',
        server_phase: 'start',
      }),
    ).toBe('docs could not start, so its tools were not available.');
    expect(
      sandboxIncidentMessage({
        incident: 'mcp_server_failed',
        alias: 'docs',
        server_phase: 'crashed',
      }),
    ).toBe('docs stopped unexpectedly. It starts again when it is next used.');
  });

  it('read generically for a kind this release does not know', () => {
    expect(sandboxIncidentMessage({ incident: 'something_new' })).toBe(
      'Something went wrong in the environment.',
    );
    expect(sandboxIncidentMessage({ incident: '__proto__' })).toBe(
      'Something went wrong in the environment.',
    );
  });

  it('show in the conversation as a lifecycle line', () => {
    expect(toDisplayEvent(incident({ incident: 'out_of_memory' }))).toEqual({
      kind: 'lifecycle',
      text: 'The environment ran out of memory, and some processes were stopped.',
      sourceEventId: 9,
    });
  });
});

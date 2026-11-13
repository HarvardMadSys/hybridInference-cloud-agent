// A model call's usage is bookkeeping, not a transcript row. Kept apart from
// adapt.test.ts so that file stays within its size budget in
// eslint-test-ratchet.json.

import { describe, expect, it } from 'vitest';

import type { AgentJobEventApi } from '@/lib/api/agents';

import { toDisplayEvent } from './events';

function event(id: number, payload: Record<string, unknown>): AgentJobEventApi {
  return { id, attempt_id: 1, seq: id, event_type: 'usage', payload, created_at: null };
}

describe('a usage event', () => {
  it('is not a transcript row', () => {
    // Claude reports one per model call (pi one per message). Each used to
    // render as an empty line and count as another folded step.
    expect(toDisplayEvent(event(1, { usage: { output_tokens: 9 }, usage_id: 'msg_1' }))).toBeNull();
    expect(toDisplayEvent(event(2, { input_tokens: 3, output_tokens: 5 }))).toBeNull();
  });
});

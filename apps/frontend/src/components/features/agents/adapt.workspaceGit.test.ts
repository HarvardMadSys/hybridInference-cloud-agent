// Reusing one workspace Git status read while a running turn is projected
// again, kept apart from adapt.test.ts so that file stays within its size
// budget in eslint-test-ratchet.json.

import { describe, expect, it } from 'vitest';

import type { AgentJobApi, AgentJobEventApi } from '@/lib/api/agents';

import { createDisplayJobAdapter } from './adapt';

const JOB: AgentJobApi = {
  id: 'ajob_1',
  repo: 'o/n',
  task_prompt: 'fix the flaky test\nmore detail',
  runtime: 'claude-code',
  model: 'glm-5.1',
  base_sha: 'abcdef1234567890',
  state: 'running',
  cancel_requested: false,
  current_attempt_id: 1,
  published_pr_url: null,
  detail: null,
  metadata: null,
  created_at: null,
  updated_at: null,
  spent_usd: null,
  tokens_in: null,
  tokens_out: null,
  model_calls: null,
};

function event(
  id: number,
  event_type: string,
  payload: Record<string, unknown> = {},
  attempt_id = 1,
): AgentJobEventApi {
  return { id, attempt_id, seq: id, event_type, payload, created_at: null };
}

describe('projecting a running turn again', () => {
  it('keeps the changed paths of one status read while the turn streams', () => {
    const project = createDisplayJobAdapter();
    const gitMetadata = {
      available: true,
      revision: 'a'.repeat(64),
      branch: 'agent/work',
      changes: [{ code: ' M', path: 'src/app.ts' }],
      change_count: 1,
    };
    const pending = project(JOB, { events: [], gitMetadataPending: true });
    const stillPending = project(JOB, {
      events: [event(1, 'message', { text: 'Streamed.' })],
      gitMetadataPending: true,
    });
    expect(stillPending.diffFiles).toEqual([]);
    expect(stillPending.diffFiles).toBe(pending.diffFiles);

    const first = project(JOB, { events: [], gitMetadata });
    const again = project(JOB, {
      events: [event(1, 'message', { text: 'Streamed.' })],
      gitMetadata,
    });
    expect(again.diffFiles).toEqual(['src/app.ts']);
    expect(again.diffFiles).toBe(first.diffFiles);
    const reread = project(JOB, {
      events: [],
      gitMetadata: {
        ...gitMetadata,
        changes: [...gitMetadata.changes, { code: '??', path: 'src/new.ts' }],
        change_count: 2,
      },
    });
    expect(reread.diffFiles).toEqual(['src/app.ts', 'src/new.ts']);
  });
});

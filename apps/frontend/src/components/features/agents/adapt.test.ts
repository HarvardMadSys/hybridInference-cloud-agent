// Tests for the API → UI adapter.
//
// The cases that matter are the ones where a wrong mapping would mislead a
// user about their own job: a completed chat looking active while it publishes, a
// superseded attempt looking live, or an event kind silently vanishing.

import { describe, expect, it } from 'vitest';

import type { AgentJobApi, AgentJobEventApi } from '@/lib/api/agents';
import { getConversationState, toConversationState } from './conversationState';

import {
  lifecyclePhaseLabel,
  ownerFailureMessage,
  siteMisconfiguredReason,
  toDiffFileDetails,
  toDiffFiles,
  toDisplayEvent,
  toDisplayJob,
  toDisplayState,
} from './adapt';

describe('owner failure wording', () => {
  it.each([
    'retained sandbox activation lease expired; context marked lost',
    'retained Agent CLI turn became indeterminate after prompt admission',
    'attempt lease lost during shared live execution',
    'native context unavailable',
    'native Agent CLI session locator is missing',
    'shared Session Supervisor is unavailable',
    'cannot settle without a live resident transport',
    'shared member resident transport is unavailable before settlement',
    'attempt dispatch failed (SandboxError)',
    'shared live chat failed unexpectedly: connection reset',
  ])('explains infrastructure failure without exposing internal terms: %s', (detail) => {
    const expected = 'The connection was interrupted before this response could be confirmed.';
    expect(ownerFailureMessage(detail)).toBe(expected);
    expect(toDisplayEvent(event(1, 'error', { text: detail }))).toEqual({
      kind: 'lifecycle',
      text: expected,
      sourceEventId: 1,
    });
    expect(toDisplayJob({ ...JOB, state: 'failed', detail }, {}).stateNote).toBe(expected);
  });

  it('keeps actionable task errors and assistant content unchanged', () => {
    const detail = 'setup failed (1): missing package example';
    expect(ownerFailureMessage(detail)).toBe(detail);
    expect(
      toDisplayEvent(event(1, 'message', { text: 'Investigate why the lease expired.' })),
    ).toEqual({ kind: 'message', text: 'Investigate why the lease expired.', sourceEventId: 1 });
  });

  it('treats a settled boundary as finished, not interrupted', () => {
    for (const detail of [
      'Agent CLI context settled, but job finalization expired; the prompt was not replayed',
      'job finalization expired after context settlement',
    ]) {
      expect(ownerFailureMessage(detail)).toContain('The response finished');
    }
  });

  it('names a repository over the import limit as such, with its size when known', () => {
    const cases: [string, string][] = [
      [
        "source checkout (8.43 GiB including its Git objects) exceeds this host's 1.00 GiB retained import size limit",
        'This repository is larger than this server allows (8.43 GiB; the limit is 1.00 GiB).',
      ],
      [
        "source archive exceeds this host's 1.00 GiB retained import size limit",
        'This repository is larger than this server allows (the limit is 1.00 GiB).',
      ],
      // A host older than the size report names neither.
      [
        'source checkout exceeds the retained import size limit',
        'This repository is larger than this server allows.',
      ],
    ];
    for (const [detail, expected] of cases) {
      expect(ownerFailureMessage(detail)).toBe(expected);
      expect(toDisplayEvent(event(1, 'error', { text: detail }))).toEqual({
        kind: 'lifecycle',
        text: expected,
        sourceEventId: 1,
      });
      const job = toDisplayJob({ ...JOB, state: 'failed', detail }, {});
      expect(job.stateNote).toBe(expected);
      expect(job.failureCause).toBe('repository_too_large');
    }
    const other = toDisplayJob({ ...JOB, state: 'failed', detail: 'agent exited 1: boom' }, {});
    expect(other.failureCause).toBeUndefined();
  });

  it('keeps a site misconfiguration in its own words and marks it for an admin', () => {
    // The runner's SITE_MISCONFIGURED_MCP, verbatim.
    const detail =
      'Site misconfigured: this chat uses connected tools, but the server that ran it ' +
      'has no MCP relay. Ask an admin to finish setting up the site.';
    expect(ownerFailureMessage(detail)).toBe(detail);
    const job = toDisplayJob({ ...JOB, state: 'failed', detail }, {});
    expect(job.stateNote).toBe(detail);
    expect(job.failureCause).toBe('site_misconfigured');
    expect(siteMisconfiguredReason(detail)).toBe(
      'This chat uses connected tools, but the server that ran it has no MCP relay. ' +
        'Ask an admin to finish setting up the site.',
    );
  });

  it('distinguishes execution timeout and cancellation from connection failure', () => {
    expect(ownerFailureMessage('shared Agent CLI turn exceeded its execution timeout')).toBe(
      'This response took too long and was stopped.',
    );
    expect(ownerFailureMessage('cancelled after an indeterminate retained Agent CLI turn')).toBe(
      'This response was stopped at your request.',
    );
  });
});

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

describe('toDisplayState', () => {
  it('separates published from merely finished', () => {
    // Both are idle conversations; per-turn results retain their legacy values.
    expect(toDisplayState({ ...JOB, state: 'succeeded', published_pr_url: 'u' })).toBe('done');
    expect(toDisplayState({ ...JOB, state: 'succeeded', published_pr_url: null })).toBe(
      'needs_review',
    );
  });

  it('shows publishing as still running', () => {
    expect(toDisplayState({ ...JOB, state: 'publishing' })).toBe('running');
  });

  it.each([
    ['queued', 'queued'],
    ['running', 'running'],
    ['failed', 'failed'],
    ['cancelled', 'cancelled'],
  ] as const)('maps %s', (wire, display) => {
    expect(toDisplayState({ ...JOB, state: wire })).toBe(display);
  });
});

describe('conversation display state', () => {
  it('preserves a cancelled turn while carrying the whole conversation activity', () => {
    const job = toDisplayJob({ ...JOB, state: 'cancelled', conversation_state: 'running' });
    expect(job.state).toBe('cancelled');
    expect(job.conversationState).toBe('running');
    expect(getConversationState(job)).toBe('running');
  });
  it.each([
    ['queued', null, 'queued'],
    ['running', null, 'running'],
    ['publishing', null, 'running'],
    ['succeeded', null, 'idle'],
    ['succeeded', 'https://github.com/o/n/pull/1', 'idle'],
    ['failed', null, 'failed'],
    ['cancelled', null, 'idle'],
  ] as const)('displays %s with PR %s as %s', (state, published_pr_url, expected) => {
    const job = toDisplayJob({ ...JOB, state, published_pr_url });
    expect(toConversationState(job.state)).toBe(expected);
    // Presentation never rewrites the individual turn's result or PR metadata.
    expect(job.state).toBe(toDisplayState({ ...JOB, state, published_pr_url }));
  });
});

describe('toDisplayEvent', () => {
  it('keeps raw runtime protocol events out of the terminal transcript', () => {
    expect(
      toDisplayEvent(
        event(1, 'raw', {
          text: '{"type":"system","subtype":"thinking_tokens","estimated_tokens":1}',
        }),
      ),
    ).toBeNull();
  });

  it('reads an older host-only failure as a connection that failed, not a block', () => {
    // Earlier runs recorded failed connections with only the host. The network
    // is open to the public internet, so the row must not claim a policy did it.
    expect(toDisplayEvent(event(1, 'error', { host: 'example.com', attempts: 3 }))).toEqual({
      kind: 'lifecycle',
      text: 'Could not reach example.com',
      sourceEventId: 1,
    });
  });

  it('keeps a plain error as a lifecycle row', () => {
    expect(toDisplayEvent(event(1, 'error', { detail: 'boom' }))).toEqual({
      kind: 'lifecycle',
      text: 'boom',
      sourceEventId: 1,
    });
  });

  it('renders an interruption without its internal reason', () => {
    const row = toDisplayEvent(event(1, 'attempt_superseded', { reason: 'lease_expired' }));
    expect(row).toEqual({ kind: 'lifecycle', text: 'attempt_superseded', sourceEventId: 1 });
  });

  it('shows every provider retry with its reported delay and attempt', () => {
    for (const [attempt, delay] of [
      [1, 5000],
      [2, 10000],
    ]) {
      const display = toDisplayEvent(
        event(attempt, 'lifecycle', {
          phase: 'api_retry',
          error_status: 503,
          attempt,
          max_retries: 10,
          retry_delay_ms: delay,
        }),
      );
      const text = `Provider temporarily unavailable. Retrying in ${delay / 1000}s (retry ${attempt} of 10).`;
      expect(display).toEqual({ kind: 'lifecycle', text, sourceEventId: attempt });
      expect(lifecyclePhaseLabel(text).milestone).toBe(false);
    }
  });

  it.each([
    [{ retry_delay_ms: 0, attempt: 1 }, 'Retrying now (retry 1).'],
    [{ retry_delay_ms: 4500 }, 'Retrying in 5s.'],
    [{}, 'Retrying model request.'],
    [{ retry_delay_ms: -1, attempt: '2', max_retries: Number.NaN }, 'Retrying model request.'],
  ])('does not invent missing retry timing or counts: %j', (payload, text) => {
    expect(toDisplayEvent(event(1, 'lifecycle', { phase: 'api_retry', ...payload }))).toEqual({
      kind: 'lifecycle',
      text,
      sourceEventId: 1,
    });
  });

  it('keeps rate-limit warnings while hiding normal allowance events', () => {
    expect(
      toDisplayEvent(event(1, 'lifecycle', { phase: 'rate_limit', info: { status: 'allowed' } })),
    ).toBeNull();
    expect(
      toDisplayEvent(event(1, 'lifecycle', { phase: 'rate_limit', info: { status: 'rejected' } })),
    ).toEqual({ kind: 'lifecycle', text: 'Model usage limit reached', sourceEventId: 1 });
    expect(
      toDisplayEvent(
        event(1, 'lifecycle', { phase: 'rate_limit', info: { status: 'allowed_warning' } }),
      ),
    ).toEqual({ kind: 'lifecycle', text: 'Approaching model usage limit', sourceEventId: 1 });
  });

  it('keeps an unfamiliar tool visible by its actual name', () => {
    expect(toDisplayEvent(event(1, 'tool_use', { name: 'WebFetch', detail: 'x' }))).toEqual({
      kind: 'tool_use',
      tool: 'WebFetch',
      detail: 'x',
      id: undefined,
      sourceEventId: 1,
    });
  });

  it('keeps tool results available for folding into the matching activity', () => {
    expect(
      toDisplayEvent(
        event(1, 'tool_result', {
          tool_use_id: 'tool_1',
          content: 'all green',
          is_error: false,
        }),
      ),
    ).toEqual({
      kind: 'tool_result',
      text: 'all green',
      toolUseId: 'tool_1',
      isError: false,
      sourceEventId: 1,
    });
    expect(toDisplayEvent(event(1, 'diff'))).toBeNull();
  });

  it('keeps complete tool arguments alongside a compact command and completion output', () => {
    expect(
      toDisplayEvent(
        event(1, 'tool_use', {
          name: 'command_execution',
          input: { command: 'pytest -q', cwd: '/workspace', timeout: 0 },
          output: '1 passed',
          exit_code: 0,
        }),
      ),
    ).toEqual({
      kind: 'tool_use',
      tool: 'command_execution',
      id: undefined,
      detail: 'pytest -q',
      input: { command: 'pytest -q', cwd: '/workspace', timeout: 0 },
      output: ['1 passed'],
      sourceEventId: 1,
    });
  });

  it('keeps internal event kinds out of the conversation', () => {
    expect(toDisplayEvent(event(1, 'something_new'))).toBeNull();
  });

  it('hides internal lifecycle phases while preserving task outcomes and errors', () => {
    for (const phase of ['shared_workspace_settled', 'thinking_tokens', 'base_imported']) {
      expect(toDisplayEvent(event(1, 'lifecycle', { phase }))).toBeNull();
    }
    expect(toDisplayEvent(event(1, 'lifecycle', { phase: 'shared_tool_errors_observed' }))).toEqual(
      {
        kind: 'lifecycle',
        text: 'Some tools encountered errors',
        sourceEventId: 1,
      },
    );
    expect(
      toDisplayEvent(
        event(1, 'error', {
          phase: 'shared_snapshot_publish_rejected',
          detail: 'Could not open pull request',
        }),
      ),
    ).toEqual({ kind: 'lifecycle', text: 'Could not open pull request', sourceEventId: 1 });
    for (const phase of [
      'result',
      'shared_snapshot_publishing',
      'shared_snapshot_published',
      'cancelled_by_owner',
    ]) {
      expect(toDisplayEvent(event(1, 'lifecycle', { phase }))).toEqual({
        kind: 'lifecycle',
        text: phase,
        sourceEventId: 1,
      });
      expect(lifecyclePhaseLabel(phase).milestone).toBe(true);
    }
  });
});

describe('toDisplayJob', () => {
  it.each([null, 'ajob_previous'])(
    'does not infer a queue reason from the parent ID %s',
    (parent_job_id) => {
      const job = toDisplayJob({ ...JOB, state: 'queued', parent_job_id, detail: null });
      expect(job.state).toBe('queued');
      expect(job.stateNote).toBeUndefined();
    },
  );

  it('states the branch the publisher will use', () => {
    expect(toDisplayJob(JOB).branch).toBe('agent/ajob_1');
  });

  it("carries the conversation goal and this round's report", () => {
    const job = toDisplayJob({
      ...JOB,
      turn_source: 'goal',
      goal_round: 3,
      goal_round_kind: 'repair',
      goal_report_parse: 'valid',
      goal_report_outcome: 'needs_input',
      goal_report_detail: 'which environment should this deploy to?',
      goal: {
        goal_id: 'goal_1',
        objective: 'make `pytest` pass',
        state: 'paused',
        revision: 4,
        max_rounds: 10,
        rounds_used: 3,
        pause_reason: 'needs_input',
        pause_detail: 'the agent stopped and asked for input',
        completion_basis: null,
        completed_by_job_id: null,
        created_at: null,
        updated_at: null,
      },
    });
    expect(job.goal).toEqual({
      objective: 'make `pytest` pass',
      state: 'paused',
      revision: 4,
      roundsUsed: 3,
      maxRounds: 10,
      pauseReason: 'needs_input',
      pauseDetail: 'the agent stopped and asked for input',
    });
    expect(job.goalTurn).toEqual({
      source: 'goal',
      round: 3,
      kind: 'repair',
      reportParse: 'valid',
      reportOutcome: 'needs_input',
      reportDetail: 'which environment should this deploy to?',
    });
  });

  it('carries a completion as the model report it is', () => {
    const job = toDisplayJob({
      ...JOB,
      turn_source: 'goal',
      goal_round: 4,
      goal_round_kind: 'work',
      goal_report_parse: 'valid',
      goal_report_outcome: 'complete',
      goal: {
        goal_id: 'goal_1',
        objective: 'make `pytest` pass',
        state: 'complete',
        revision: 4,
        max_rounds: 10,
        rounds_used: 4,
        pause_reason: null,
        pause_detail: null,
        completion_basis: 'model_reported',
        completed_by_job_id: 'ajob_1',
        created_at: null,
        updated_at: null,
      },
    });
    expect(job.goal?.completionBasis).toBe('model_reported');
    expect(job.goalTurn?.reportOutcome).toBe('complete');
  });

  it('says nothing about a goal on a conversation that has none', () => {
    // A deployment older than this feature, and every conversation nobody set
    // a goal on: the transcript must not grow an empty band for either, and a
    // turn must not carry a goal relationship it does not have. Written out
    // inline, this object was on every job in the product.
    const job = toDisplayJob(JOB);
    expect(job.goal).toBeUndefined();
    expect(job.goalTurn).toBeUndefined();
  });

  it("reads no completion report out of a person's own turn", () => {
    // The platform parses the final message of the rounds it queued itself.
    // A report-shaped row on a turn the owner sent is not a report the platform
    // made, and the transcript must not present one as the goal's last word.
    const job = toDisplayJob({
      ...JOB,
      turn_source: 'human',
      goal_report_parse: 'valid',
      goal_report_outcome: 'complete',
      goal_report_detail: 'all green',
    });
    expect(job.goalTurn).toBeUndefined();
  });

  it('maps the conversation pin time', () => {
    expect(toDisplayJob({ ...JOB, pinned_at: '2026-07-29T12:00:00Z' }).pinnedAt).toBe(
      '2026-07-29T12:00:00Z',
    );
  });

  it('uses server-authoritative base and output branch fields', () => {
    const job = toDisplayJob({ ...JOB, base_ref: 'dev', output_branch: 'agent/thread_7' });
    expect(job.baseRef).toBe('dev');
    expect(job.branch).toBe('agent/thread_7');
  });

  it('names the isolation boundary from the current attempt without exposing runtime internals', () => {
    const events = [
      event(1, 'lifecycle', { phase: 'started', sandbox_backend: 'process' }, 10),
      event(
        2,
        'lifecycle',
        {
          phase: 'started',
          sandbox_backend: 'container',
          sandbox_runtime: 'io.containerd.kata.v2',
          sandbox_image: 'registry.example/agent:1',
        },
        11,
      ),
    ];
    const job = toDisplayJob({ ...JOB, current_attempt_id: 11 }, { events });

    expect(job.sandbox).toBe('Isolated VM');
  });

  it.each([
    ['container', 'runc', 'Container'],
    ['process', '', 'Host process'],
    ['kata', '', 'Isolated VM'],
    ['io.containerd.kata.v2', '', 'Isolated VM'],
    ['', 'io.containerd.kata.v2', 'Isolated VM'],
  ])('describes the %s boundary accurately', (backend, runtime, label) => {
    const job = toDisplayJob(JOB, {
      events: [
        event(1, 'lifecycle', {
          phase: 'started',
          sandbox_backend: backend,
          sandbox_runtime: runtime,
        }),
      ],
    });
    expect(job.sandbox).toBe(label);
  });

  it('uses execution timestamps independently of submission and metadata update times', () => {
    const job = toDisplayJob({
      ...JOB,
      created_at: '2026-09-10T10:00:00Z',
      updated_at: '2026-09-10T10:12:00Z',
      started_at: '2026-09-10T10:05:00Z',
      finished_at: '2026-09-10T10:06:00Z',
    });
    expect(job.startedAt).toBe('2026-09-10T10:05:00Z');
    expect(job.finishedAt).toBe('2026-09-10T10:06:00Z');
    const legacy = toDisplayJob({ ...JOB, created_at: '2026-09-10T10:00:00Z' });
    expect(legacy.startedAt).toBeUndefined();
    expect(legacy.finishedAt).toBeUndefined();
  });

  it('distinguishes a readable empty workspace from an unavailable diff', () => {
    expect(toDisplayJob(JOB, { patch: '' }).workspaceDiffAvailable).toBe(true);
    expect(toDisplayJob(JOB, { patch: null }).workspaceDiffAvailable).toBe(false);
    expect(toDisplayJob(JOB).workspaceDiffAvailable).toBe(false);
  });

  it('marks a superseded attempt rather than showing it live', () => {
    const events = [
      event(1, 'message', {}, 10),
      event(2, 'attempt_superseded', { reason: 'lease_expired' }, 10),
      event(3, 'message', {}, 11),
    ];
    const job = toDisplayJob(JOB, { events });
    expect(job.attempts).toHaveLength(2);
    expect(job.attempts[0].status).toBe('superseded');
    expect(job.attempts[0].note).toContain('This run was interrupted.');
    expect(job.attempts[0].note).toContain('Another run started.');
    expect(job.attempts[1].status).toBe('live');
  });

  it('does not invent a replacement when lease loss terminates the only attempt', () => {
    const job = toDisplayJob(
      { ...JOB, state: 'failed', current_attempt_id: 10 },
      {
        events: [
          event(1, 'lifecycle', { phase: 'workspace_preparing' }, 10),
          event(2, 'attempt_superseded', { reason: 'lease_expired' }, 10),
        ],
      },
    );
    expect(job.attempts).toHaveLength(1);
    expect(job.attempts[0].note).toBe('This run was interrupted.');
    expect(lifecyclePhaseLabel('attempt superseded (lease_expired)').text).toBe('Run interrupted');
  });

  it('identifies the server-authoritative current attempt for readiness checks', () => {
    const events = [
      event(1, 'lifecycle', { phase: 'workspace_ready' }, 10),
      event(2, 'attempt_superseded', { reason: 'lease_expired' }, 10),
      event(3, 'lifecycle', { phase: 'started' }, 11),
    ];
    const job = toDisplayJob({ ...JOB, current_attempt_id: 11 }, { events });

    expect(job.currentAttemptNo).toBe(2);
  });

  it('pairs telemetry with attempts by server identity', () => {
    const events = [
      event(1, 'attempt_superseded', { reason: 'lease_expired' }, 10),
      event(2, 'message', {}, 11),
    ];
    const job = toDisplayJob(
      {
        ...JOB,
        current_attempt_id: 11,
        attempt_telemetry: [
          {
            attempt_id: 10,
            attempt_no: 1,
            continuity_mode: 'replay',
            context_status: 'lost',
            duration_ms: 3_000,
          },
          {
            attempt_id: 11,
            attempt_no: 2,
            continuity_mode: 'warm_native',
            context_status: 'restored',
            duration_ms: 9_000,
          },
        ],
      },
      { events },
    );

    expect(job.attempts[0].telemetry).toMatchObject({
      contextStatus: 'lost',
      durationMs: 3_000,
    });
    expect(job.attempts[1].telemetry).toMatchObject({
      contextStatus: 'restored',
      durationMs: 9_000,
    });
    expect(job.attempts.map((attempt) => attempt.id)).toEqual([10, 11]);
    expect(job.attempts.every((attempt) => attempt.telemetry?.resources === undefined)).toBe(true);
    expect(job.attemptTelemetryAvailable).toBe(true);
  });

  it('does not assign a cached snapshot to a new attempt missing from the summary', () => {
    const job = toDisplayJob(
      {
        ...JOB,
        current_attempt_id: 11,
        telemetry_attempt_id: 10,
        duration_ms: 3_000,
        context_status: 'lost',
        attempt_telemetry: [
          {
            attempt_id: 10,
            attempt_no: 1,
            continuity_mode: 'replay',
            context_status: 'lost',
            duration_ms: 3_000,
          },
        ],
      },
      { events: [event(1, 'attempt_superseded', {}, 10), event(2, 'lifecycle', {}, 11)] },
    );
    expect(job.attempts[0].telemetry?.durationMs).toBe(3_000);
    expect(job.attempts[1].telemetry).toBeUndefined();
  });

  it('never uses top-level fallback when a modern server reports no summaries', () => {
    const job = toDisplayJob(
      { ...JOB, attempt_telemetry: [], duration_ms: 3_000 },
      { events: [event(1, 'message')] },
    );
    expect(job.attempts[0].telemetry).toBeUndefined();
  });

  it('retains unmatched tool results', () => {
    const events = [event(1, 'message'), event(2, 'tool_result', { content: 'done' })];
    const job = toDisplayJob(JOB, { events });
    expect(job.events).toHaveLength(2);
  });

  it('leaves unknown operational fields empty rather than inventing them', () => {
    // The API does not report these yet; a fabricated value would be worse
    // than a blank one.
    const job = toDisplayJob(JOB);
    expect(job.gates).toEqual([]);
    expect(job.spentUsd).toBe(0);
  });

  it('folds a tool result into its matching activity and preserves its attempt', () => {
    const events = [
      event(1, 'tool_use', { id: 'tool_1', name: 'Bash', input: { command: 'pytest' } }, 7),
      event(2, 'tool_result', { tool_use_id: 'tool_1', content: '1 passed', is_error: false }, 7),
    ];

    const job = toDisplayJob(JOB, { events });

    expect(job.events).toEqual([
      expect.objectContaining({
        kind: 'tool_use',
        detail: 'pytest',
        output: ['1 passed'],
        attemptNo: 1,
      }),
    ]);
  });

  const piece = (id: number, preview_id: string, delta: string, extra = {}) =>
    event(id, 'raw', { protocol: 'reply-preview', preview_id, delta, ...extra });

  it('grows an answer while it is written, then shows the complete message in its place', () => {
    const streaming = [
      event(1, 'thinking', { text: 'plan', usage_id: 'msg_1' }),
      piece(2, 'msg_1:1', 'Let me '),
      piece(3, 'msg_1:1', 'look.'),
    ];

    expect(toDisplayJob(JOB, { events: streaming }).events).toEqual([
      expect.objectContaining({ kind: 'thinking' }),
      {
        kind: 'message',
        text: 'Let me look.',
        previewId: 'msg_1:1',
        streaming: true,
        sourceEventId: 2,
        attemptNo: 1,
      },
    ]);

    const rows = toDisplayJob(JOB, {
      events: [
        ...streaming,
        event(4, 'message', { text: 'Let me look.', usage_id: 'msg_1', preview_id: 'msg_1:1' }),
      ],
    }).events;
    // The complete message keeps the row's first id and is no longer a preview.
    expect(rows[1]).toEqual({
      kind: 'message',
      text: 'Let me look.',
      previewId: 'msg_1:1',
      sourceEventId: 2,
      attemptNo: 1,
    });
  });

  it('completes each answer where it was written, even when it arrives after later activity', () => {
    // OpenCode and Kilo report their complete parts only when the turn settles.
    const rows = toDisplayJob(JOB, {
      events: [
        piece(1, 'first', 'Let me'),
        piece(2, 'first', ' check.'),
        event(3, 'tool_use', { id: 'call_1', name: 'bash', input: { command: 'pwd' } }),
        event(4, 'tool_result', { tool_use_id: 'call_1', content: '/workspace' }),
        piece(5, 'second', 'Al'),
        piece(6, 'second', 'All done.', { reset: true }),
        event(7, 'message', { text: 'Let me check.', preview_id: 'first' }),
        event(8, 'message', { text: 'All done.', preview_id: 'second' }),
      ],
    }).events;

    expect(rows.map((row) => [row.kind, 'text' in row ? row.text : row.detail])).toEqual([
      ['message', 'Let me check.'],
      ['tool_use', 'pwd'],
      ['message', 'All done.'],
    ]);
    expect(rows.some((row) => row.kind === 'message' && row.streaming)).toBe(false);
  });

  it('shows a complete message on its own when nothing was previewed before it', () => {
    const job = toDisplayJob(JOB, {
      events: [event(1, 'message', { text: 'Hello.', usage_id: 'msg_1' })],
    });
    expect(job.events).toEqual([
      { kind: 'message', text: 'Hello.', sourceEventId: 1, attemptNo: 1 },
    ]);
  });

  it('keeps a message sent into a running turn where it arrived, marked as such', () => {
    expect(
      toDisplayEvent(
        event(9, 'user_message', { text: 'also check the docs', pending_message_id: 'apmsg_1' }),
      ),
    ).toEqual({ kind: 'user_message', text: 'also check the docs', sourceEventId: 9 });
    const job = toDisplayJob(
      { ...JOB, thread_id: 'thread_1', parent_job_id: 'ajob_0', turn_no: 2 },
      {
        thread: {
          thread_id: 'thread_1',
          title: 'Stable conversation title',
          jobs: [],
          messages: [
            { id: 1, role: 'user', content: 'first question', job_id: 'ajob_0', created_at: null },
            {
              id: 2,
              role: 'user',
              content: 'also check the docs',
              job_id: 'ajob_0',
              created_at: null,
              mid_turn: true,
            },
          ],
        },
      },
    );
    expect(job.threadMessages?.map((message) => [message.content, message.midTurn])).toEqual([
      ['first question', undefined],
      ['also check the docs', true],
    ]);
  });

  it('renders earlier thread messages without duplicating the current turn', () => {
    const job = toDisplayJob(
      { ...JOB, thread_id: 'thread_1', parent_job_id: 'ajob_0', turn_no: 2 },
      {
        thread: {
          thread_id: 'thread_1',
          title: 'Stable conversation title',
          jobs: [],
          messages: [
            {
              id: 1,
              role: 'user',
              content: 'first question',
              job_id: 'ajob_0',
              created_at: null,
            },
            {
              id: 2,
              role: 'assistant',
              content: 'first answer',
              job_id: 'ajob_0',
              created_at: null,
            },
            {
              id: 3,
              role: 'user',
              content: JOB.task_prompt,
              job_id: JOB.id,
              created_at: null,
            },
          ],
        },
      },
    );

    expect(job.prompt).toBe(JOB.task_prompt);
    expect(job.title).toBe('Stable conversation title');
    expect(job.turnNo).toBe(2);
    expect(job.branch).toBe('agent/thread_1');
    expect(job.threadMessages?.map((message) => message.content)).toEqual([
      'first question',
      'first answer',
    ]);
  });

  it('reconstructs a previous turn with its tool activity in stream order', () => {
    const current = { ...JOB, id: 'ajob_2', thread_id: 'thread_1', turn_no: 2 };
    const job = toDisplayJob(current, {
      thread: {
        thread_id: 'thread_1',
        jobs: [
          {
            ...JOB,
            id: 'ajob_1',
            thread_id: 'thread_1',
            turn_no: 1,
            native_fork_available: true,
          },
          current,
        ],
        messages: [
          { id: 1, role: 'user', content: 'first question', job_id: 'ajob_1', created_at: null },
          { id: 2, role: 'assistant', content: 'first answer', job_id: 'ajob_1', created_at: null },
        ],
      },
      threadEvents: [
        {
          ...event(1, 'tool_use', { id: 'tool_1', name: 'Bash', input: { command: 'pwd' } }),
          job_id: 'ajob_1',
          turn_no: 1,
        },
        {
          ...event(2, 'tool_result', { tool_use_id: 'tool_1', content: '/workspace' }),
          job_id: 'ajob_1',
          turn_no: 1,
        },
        {
          ...event(3, 'message', { text: 'first answer' }),
          job_id: 'ajob_1',
          turn_no: 1,
        },
      ],
    });

    expect(job.historyTurns).toHaveLength(1);
    expect(job.historyTurns?.[0].forkable).toBe(true);
    expect(job.historyTurns?.[0].messages[0].content).toBe('first question');
    expect(job.historyTurns?.[0].events).toEqual([
      expect.objectContaining({ kind: 'tool_use', detail: 'pwd', output: ['/workspace'] }),
      expect.objectContaining({ kind: 'message', text: 'first answer' }),
    ]);
  });

  it.each([
    ['failed', 'failed'],
    ['cancelled', 'cancelled'],
    ['succeeded', 'needs_review'],
  ] as const)("carries a stored turn's %s outcome onto its rail entry", (state, expected) => {
    // A turn that failed is not the same as a turn that was never classified:
    // the rail says so from the thread's job row rather than from whichever
    // events that turn happens to carry.
    const current = {
      ...JOB,
      id: 'ajob_2',
      thread_id: 'thread_1',
      turn_no: 2,
      state: 'succeeded' as const,
    };
    const job = toDisplayJob(current, {
      thread: {
        thread_id: 'thread_1',
        jobs: [{ ...JOB, id: 'ajob_1', thread_id: 'thread_1', turn_no: 1, state }, current],
        messages: [],
      },
      threadEvents: [],
    });

    expect(toConversationState(job.state)).toBe('idle');
    expect(job.historyTurns?.[0].state).toBe(expected);
  });

  it('renders a settled turn from durable messages when it has no events', () => {
    // A fork's copied anchor never streamed: its thread messages are the only
    // record, prompt included, so the separate prompt card must stand down.
    const job = toDisplayJob(
      { ...JOB, id: 'ajob_2', state: 'succeeded', thread_id: 'thread_1', turn_no: 2 },
      {
        thread: {
          thread_id: 'thread_1',
          jobs: [],
          messages: [
            { id: 1, role: 'user', content: 'first question', job_id: 'ajob_0', created_at: null },
            {
              id: 2,
              role: 'assistant',
              content: 'first answer',
              job_id: 'ajob_0',
              created_at: null,
            },
            { id: 3, role: 'user', content: 'forked question', job_id: 'ajob_2', created_at: null },
            {
              id: 4,
              role: 'assistant',
              content: 'forked answer',
              job_id: 'ajob_2',
              created_at: null,
            },
          ],
        },
      },
    );

    expect(job.threadMessages?.map((message) => message.content)).toEqual([
      'first question',
      'first answer',
      'forked question',
      'forked answer',
    ]);
    expect(job.historyIncludesPrompt).toBe(true);
  });

  it('keeps a live turn rendering from its stream, not from thread copies', () => {
    const job = toDisplayJob(
      { ...JOB, id: 'ajob_2', state: 'running', thread_id: 'thread_1', turn_no: 2 },
      {
        thread: {
          thread_id: 'thread_1',
          jobs: [],
          messages: [
            {
              id: 3,
              role: 'user',
              content: 'current question',
              job_id: 'ajob_2',
              created_at: null,
            },
          ],
        },
      },
    );

    expect(job.threadMessages).toEqual([]);
    expect(job.historyIncludesPrompt).toBe(false);
  });

  it('does not duplicate a settled turn that already streamed its answer', () => {
    const job = toDisplayJob(
      { ...JOB, id: 'ajob_2', state: 'succeeded', thread_id: 'thread_1', turn_no: 2 },
      {
        events: [event(1, 'message', { text: 'streamed answer' })],
        thread: {
          thread_id: 'thread_1',
          jobs: [],
          messages: [
            { id: 3, role: 'user', content: 'q', job_id: 'ajob_2', created_at: null },
            {
              id: 4,
              role: 'assistant',
              content: 'streamed answer',
              job_id: 'ajob_2',
              created_at: null,
            },
          ],
        },
      },
    );

    expect(job.threadMessages).toEqual([]);
    expect(job.historyIncludesPrompt).toBe(false);
  });

  it('does not render turns queued after the job being viewed as history', () => {
    const current = { ...JOB, thread_id: 'thread_1', turn_no: 2 };
    const future = { ...JOB, id: 'ajob_3', thread_id: 'thread_1', turn_no: 3 };
    const job = toDisplayJob(current, {
      thread: {
        thread_id: 'thread_1',
        jobs: [{ ...JOB, id: 'ajob_1', thread_id: 'thread_1', turn_no: 1 }, current, future],
        messages: [
          { id: 1, role: 'user', content: 'first', job_id: 'ajob_1', created_at: null },
          { id: 2, role: 'user', content: 'future', job_id: 'ajob_3', created_at: null },
        ],
      },
    });

    expect(job.threadMessages?.map((message) => message.content)).toEqual(['first']);
  });

  it('carries the PR label through', () => {
    const job = toDisplayJob({
      ...JOB,
      state: 'succeeded',
      published_pr_url: 'https://github.com/o/n/pull/44',
    });
    expect(job.prLabel).toBe('draft PR 44');
  });

  it('keeps the thread draft PR visible on a later no-op turn', () => {
    const previous = {
      ...JOB,
      id: 'ajob_0',
      thread_id: 'thread_1',
      turn_no: 1,
      published_pr_url: 'https://github.com/o/n/pull/44',
    };
    const current = {
      ...JOB,
      thread_id: 'thread_1',
      turn_no: 2,
      state: 'succeeded' as const,
      published_pr_url: null,
    };
    const job = toDisplayJob(current, {
      thread: { thread_id: 'thread_1', jobs: [previous, current], messages: [] },
    });

    expect(job.prUrl).toBe('https://github.com/o/n/pull/44');
    expect(job.prLabel).toBe('draft PR 44');
  });
});

describe('patch parsing', () => {
  const PATCH = [
    'diff --git a/src/x.py b/src/x.py',
    'index 111..222 100644',
    '--- a/src/x.py',
    '+++ b/src/x.py',
    '@@ -1,3 +1,3 @@',
    ' context',
    '-old',
    '+new',
  ].join('\n');

  it('lists changed files', () => {
    expect(toDiffFiles(PATCH)).toEqual(['src/x.py']);
  });

  it('creates per-file diffs and stats for the Git workspace', () => {
    const second = [
      'diff --git a/src/y.py b/src/y.py',
      '--- a/src/y.py',
      '+++ b/src/y.py',
      '@@ -0,0 +1 @@',
      '+created',
    ].join('\n');
    expect(toDiffFileDetails(`${PATCH}\n${second}`)).toEqual([
      expect.objectContaining({ path: 'src/x.py', add: 1, del: 1 }),
      expect.objectContaining({ path: 'src/y.py', add: 1, del: 0 }),
    ]);
  });

  it('counts added and removed lines without the headers', () => {
    expect(toDisplayJob(JOB, { patch: PATCH }).diffStat).toEqual({ add: 1, del: 1 });
  });

  it('handles a job that produced no patch', () => {
    const job = toDisplayJob(JOB, { patch: null });
    expect(job.diffFiles).toEqual([]);
    expect(job.diffStat).toBeUndefined();
  });
});

describe('ledger-sourced fields', () => {
  it('reports spend and usage from the API rather than inventing them', () => {
    const job = toDisplayJob({
      ...JOB,
      spent_usd: 0.0075,
      tokens_in: 74523,
      tokens_out: 512,
      model_calls: 3,
    });

    expect(job.spentUsd).toBe(0.0075);
    expect(job.hasLedger).toBe(true);
    expect(job.usage.tokensIn).toBe('74.5k');
    expect(job.usage.turns).toBe(3);
  });

  it('distinguishes "no ledger" from "spent nothing"', () => {
    // A deployment with no billing ledger must not be shown as a free job.
    const job = toDisplayJob(JOB);

    expect(job.spentUsd).toBe(0);
    expect(job.hasLedger).toBe(false);
    expect(job.usage.tokensIn).toBe('');
    expect(job.usage.turns).toBeNull();
  });
});

describe('lifecycle noise', () => {
  const event = (id: number, event_type: string, payload: Record<string, unknown>) => ({
    id,
    attempt_id: 1,
    seq: id,
    event_type,
    payload,
    created_at: null,
  });

  const job = {
    id: 'ajob_1',
    repo: 'owner/repo',
    task_prompt: 'walk through this repo',
    runtime: 'claude-code',
    model: 'glm-5.1',
    base_sha: null,
    state: 'succeeded' as const,
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

  it('does not render an unknown phase as a completed milestone', () => {
    // The staging run stored 408 of these; each one was drawn with a ✓.
    const { text, milestone } = lifecyclePhaseLabel('thinking_tokens');
    expect(milestone).toBe(false);
    expect(text).toBe('thinking tokens');
    expect(lifecyclePhaseLabel('checked_out')).toEqual({
      text: 'Repository ready',
      milestone: true,
    });
    expect(lifecyclePhaseLabel('workspace_ready')).toEqual({
      text: 'Workspace ready',
      milestone: true,
    });
    expect(lifecyclePhaseLabel('workspace_finalizing')).toEqual({
      text: 'Saving workspace changes',
      milestone: true,
    });
  });

  it('collapses consecutive reasoning rows into one status', () => {
    const events = [
      event(1, 'thinking', { text: '' }),
      event(2, 'thinking', { text: '' }),
      event(3, 'thinking', { text: '' }),
      event(4, 'message', { text: 'done' }),
      event(5, 'thinking', { text: '' }),
    ];
    const kinds = toDisplayJob(job, { events }).events.map((e) => e.kind);
    // Three in a row become one; the later one after a message is its own.
    expect(kinds).toEqual(['thinking', 'message', 'thinking']);
  });

  it('tells the platform starting a turn apart from the agent coming up', () => {
    // These were one label, collapsed when adjacent. They are two different
    // milestones — the platform claimed the turn, then the CLI announced
    // itself — and on a resident turn other milestones fall between them, so
    // the collapse missed and the same words appeared twice.
    const events = [
      event(1, 'lifecycle', { phase: 'started' }),
      event(2, 'lifecycle', { phase: 'checked_out' }),
      event(3, 'lifecycle', { phase: 'init' }),
    ];

    const texts = toDisplayJob(job, { events })
      .events.filter((e) => e.kind === 'lifecycle')
      .map((e) => (e.kind === 'lifecycle' ? lifecyclePhaseLabel(e.text).text : ''));

    expect(texts).toEqual(['Run started', 'Repository ready', 'Agent ready']);
  });

  it('still collapses the same milestone reported twice in a row', () => {
    const events = [
      event(1, 'lifecycle', { phase: 'init' }),
      event(2, 'lifecycle', { phase: 'system' }),
      event(3, 'lifecycle', { phase: 'checked_out' }),
    ];

    const texts = toDisplayJob(job, { events })
      .events.filter((e) => e.kind === 'lifecycle')
      .map((e) => (e.kind === 'lifecycle' ? lifecyclePhaseLabel(e.text).text : ''));

    expect(texts).toEqual(['Agent ready', 'Repository ready']);
  });

  it('says a resident turn reused its tree rather than claiming a checkout', () => {
    // The runner skips the clone entirely on a resident turn. Reporting
    // "Repository ready" either way told the owner their conversation had
    // been rebuilt, contradicting the continuity badge on the same turn.
    const events = [event(1, 'lifecycle', { phase: 'checked_out', reused: true })];

    const texts = toDisplayJob(job, { events })
      .events.filter((e) => e.kind === 'lifecycle')
      .map((e) => (e.kind === 'lifecycle' ? lifecyclePhaseLabel(e.text).text : ''));

    expect(texts).toEqual(['Reusing this conversation\u2019s workspace']);
  });

  it('still reports a real checkout as one', () => {
    const events = [event(1, 'lifecycle', { phase: 'checked_out', reused: false })];

    const texts = toDisplayJob(job, { events })
      .events.filter((e) => e.kind === 'lifecycle')
      .map((e) => (e.kind === 'lifecycle' ? lifecyclePhaseLabel(e.text).text : ''));

    expect(texts).toEqual(['Repository ready']);
  });
});

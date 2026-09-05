// Tests for the API → UI adapter.
//
// The cases that matter are the ones where a wrong mapping would mislead a
// user about their own job: a finished-but-unpublished job looking done, a
// superseded attempt looking live, or an event kind silently vanishing.

import { describe, expect, it } from 'vitest';

import type { AgentJobApi, AgentJobEventApi } from '@/lib/api/agents';

import {
  lifecyclePhaseLabel,
  toDiffFileDetails,
  toDiffFiles,
  toDiffLines,
  toDisplayEvent,
  toDisplayJob,
  toDisplayState,
} from './adapt';

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
  setup_egress_tier: null,
  agent_egress_tier: null,
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
    // Both are `succeeded` on the wire, but only one is done to a user.
    expect(toDisplayState({ ...JOB, state: 'succeeded', published_pr_url: 'u' })).toBe('done');
    expect(toDisplayState({ ...JOB, state: 'succeeded', published_pr_url: null })).toBe(
      'needs_review',
    );
  });

  it('shows publishing as still running', () => {
    expect(toDisplayState({ ...JOB, state: 'publishing' })).toBe('running');
  });

  it.each([
    ['waiting', 'queued'],
    ['queued', 'queued'],
    ['running', 'running'],
    ['failed', 'failed'],
    ['cancelled', 'cancelled'],
  ] as const)('maps %s', (wire, display) => {
    expect(toDisplayState({ ...JOB, state: wire })).toBe(display);
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

  it('promotes an egress denial out of the generic error kind', () => {
    // "the sandbox tried to reach X" is a different story from "it failed".
    expect(toDisplayEvent(event(1, 'error', { host: 'evil.example', attempts: 3 }))).toEqual({
      kind: 'egress_denied',
      host: 'evil.example',
      attempts: 3,
    });
  });

  it('keeps a plain error as a lifecycle row', () => {
    expect(toDisplayEvent(event(1, 'error', { detail: 'boom' }))).toEqual({
      kind: 'lifecycle',
      text: 'boom',
    });
  });

  it('renders a supersede with its reason', () => {
    const row = toDisplayEvent(event(1, 'attempt_superseded', { reason: 'lease_expired' }));
    expect(row).toEqual({ kind: 'lifecycle', text: 'attempt superseded (lease_expired)' });
  });

  it('keeps an unfamiliar tool visible by its actual name', () => {
    expect(toDisplayEvent(event(1, 'tool_use', { name: 'WebFetch', detail: 'x' }))).toEqual({
      kind: 'tool_use',
      tool: 'WebFetch',
      detail: 'x',
      id: undefined,
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
    });
    expect(toDisplayEvent(event(1, 'diff'))).toBeNull();
  });

  it('reads a command and completion output from normalized Codex events', () => {
    expect(
      toDisplayEvent(
        event(1, 'tool_use', {
          name: 'command_execution',
          input: { command: 'pytest -q' },
          output: '1 passed',
          exit_code: 0,
        }),
      ),
    ).toEqual({
      kind: 'tool_use',
      tool: 'command_execution',
      id: undefined,
      detail: 'pytest -q',
      output: ['1 passed'],
    });
  });

  it('surfaces an unknown event kind instead of dropping it', () => {
    expect(toDisplayEvent(event(1, 'something_new'))).toEqual({
      kind: 'lifecycle',
      text: 'something_new',
    });
  });
});

describe('toDisplayJob', () => {
  it('states the branch the publisher will use', () => {
    expect(toDisplayJob(JOB).branch).toBe('agent/ajob_1');
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

  it('shows trusted sandbox metadata for the current attempt', () => {
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

    expect(job.sandbox).toBe('container (io.containerd.kata.v2) · registry.example/agent:1');
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
    expect(job.attempts[0].note).toContain('lease expired');
    expect(job.attempts[1].status).toBe('live');
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

  it('counts every stored event and retains unmatched tool results', () => {
    const events = [event(1, 'message'), event(2, 'tool_result', { content: 'done' })];
    const job = toDisplayJob(JOB, { events });
    expect(job.eventCount).toBe(2);
    expect(job.events).toHaveLength(2);
  });

  it('leaves unknown operational fields empty rather than inventing them', () => {
    // The API does not report these yet; a fabricated value would be worse
    // than a blank one.
    const job = toDisplayJob(JOB);
    expect(job.gates).toEqual([]);
    expect(job.rawLines).toEqual([]);
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
    expect(job.rawLines).toHaveLength(2);
  });

  it('does not expose raw model reasoning in the operational log', () => {
    const job = toDisplayJob(JOB, {
      events: [event(1, 'thinking', { text: 'private chain of thought' })],
    });

    expect(job.rawLines[0]).not.toContain('private chain of thought');
    expect(job.rawLines[0]).toContain('[reasoning hidden]');
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
        jobs: [{ ...JOB, id: 'ajob_1', thread_id: 'thread_1', turn_no: 1 }, current],
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
    expect(job.historyTurns?.[0].messages[0].content).toBe('first question');
    expect(job.historyTurns?.[0].events).toEqual([
      expect.objectContaining({ kind: 'tool_use', detail: 'pwd', output: ['/workspace'] }),
      expect.objectContaining({ kind: 'message', text: 'first answer' }),
    ]);
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

  it('tags lines by marker and drops git noise', () => {
    const lines = toDiffLines(PATCH);
    expect(lines.some((l) => l.text.startsWith('diff --git'))).toBe(false);
    expect(lines.find((l) => l.text === '+new')?.marker).toBe('add');
    expect(lines.find((l) => l.text === '-old')?.marker).toBe('del');
    expect(lines.find((l) => l.text.startsWith('@@'))?.marker).toBe('hunk');
    // File headers are context, not additions — otherwise every diff reads +1.
    expect(lines.find((l) => l.text.startsWith('+++'))?.marker).toBe('ctx');
  });

  it('counts added and removed lines without the headers', () => {
    expect(toDisplayJob(JOB, { patch: PATCH }).diffStat).toEqual({ add: 1, del: 1 });
  });

  it('handles a job that produced no patch', () => {
    const job = toDisplayJob(JOB, { patch: null });
    expect(job.diffFiles).toEqual([]);
    expect(job.diffLines).toEqual([]);
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
      agent_egress_tier: 'platform_only',
    });

    expect(job.spentUsd).toBe(0.0075);
    expect(job.hasLedger).toBe(true);
    expect(job.usage.tokensIn).toBe('74.5k');
    expect(job.usage.turns).toBe(3);
    // The tier is rendered for a reader who does not know the tier names.
    expect(job.networkAgent).toBe('gateway only');
  });

  it('distinguishes "no ledger" from "spent nothing"', () => {
    // A deployment with no billing ledger must not be shown as a free job.
    const job = toDisplayJob(JOB);

    expect(job.spentUsd).toBe(0);
    expect(job.hasLedger).toBe(false);
    expect(job.usage.tokensIn).toBe('');
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
    setup_egress_tier: null,
    agent_egress_tier: null,
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

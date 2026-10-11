import { describe, expect, it } from 'vitest';
import type { AgentJobApi, AgentThreadApi, AgentThreadEventApi } from '@/lib/api/agents';
import { createDisplayJobAdapter, toDisplayJob } from './adapt';

function fixture(turns = 3, eventsPerTurn = 3) {
  const jobs: AgentJobApi[] = Array.from({ length: turns }, (_, index) => ({
    id: `job-${index + 1}`,
    thread_id: 'thread-1',
    turn_no: index + 1,
    repo: 'owner/repo',
    task_prompt: `Question ${index + 1}`,
    runtime: 'claude-code',
    model: 'glm-5.1',
    base_sha: null,
    state: index === turns - 1 ? 'running' : 'succeeded',
    cancel_requested: false,
    current_attempt_id: index + 1,
    published_pr_url: null,
    detail: null,
    metadata: null,
    created_at: null,
    updated_at: null,
    spent_usd: null,
    tokens_in: null,
    tokens_out: null,
    model_calls: null,
  }));
  let payloadReads = 0;
  const events: AgentThreadEventApi[] = jobs.flatMap((job, index) =>
    Array.from({ length: eventsPerTurn }, (_, seq) => ({
      id: index * eventsPerTurn + seq + 1,
      job_id: job.id,
      turn_no: index + 1,
      attempt_id: index + 1,
      seq,
      event_type: 'message',
      created_at: null,
      get payload() {
        payloadReads += 1;
        return { text: `Reply ${index + 1}/${seq}` };
      },
    })),
  );
  const thread: AgentThreadApi = {
    thread_id: 'thread-1',
    jobs,
    messages: jobs.map((job, index) => ({
      id: index + 1,
      job_id: job.id,
      role: 'user',
      content: job.task_prompt,
      created_at: null,
    })),
  };
  return { jobs, events, thread, payloadReads: () => payloadReads };
}

describe('conversation projection reuse', () => {
  it('does not reread old payloads while the current turn streams in a large conversation', () => {
    const { jobs, events, thread, payloadReads } = fixture(100, 100);
    const current = jobs[jobs.length - 1];
    const ownEvents = events.filter((event) => event.job_id === current.id);
    const adapt = createDisplayJobAdapter();
    const first = adapt(current, { thread, threadEvents: events, events: ownEvents });
    expect(first).toEqual(
      toDisplayJob(current, { thread, threadEvents: events, events: ownEvents }),
    );
    const historical = events.filter((event) => event.job_id !== current.id);
    // Keep the current turn ordinary so this count measures historical work.
    const own = ownEvents.map((event) => ({ ...event, payload: event.payload }));
    const readsBefore = payloadReads();
    for (let index = 0; index < 5; index += 1) {
      own.push({
        ...own[0],
        id: events.length + index + 1,
        seq: own.length,
        payload: { text: `New live reply ${index}` },
      });
      const next = adapt(current, {
        thread,
        events: [...own],
        threadEvents: [...historical, ...own],
      });
      expect(next.historyTurns).toBe(first.historyTurns);
      expect(next.events[next.events.length - 1]).toMatchObject({
        kind: 'message',
        text: `New live reply ${index}`,
      });
    }
    expect(payloadReads() - readsBefore).toBe(0);
  });

  it('refreshes late historical results, durable messages, forkability and goal metadata', () => {
    const { jobs, events, thread } = fixture();
    const current = jobs[2];
    const adapt = createDisplayJobAdapter();
    const before = adapt(current, { thread, threadEvents: events });
    const changedThread: AgentThreadApi = {
      ...thread,
      jobs: [
        {
          ...jobs[0],
          state: 'failed',
          native_fork_available: true,
          turn_source: 'goal',
          goal_round: 2,
          goal_report_detail: 'Needs another round',
        },
        ...jobs.slice(1),
      ],
      messages: [...thread.messages, { ...thread.messages[0], id: 10, content: 'Late message' }],
    };
    const changedEvents = [
      ...events,
      { ...events[0], id: 100, seq: 100, payload: { text: 'Late stored response' } },
    ];
    const after = adapt(current, { thread: changedThread, threadEvents: changedEvents });
    expect(after).toEqual(
      toDisplayJob(current, { thread: changedThread, threadEvents: changedEvents }),
    );
    expect(after.historyTurns?.[0]).not.toBe(before.historyTurns?.[0]);
    expect(after.historyTurns?.[1]).toBe(before.historyTurns?.[1]);
    expect(after.historyTurns?.[0]).toMatchObject({
      state: 'failed',
      forkable: true,
      goalTurn: { source: 'goal', round: 2, reportDetail: 'Needs another round' },
    });
    expect(after.historyTurns?.[0].events.at(-1)).toMatchObject({ text: 'Late stored response' });
  });

  it('rebuilds changed historical events even when the job row and messages are unchanged', () => {
    const { jobs, events, thread } = fixture();
    const adapt = createDisplayJobAdapter();
    const before = adapt(jobs[2], { thread, threadEvents: events });
    const replaced = events.map((event, index) =>
      index === 0 ? { ...event, payload: { text: 'Corrected response' } } : event,
    );
    const after = adapt(jobs[2], { thread, threadEvents: replaced });
    expect(after.historyTurns?.[0]).not.toBe(before.historyTurns?.[0]);
    expect(after.historyTurns?.[0].events[0]).toMatchObject({ text: 'Corrected response' });
    expect(after.historyTurns?.[1]).toBe(before.historyTurns?.[1]);
  });

  it('evicts hidden turns and clears projections when the thread changes', () => {
    const { jobs, events, thread } = fixture();
    const adapt = createDisplayJobAdapter();
    const before = adapt(jobs[2], { thread, threadEvents: events });
    const hiddenThread = { ...thread, jobs: thread.jobs.slice(1) };
    expect(
      adapt(jobs[2], { thread: hiddenThread, threadEvents: events }).historyTurns,
    ).toHaveLength(1);
    const restored = adapt(jobs[2], { thread, threadEvents: events });
    expect(restored.historyTurns?.[0]).not.toBe(before.historyTurns?.[0]);
    expect(restored.historyTurns?.[1]).toBe(before.historyTurns?.[1]);
    const otherThread = { ...thread, thread_id: 'thread-2' };
    const other = adapt(jobs[2], { thread: otherThread, threadEvents: events });
    expect(other.historyTurns?.[1]).not.toBe(restored.historyTurns?.[1]);
    expect(
      createDisplayJobAdapter()(jobs[2], { thread, threadEvents: events }).historyTurns?.[0],
    ).not.toBe(restored.historyTurns?.[0]);
  });
});

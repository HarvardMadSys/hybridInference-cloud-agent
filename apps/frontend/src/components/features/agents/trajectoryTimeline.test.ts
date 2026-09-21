import { describe, expect, it } from 'vitest';

import type { AgentThreadEventApi } from '@/lib/api/agents';
import { buildTrajectorySteps } from './trajectorySteps';
import { deriveTimeline, focusKeys, hasRecordedTiming, nearestSpan } from './trajectoryTimeline';

function event(over: Partial<AgentThreadEventApi> & { id: number }): AgentThreadEventApi {
  return {
    attempt_id: 1,
    seq: over.id,
    event_type: 'message',
    payload: {},
    created_at: '2026-08-19T10:00:00.000Z',
    job_id: 'ajob_1',
    turn_no: 1,
    ...over,
  } as AgentThreadEventApi;
}

/** Distinct assistant messages: one step each, because each carries its own id. */
function messages(count: number): AgentThreadEventApi[] {
  return Array.from({ length: count }, (_, index) =>
    event({ id: index + 1, payload: { text: `step ${index + 1}` } }),
  );
}

const TIMED = [
  event({
    id: 1,
    event_type: 'tool_use',
    payload: { id: 't1', name: 'Bash', input: { command: 'cargo build' } },
    created_at: '2026-08-19T10:00:00.000Z',
  }),
  event({
    id: 2,
    event_type: 'tool_result',
    payload: { tool_use_id: 't1', content: 'Compiling' },
    created_at: '2026-08-19T10:00:15.000Z',
  }),
  event({
    id: 3,
    payload: { text: 'the build passes' },
    created_at: '2026-08-19T10:01:00.000Z',
  }),
];

describe('deriveTimeline in steps mode', () => {
  const steps = buildTrajectorySteps(messages(8));
  const model = deriveTimeline(steps, 'steps');

  it('gives every step one operation of the domain', () => {
    expect(steps).toHaveLength(8);
    expect(model.mode).toBe('steps');
    expect(model.start).toBe(0);
    expect(model.end).toBe(steps.length);
    expect(model.spans.map((span) => span.start)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect(model.spans.map((span) => span.end)).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
  });

  it('lets the reader zoom to four operations, and no further', () => {
    expect(model.minimumWindow).toBe(4);
  });

  it('starts a turn boundary at the first step of the turn', () => {
    expect(model.turns).toEqual([{ turnNo: 1, at: 0 }]);
  });

  it('keeps the step number the list shows, and the recorded duration', () => {
    expect(model.spans.map((span) => span.stepNo)).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
    expect(model.spans.every((span) => span.durationMs === null)).toBe(true);
    expect(model.timed).toBe(false);
  });
});

describe('deriveTimeline in time mode', () => {
  const steps = buildTrajectorySteps(TIMED);
  const model = deriveTimeline(steps, 'time');

  it('projects the run by recorded time, with the idle time compressed away', () => {
    const base = Date.parse('2026-08-19T10:00:00.000Z');

    expect(model.mode).toBe('time');
    expect(model.timed).toBe(true);
    // The domain is the recorded clock, so it starts where the run started.
    expect(model.start).toBe(base);
    // The call took fifteen seconds; the message arrived a minute after the call
    // started, and the forty-five seconds of waiting are not drawn.
    expect(model.spans.map((span) => [span.start - base, span.end - base])).toEqual([
      [0, 15_000],
      [15_000, 15_000],
    ]);
    expect(model.end - base).toBe(15_000);
  });

  it("keeps each span's own recorded duration", () => {
    expect(model.spans.map((span) => span.durationMs)).toEqual([15_000, null]);
  });

  it('falls back to the order when the platform recorded no timing', () => {
    const untimed = buildTrajectorySteps(
      messages(3).map((item) => ({ ...item, created_at: '' }) as AgentThreadEventApi),
    );

    expect(hasRecordedTiming(untimed)).toBe(false);
    const fallback = deriveTimeline(untimed, 'time');
    expect(fallback.mode).toBe('steps');
    expect(fallback.end).toBe(3);
  });
});

describe('focusKeys', () => {
  const model = deriveTimeline(buildTrajectorySteps(messages(8)), 'steps');

  it('names the steps an interval covers, boundaries included', () => {
    const keys = focusKeys(model, { start: 2, end: 3 });

    // Three unit spans meet those two values: the interval touches the end of
    // one, the whole of the next, and the start of the one after it.
    expect(keys.size).toBe(3);
    expect(keys.has(model.spans[1].key)).toBe(true);
    expect(keys.has(model.spans[2].key)).toBe(true);
    expect(keys.has(model.spans[3].key)).toBe(true);
    expect(keys.has(model.spans[5].key)).toBe(false);
  });

  it('counts a step that only touches an edge of the interval', () => {
    // Two one-operation spans meet at three; an interval ending inside that
    // boundary names both of them.
    expect(focusKeys(model, { start: 3, end: 3.5 }).size).toBe(2);
  });
});

describe('nearestSpan', () => {
  const model = deriveTimeline(buildTrajectorySteps(messages(8)), 'steps');

  it('finds the step nearest a point between two marks', () => {
    expect(nearestSpan(model, 2.4)?.index).toBe(2);
    expect(nearestSpan(model, 2.9)?.index).toBe(2);
    expect(nearestSpan(model, -5)?.index).toBe(0);
    expect(nearestSpan(model, 99)?.index).toBe(7);
  });
});

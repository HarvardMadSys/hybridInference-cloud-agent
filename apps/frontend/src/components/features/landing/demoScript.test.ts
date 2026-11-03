import { describe, expect, it } from 'vitest';
import {
  AGENT_IDS,
  EXAMPLES,
  STAGES,
  frameAfter,
  initialFrame,
  stageBeats,
  stageDuration,
  visibleTerminal,
} from './demoScript';

describe('the home page demo script', () => {
  it.each(EXAMPLES.map((example) => [example.title, example] as const))(
    '%s starts a new conversation on the agent last used',
    (_, example) => {
      const start = frameAfter(example, -1, 'kilo');
      expect(start).toEqual(initialFrame('kilo'));
      expect(start.sent).toBe(false);
      expect(start.status).toBe('none');
    },
  );

  it.each(EXAMPLES.map((example) => [example.title, example] as const))(
    '%s picks its agent and types the whole prompt before sending',
    (_, example) => {
      const asked = frameAfter(example, 0, 'kilo');
      expect(asked.agent).toBe(example.agent);
      expect(asked.highlight).toBe(example.agent);
      expect(asked.picker).toBe(false);
      expect(asked.typed).toBe(example.prompt);
      expect(asked.sent).toBe(false);
    },
  );

  it.each(EXAMPLES.map((example) => [example.title, example] as const))(
    '%s finishes every step, and its clock ends on the time the answer reports',
    (_, example) => {
      const worked = frameAfter(example, 1, 'kilo');
      expect(worked).toMatchObject({
        sent: true,
        typed: '',
        worked: 'done',
        stepsStarted: example.steps.length,
        stepsDone: example.steps.length,
        clock: example.seconds,
        status: 'ready',
        dot: 'review',
        row: 'hidden',
      });
      // Every line of output is on screen by then.
      expect(visibleTerminal(example, worked)).toEqual(
        example.steps.flatMap((step) => step.terminal ?? []),
      );
    },
  );

  it.each(EXAMPLES.map((example) => [example.title, example] as const))(
    '%s only shows a pull request after Create PR was pressed',
    (_, example) => {
      let frame = frameAfter(example, 1, 'kilo');
      for (const beat of stageBeats(example, 2)) {
        frame = { ...frame, ...beat.patch };
        // The card may say "draft pull request" only once the row says it opened.
        if (frame.status === 'pr') expect(frame.row).toBe('opened');
        if (frame.checksPassed) expect(frame.status).toBe('pr');
      }
      expect(frame).toMatchObject({ pane: 'changes', answer: true, row: 'opened', status: 'pr' });
      expect(frame.checksPassed).toBe(true);
    },
  );

  it('counts the clock up, never down, while the agent works', () => {
    for (const example of EXAMPLES) {
      const clocks = stageBeats(example, 1)
        .map((beat) => beat.patch.clock)
        .filter((clock): clock is number => clock !== undefined);
      expect(clocks).toEqual([...clocks].sort((a, b) => a - b));
      expect(clocks.at(-1)).toBe(example.seconds);
    }
  });

  it('prints a step’s command when it starts and its result when it ends', () => {
    const example = EXAMPLES[0];
    const failing = example.steps.findIndex((step) => step.result === '1 failed');
    const started = { ...initialFrame('claude'), stepsStarted: failing + 1, stepsDone: failing };
    expect(visibleTerminal(example, started).map((line) => line.text)).toEqual([
      '$ npm test -- csv',
    ]);
    const done = { ...started, stepsDone: failing + 1 };
    expect(visibleTerminal(example, done).map((line) => line.tone)).toEqual([
      'command',
      'bad',
      'dim',
    ]);
  });

  it('gives each stage a positive duration that is the sum of its beats', () => {
    for (const example of EXAMPLES) {
      for (const stage of STAGES) {
        const beats = stageBeats(example, stage);
        expect(beats.every((beat) => beat.wait > 0)).toBe(true);
        expect(stageDuration(example, stage)).toBe(
          beats.reduce((total, beat) => total + beat.wait, 0),
        );
      }
    }
  });

  it('plays each example on a different agent the app runs', () => {
    const agents = EXAMPLES.map((example) => example.agent);
    expect(new Set(agents).size).toBe(agents.length);
    for (const agent of agents) expect(AGENT_IDS).toContain(agent);
  });
});

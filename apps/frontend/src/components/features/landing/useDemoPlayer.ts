'use client';

import { useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import {
  EXAMPLES,
  STAGES,
  frameAfter,
  stageBeats,
  stageDuration,
  type DemoFrame,
  type DemoStage,
} from './demoScript';

/** What the demo shows, and where the step list on the left stands. */
export interface DemoView {
  example: number;
  frame: DemoFrame;
  /** The stage the step list marks, and how long its bar takes to fill (0: already full). */
  flow: { stage: DemoStage; duration: number; run: number };
  /** The frame was jumped to: draw it without transitions. */
  instant: boolean;
  /** Fading out between two examples. */
  fading: boolean;
}

/** How long the finished task stays on screen before the next example. */
export const HOLD_MS = 5200;
const FADE_MS = 340;
const SETTLE_MS = 360;

const STOP = Symbol('stopped');
const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

function firstView(calm: boolean): DemoView {
  const last = EXAMPLES[EXAMPLES.length - 1].agent;
  return {
    example: 0,
    frame: frameAfter(EXAMPLES[0], calm ? 2 : -1, last),
    flow: { stage: calm ? 2 : 0, duration: 0, run: 0 },
    instant: true,
    fading: false,
  };
}

interface Pause {
  hover: boolean;
  offscreen: boolean;
}

/**
 * Plays the demo: each example through its three stages, a pause on the
 * result, then the next example. Every run carries a generation; starting
 * anything new bumps it, and a run that finds itself superseded stops at its
 * next beat.
 */
function createPlayer(
  setView: Dispatch<SetStateAction<DemoView>>,
  generation: { current: number },
  pause: { current: Pause },
  calm: boolean,
) {
  let example = 0;

  async function wait(run: number, ms: number) {
    await sleep(ms);
    if (run !== generation.current) throw STOP;
  }

  function jump(index: number, stage: DemoStage | -1) {
    example = index;
    setView((view) => ({
      ...view,
      example: index,
      frame: frameAfter(EXAMPLES[index], stage, view.frame.agent),
      instant: true,
      fading: false,
    }));
  }

  function markFlow(stage: DemoStage, duration: number) {
    setView((view) => ({ ...view, flow: { stage, duration, run: view.flow.run + 1 } }));
  }

  function patch(changes: Partial<DemoFrame>) {
    setView((view) => ({ ...view, frame: { ...view.frame, ...changes }, instant: false }));
  }

  async function playStage(run: number, stage: DemoStage) {
    const current = EXAMPLES[example];
    markFlow(stage, stageDuration(current, stage));
    for (const beat of stageBeats(current, stage)) {
      await wait(run, beat.wait);
      patch(beat.patch);
    }
  }

  async function hold(run: number, ms: number) {
    let left = ms;
    while (left > 0) {
      await wait(run, 100);
      const paused = pause.current.hover || pause.current.offscreen || document.hidden;
      if (!paused) left -= 100;
    }
  }

  async function changeExample(run: number, index: number, fade: boolean) {
    if (fade) {
      setView((view) => ({
        ...view,
        fading: true,
        frame: { ...view.frame, picker: false, status: 'none' },
        instant: false,
      }));
      await wait(run, FADE_MS);
    }
    jump(index, -1);
    markFlow(0, 0);
    await wait(run, SETTLE_MS);
  }

  async function guard(task: () => Promise<void>) {
    try {
      await task();
    } catch (cause) {
      if (cause !== STOP) throw cause;
    }
  }

  /** Play from `index` on, looping through the examples. */
  function autoplay(index: number, { fade = true, delay = 0 } = {}) {
    const run = ++generation.current;
    return guard(async () => {
      let next = index;
      await changeExample(run, next, fade);
      await wait(run, delay);
      for (;;) {
        for (const stage of STAGES) await playStage(run, stage);
        await hold(run, HOLD_MS);
        next = (next + 1) % EXAMPLES.length;
        await changeExample(run, next, true);
      }
    });
  }

  /** Play one stage of the current example from its start, then stop. */
  function play(stage: DemoStage) {
    const run = ++generation.current;
    if (calm) {
      jump(example, stage);
      markFlow(stage, 0);
      return Promise.resolve();
    }
    jump(example, stage === 0 ? -1 : ((stage - 1) as DemoStage));
    return guard(() => playStage(run, stage));
  }

  function choose(index: number) {
    if (!calm) return autoplay(index);
    ++generation.current;
    jump(index, 2);
    markFlow(2, 0);
    return Promise.resolve();
  }

  return {
    autoplay,
    play,
    choose,
    replay: () => (calm ? choose(example) : autoplay(example)),
    stop: () => void ++generation.current,
  };
}

export function useDemoPlayer(calm: boolean) {
  const [view, setView] = useState<DemoView>(() => firstView(calm));
  const generation = useRef(0);
  const pause = useRef<Pause>({ hover: false, offscreen: false });
  const player = useMemo(() => createPlayer(setView, generation, pause, calm), [calm]);

  useEffect(() => {
    if (calm) {
      void player.choose(0);
    } else {
      void player.autoplay(0, { fade: false, delay: 700 });
    }
    return player.stop;
  }, [player, calm]);

  // A jump is drawn once without transitions, then they come back.
  useEffect(() => {
    if (!view.instant) return;
    let second = 0;
    const first = window.requestAnimationFrame(() => {
      second = window.requestAnimationFrame(() =>
        setView((current) => (current.instant ? { ...current, instant: false } : current)),
      );
    });
    return () => {
      window.cancelAnimationFrame(first);
      window.cancelAnimationFrame(second);
    };
  }, [view.instant, view.frame]);

  return { view, player, pause };
}

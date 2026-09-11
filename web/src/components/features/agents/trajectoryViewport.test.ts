import { describe, expect, it } from 'vitest';

import type { DomainRange } from './trajectoryTimeline';
import {
  activeWindow,
  committedRange,
  fractionOf,
  rangeFraction,
  sameZoom,
  shiftedWindow,
  valueAtFraction,
  viewportAfterStep,
  viewportAfterWheel,
  viewportWithValue,
  windowDuration,
  type TimelineViewport,
} from './trajectoryViewport';

const DOMAIN: DomainRange = { start: 0, end: 60_000 };
const MINIMUM_WINDOW = 5_000;
const FITTED = null;

describe('activeWindow', () => {
  it('is the whole domain while nothing is zoomed', () => {
    expect(activeWindow(FITTED, DOMAIN, MINIMUM_WINDOW)).toEqual(DOMAIN);
  });

  it('clamps a window the domain no longer allows', () => {
    // A zoom chosen in a wider run, or before this run grew: the width is what
    // the reader chose, so the window moves rather than narrows.
    expect(activeWindow({ start: -1_000, end: 4_000 }, DOMAIN, MINIMUM_WINDOW)).toEqual({
      start: 0,
      end: 5_000,
    });
    expect(activeWindow({ start: 58_000, end: 68_000 }, DOMAIN, MINIMUM_WINDOW)).toEqual({
      start: 50_000,
      end: 60_000,
    });
  });
});

describe('viewportAfterWheel', () => {
  it('zooms in around the value under the pointer', () => {
    const next = viewportAfterWheel(FITTED, -100, 30_000, 0.5, DOMAIN, MINIMUM_WINDOW);

    expect(next).not.toBeNull();
    const window_ = next as DomainRange;
    expect(windowDuration(window_)).toBeLessThan(windowDuration(DOMAIN));
    expect(valueAtFraction(window_, 0.5)).toBeCloseTo(30_000, 6);
    expect(fractionOf(30_000, window_)).toBeCloseTo(0.5, 6);
  });

  it('keeps a zoom pinned to the left edge', () => {
    const window_ = viewportAfterWheel(FITTED, -100, 0, 0, DOMAIN, MINIMUM_WINDOW);

    expect(window_?.start).toBe(0);
  });

  it('returns to the whole domain when the step zooms all the way out', () => {
    const zoomed = viewportAfterWheel(FITTED, -100, 30_000, 0.5, DOMAIN, MINIMUM_WINDOW);

    expect(viewportAfterWheel(zoomed, 100, 30_000, 0.5, DOMAIN, MINIMUM_WINDOW)).toBeNull();
  });

  it('stops at the narrowest window, and then does nothing at all', () => {
    let viewport: TimelineViewport = FITTED;
    for (let step = 0; step < 40; step += 1) {
      viewport = viewportAfterWheel(viewport, -100, 30_000, 0.5, DOMAIN, MINIMUM_WINDOW);
    }

    expect(windowDuration(activeWindow(viewport, DOMAIN, MINIMUM_WINDOW))).toBeCloseTo(
      MINIMUM_WINDOW,
      6,
    );
    // Reporting "the same zoom" is how the caller knows to leave the wheel to
    // the page rather than swallowing it.
    const pinned = viewportAfterWheel(viewport, -100, 30_000, 0.5, DOMAIN, MINIMUM_WINDOW);
    expect(sameZoom(pinned, viewport, DOMAIN)).toBe(true);
  });

  it('leaves a fitted overview fitted when the wheel zooms out', () => {
    expect(viewportAfterWheel(FITTED, 100, 30_000, 0.5, DOMAIN, MINIMUM_WINDOW)).toBeNull();
    expect(sameZoom(FITTED, FITTED, DOMAIN)).toBe(true);
  });
});

describe('viewportAfterStep', () => {
  it('moves further a press than one wheel notch does', () => {
    const wheeled = viewportAfterWheel(FITTED, -100, 30_000, 0.5, DOMAIN, MINIMUM_WINDOW);
    const stepped = viewportAfterStep(FITTED, 1, 30_000, 0.5, DOMAIN, MINIMUM_WINDOW);

    expect(stepped).not.toBeNull();
    expect(windowDuration(stepped as DomainRange)).toBeLessThan(
      windowDuration(wheeled as DomainRange),
    );
    expect(windowDuration(DOMAIN) / windowDuration(stepped as DomainRange)).toBeCloseTo(1.5, 2);
  });

  it('zooms back out to the whole domain', () => {
    const zoomed = viewportAfterStep(FITTED, 1, 30_000, 0.5, DOMAIN, MINIMUM_WINDOW);

    expect(viewportAfterStep(zoomed, -1, 30_000, 0.5, DOMAIN, MINIMUM_WINDOW)).toBeNull();
  });
});

describe('shiftedWindow', () => {
  it('walks the window without changing its width', () => {
    expect(shiftedWindow({ start: 20_000, end: 30_000 }, 0.5, DOMAIN, MINIMUM_WINDOW)).toEqual({
      start: 15_000,
      end: 25_000,
    });
  });

  it('clamps at the ends of the domain', () => {
    expect(shiftedWindow({ start: 1_000, end: 11_000 }, 0.5, DOMAIN, MINIMUM_WINDOW)).toEqual({
      start: 0,
      end: 10_000,
    });
    expect(shiftedWindow({ start: 50_000, end: 60_000 }, -0.5, DOMAIN, MINIMUM_WINDOW)).toEqual({
      start: 50_000,
      end: 60_000,
    });
  });
});

describe('viewportWithValue', () => {
  it('keeps a window that already shows the value, by identity', () => {
    const viewport = { start: 10_000, end: 20_000 };

    expect(viewportWithValue(viewport, 15_000, DOMAIN, MINIMUM_WINDOW)).toBe(viewport);
  });

  it('pans the shortest way to a value outside it', () => {
    const viewport = { start: 10_000, end: 20_000 };

    expect(viewportWithValue(viewport, 25_000, DOMAIN, MINIMUM_WINDOW)).toEqual({
      start: 15_000,
      end: 25_000,
    });
    expect(viewportWithValue(viewport, 5_000, DOMAIN, MINIMUM_WINDOW)).toEqual({
      start: 5_000,
      end: 15_000,
    });
  });

  it('does nothing while the whole domain is visible', () => {
    expect(viewportWithValue(FITTED, 25_000, DOMAIN, MINIMUM_WINDOW)).toBeNull();
  });
});

describe('committedRange', () => {
  it('orders a drag and widens a short one to the narrowest window', () => {
    expect(committedRange(30_000, 10_000, DOMAIN, MINIMUM_WINDOW)).toEqual({
      start: 10_000,
      end: 30_000,
    });
    expect(committedRange(20_000, 20_100, DOMAIN, MINIMUM_WINDOW)).toEqual({
      start: 17_550,
      end: 22_550,
    });
  });

  it('clamps a selection made at an edge', () => {
    expect(committedRange(0, 100, DOMAIN, MINIMUM_WINDOW)).toEqual({ start: 0, end: 5_000 });
  });

  it('never widens past a run shorter than the narrowest window', () => {
    const short: DomainRange = { start: 0, end: 3_000 };

    expect(committedRange(1_000, 1_100, short, MINIMUM_WINDOW)).toEqual({
      start: 0,
      end: 3_000,
    });
  });
});

describe('rangeFraction', () => {
  it('projects a selection into the visible window', () => {
    expect(rangeFraction({ start: 15_000, end: 45_000 }, { start: 30_000, end: 60_000 })).toEqual({
      start: 0,
      end: 0.5,
    });
  });

  it('clamps a selection that reaches outside the window', () => {
    expect(rangeFraction({ start: 0, end: 60_000 }, { start: 30_000, end: 60_000 })).toEqual({
      start: 0,
      end: 1,
    });
  });
});

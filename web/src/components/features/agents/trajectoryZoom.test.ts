import { describe, expect, it } from 'vitest';

import {
  COLUMN_GAP_PX,
  MAXIMUM_EVENT_WIDTH_PX,
  MINIMUM_FITTED_EVENT_WIDTH_PX,
  TRACK_PADDING_PX,
  columnAt,
  fittedEventWidthPx,
  layoutFor,
  scrollLeftForColumn,
  zoomAfterStep,
  zoomAfterWheel,
} from './trajectoryZoom';

describe('fittedEventWidthPx', () => {
  it('spreads the run across the viewport, gaps included', () => {
    // 40 columns, 39 gaps and the track's own padding are what has to fit.
    expect(fittedEventWidthPx(1000, 40)).toBeCloseTo((1000 - 8 - 39 * 2) / 40, 6);
  });

  it('keeps the pitch that makes a long run scroll instead of smear', () => {
    expect(fittedEventWidthPx(1000, 4000)).toBe(MINIMUM_FITTED_EVENT_WIDTH_PX);
    expect(MINIMUM_FITTED_EVENT_WIDTH_PX + COLUMN_GAP_PX).toBe(12);
  });

  it('survives a viewport that has not been measured yet, and an empty run', () => {
    expect(fittedEventWidthPx(0, 40)).toBe(MINIMUM_FITTED_EVENT_WIDTH_PX);
    expect(fittedEventWidthPx(1000, 0)).toBe(MINIMUM_FITTED_EVENT_WIDTH_PX);
  });
});

describe('layoutFor', () => {
  it('lays out one column per step, plus the gaps between them', () => {
    const layout = layoutFor(null, 0, 10);

    expect(layout.pitchPx).toBe(MINIMUM_FITTED_EVENT_WIDTH_PX + COLUMN_GAP_PX);
    expect(layout.trackWidthPx).toBe(10 * 12 - COLUMN_GAP_PX + 2 * TRACK_PADDING_PX);
  });

  it('fills the viewport exactly when the whole run fits in it', () => {
    expect(layoutFor(null, 1000, 40).trackWidthPx).toBe(1000);
  });

  it('never makes the track narrower than the viewport it sits in', () => {
    expect(layoutFor(null, 1000, 3).trackWidthPx).toBe(1000);
  });

  it('clamps a zoom the viewport no longer allows instead of scrolling into blank space', () => {
    // A zoom chosen in a wider window, or before this run grew.
    expect(layoutFor(1, 1000, 40).eventWidthPx).toBe(fittedEventWidthPx(1000, 40));
    expect(layoutFor(10_000, 1000, 40).eventWidthPx).toBe(MAXIMUM_EVENT_WIDTH_PX);
  });

  it('reports whether any zoom level is left to reach', () => {
    expect(layoutFor(null, 1000, 40).canZoomIn).toBe(true);
    // Three steps across a wide card are already wider than the ceiling.
    expect(layoutFor(null, 1000, 3).canZoomIn).toBe(false);
  });
});

describe('zoomAfterWheel', () => {
  const fitted = fittedEventWidthPx(1000, 40);

  it('opens the columns on a wheel-up and closes them on a wheel-down', () => {
    const zoomedIn = zoomAfterWheel(null, -100, 1000, 40);
    expect(zoomedIn).toBeGreaterThan(fitted);
    expect(zoomAfterWheel(zoomedIn, 100, 1000, 40)).toBeNull();
  });

  it('leaves the wheel alone when there is nothing to zoom out to', () => {
    expect(zoomAfterWheel(null, 100, 1000, 40)).toBeNull();
  });

  it('leaves the wheel alone when the run has no room to zoom in', () => {
    expect(zoomAfterWheel(null, -100, 1000, 3)).toBeNull();
  });

  it('stops at the ceiling without pretending the step did something', () => {
    expect(zoomAfterWheel(MAXIMUM_EVENT_WIDTH_PX, -100, 1000, 40)).toBe(MAXIMUM_EVENT_WIDTH_PX);
  });

  it('never zooms below the fitted width', () => {
    const fittedLong = fittedEventWidthPx(1000, 4000);
    expect(zoomAfterWheel(fittedLong * 2, 1000, 1000, 4000)).toBeNull();
  });
});

describe('zoomAfterStep', () => {
  it('moves further per press than one wheel notch does', () => {
    const step = zoomAfterStep(null, 1, 1000, 40);
    expect(step).not.toBeNull();
    expect((step as number) / fittedEventWidthPx(1000, 40)).toBeCloseTo(1.5, 1);
    expect(zoomAfterStep(step, -1, 1000, 40)).toBeNull();
  });

  it('does nothing at the fitted width when asked to zoom out', () => {
    expect(zoomAfterStep(null, -1, 1000, 40)).toBeNull();
  });
});

describe('anchoring a zoom', () => {
  it('keeps the anchored column under the same x', () => {
    const before = layoutFor(null, 1000, 400);
    const after = layoutFor(MAXIMUM_EVENT_WIDTH_PX, 1000, 400);
    const x = 300;
    const column = columnAt(x, 1200, before);

    const scrollLeft = scrollLeftForColumn(column, x, after, 1000);

    expect(columnAt(x, scrollLeft, after)).toBeCloseTo(column, 6);
  });

  it('clamps the anchor at the ends of the track', () => {
    const layout = layoutFor(null, 1000, 400);

    expect(scrollLeftForColumn(-5, 10, layout, 1000)).toBe(0);
    expect(scrollLeftForColumn(100_000, 10, layout, 1000)).toBe(layout.trackWidthPx - 1000);
  });

  it('never scrolls a track that already fits', () => {
    expect(scrollLeftForColumn(2, 10, layoutFor(null, 1000, 3), 1000)).toBe(0);
  });
});

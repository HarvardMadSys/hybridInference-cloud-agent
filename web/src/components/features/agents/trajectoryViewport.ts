// The overview's viewport: which slice of the domain the reader is looking at,
// and the arithmetic that keeps a zoom under the pointer.
//
// The projection decides what the numbers mean (`steps` counts operations,
// `time` counts milliseconds); nothing here needs to know which. A window is a
// slice of that domain, `null` is the whole of it, and every function returns a
// window the domain allows rather than one the caller has to check afterwards.

import type { DomainRange } from './trajectoryTimeline';

/** The visible slice of the domain, or `null` when the whole run is visible. */
export type TimelineViewport = DomainRange | null;

/**
 * Wheel delta to zoom factor, the constant the desktop harness uses, so a notch
 * feels the same in both: `exp(0.0015 * 100)` is about 1.16x.
 */
export const ZOOM_SENSITIVITY = 0.0015;

export function clampFraction(value: number): number {
  return Math.min(1, Math.max(0, value));
}

export function windowDuration(window: DomainRange): number {
  return Math.max(Number.EPSILON, window.end - window.start);
}

/** A window no wider than the domain and no narrower than the reader may zoom. */
export function clampWindow(
  window: DomainRange,
  domain: DomainRange,
  minimumWindow: number,
): DomainRange {
  const full = windowDuration(domain);
  const floor = Math.min(Math.max(minimumWindow, Number.EPSILON), full);
  const duration = Math.min(full, Math.max(floor, windowDuration(window)));
  const start = Math.min(Math.max(window.start, domain.start), domain.end - duration);
  return { start, end: start + duration };
}

/** The window actually on screen: the reader's, or the whole domain. */
export function activeWindow(
  viewport: TimelineViewport,
  domain: DomainRange,
  minimumWindow: number,
): DomainRange {
  return viewport === null ? domain : clampWindow(viewport, domain, minimumWindow);
}

/** The domain value under a fraction of the track's width. */
export function valueAtFraction(window: DomainRange, fraction: number): number {
  return window.start + clampFraction(fraction) * windowDuration(window);
}

/** Where a domain value sits in the window, as a fraction of its width. */
export function fractionOf(value: number, window: DomainRange): number {
  return (value - window.start) / windowDuration(window);
}

/**
 * The window duration one wheel step lands on, clamped to what the projection
 * allows. A positive delta is a wheel-down: the window grows and the picture
 * zooms out, which is the direction the desktop harness uses.
 */
export function zoomedWindowDuration(
  viewport: TimelineViewport,
  deltaY: number,
  domain: DomainRange,
  minimumWindow: number,
): number {
  const full = windowDuration(domain);
  const floor = Math.min(Math.max(minimumWindow, Number.EPSILON), full);
  const current = windowDuration(activeWindow(viewport, domain, minimumWindow));
  return Math.min(full, Math.max(floor, current * Math.exp(deltaY * ZOOM_SENSITIVITY)));
}

/** The window of `duration` that keeps `anchorValue` under `anchorFraction`. */
export function anchoredWindow(
  duration: number,
  anchorValue: number,
  anchorFraction: number,
  domain: DomainRange,
  minimumWindow: number,
): DomainRange {
  const start = anchorValue - clampFraction(anchorFraction) * duration;
  return clampWindow({ start, end: start + duration }, domain, minimumWindow);
}

/**
 * The viewport after a wheel step. `null` means the step landed back on the
 * whole domain: fitting everything is a state of its own, not a very small
 * zoom, and the difference is what the caller reports to the reader.
 */
export function viewportAfterWheel(
  viewport: TimelineViewport,
  deltaY: number,
  anchorValue: number,
  anchorFraction: number,
  domain: DomainRange,
  minimumWindow: number,
): TimelineViewport {
  const duration = zoomedWindowDuration(viewport, deltaY, domain, minimumWindow);
  if (duration >= windowDuration(domain) * 0.999) return null;
  return anchoredWindow(duration, anchorValue, anchorFraction, domain, minimumWindow);
}

/** The window shifted by a fraction of its own width, clamped to the domain. */
export function shiftedWindow(
  window: DomainRange,
  deltaFraction: number,
  domain: DomainRange,
  minimumWindow: number,
): DomainRange {
  const start = window.start - deltaFraction * windowDuration(window);
  return clampWindow({ start, end: start + windowDuration(window) }, domain, minimumWindow);
}

/**
 * A window that shows `span`, keeping the reader's zoom and moving as little as
 * it can. The span is kept a margin inside the window rather than exactly on its
 * edge, because a mark is drawn from its start rightward: a span parked on the
 * right edge has no visible width at all. The same viewport comes back when the
 * span is already comfortably visible, so a caller can use identity to decide
 * whether anything moved.
 */
export function viewportWithSpan(
  viewport: TimelineViewport,
  span: DomainRange,
  domain: DomainRange,
  minimumWindow: number,
  marginFraction = 0.08,
): TimelineViewport {
  if (viewport === null) return null;
  const window_ = activeWindow(viewport, domain, minimumWindow);
  const duration = windowDuration(window_);
  const margin = duration * marginFraction;
  if (span.start >= window_.start + margin && span.end <= window_.end - margin) return viewport;
  // A span wider than the window is shown from its start: the reader asked for
  // the beginning of it, not for its end.
  const wanted =
    span.end - span.start + 2 * margin > duration || span.start < window_.start + margin
      ? span.start - margin
      : span.end + margin - duration;
  const next = clampWindow({ start: wanted, end: wanted + duration }, domain, minimumWindow);
  return next.start === window_.start ? viewport : next;
}

/** Whether two viewports show the same amount of the domain. */
export function sameZoom(
  left: TimelineViewport,
  right: TimelineViewport,
  domain: DomainRange,
): boolean {
  const first = windowDuration(left ?? domain);
  const second = windowDuration(right ?? domain);
  return Math.abs(first - second) <= Math.max(first, second) * 1e-9;
}

/**
 * The selection a drag commits: ordered, widened to the narrowest window the
 * reader may zoom to, and clamped. A click that moved less than the drag
 * threshold commits the same shape, which is how a click between two marks
 * still names an interval.
 */
export function committedRange(
  anchorValue: number,
  value: number,
  domain: DomainRange,
  minimumWindow: number,
): DomainRange {
  const start = Math.min(anchorValue, value);
  const end = Math.max(anchorValue, value);
  const full = windowDuration(domain);
  const floor = Math.min(Math.max(minimumWindow, Number.EPSILON), full);
  if (end - start >= floor) return { start, end };
  const centre = (start + end) / 2;
  return clampWindow({ start: centre - floor / 2, end: centre + floor / 2 }, domain, floor);
}

/** A selection projected into the visible window, for drawing it. */
export function rangeFraction(range: DomainRange, window: DomainRange): DomainRange {
  const duration = windowDuration(window);
  return {
    start: clampFraction((range.start - window.start) / duration),
    end: clampFraction((range.end - window.start) / duration),
  };
}

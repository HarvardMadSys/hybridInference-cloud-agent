// Zoom arithmetic for the execution overview.
//
// The overview lays one column per step, so how wide that column is belongs to
// the viewport rather than to the data. The component owns the DOM and the
// React state; the numbers live here, where they can be read and tested without
// a browser around them.
//
// The model follows the desktop harness's trajectory timeline: the wheel zooms
// around the pointer, zooming out can never go past "the whole run is laid
// out", and no zoom level makes the columns a smear.

/** Horizontal padding inside one lane row, from that row's `px-1`. */
export const TRACK_PADDING_PX = 4;

/** Distance between two adjacent columns, from the lane grid's `gap-0.5`. */
export const COLUMN_GAP_PX = 2;

/**
 * Narrowest a fitted column gets before the track starts scrolling.
 *
 * Ten pixels of column plus the two-pixel gap is the twelve-pixel pitch the
 * unzoomed overview has always laid out (`steps.length * 12 + 8`), so a run of
 * four hundred steps stays the strip it is today: something you can read,
 * rather than a smear you cannot.
 */
export const MINIMUM_FITTED_EVENT_WIDTH_PX = 10;

/** Widest a column gets when zoomed in: one step, comfortably clickable. */
export const MAXIMUM_EVENT_WIDTH_PX = 64;

/**
 * Wheel delta to zoom factor, the constant the desktop harness uses, so one
 * notch feels the same in both: `exp(0.0015 * 100)` is about 1.16x.
 */
export const ZOOM_SENSITIVITY = 0.0015;

/** Wheel delta a zoom button stands in for: about 1.5x per press. */
export const ZOOM_STEP_DELTA_Y = 270;

export interface OverviewLayout {
  /** Width of one step column. */
  eventWidthPx: number;
  /** Distance from one column's left edge to the next. */
  pitchPx: number;
  /** Width of the scrollable track. */
  trackWidthPx: number;
  /** Whether a zoom level is left between the fitted width and the ceiling. */
  canZoomIn: boolean;
}

/** What a zoom holds still: a column, and the x it sat under. */
export interface ZoomAnchor {
  /** Fractional column index, so a zoom does not snap to a column edge. */
  column: number;
  /** Distance from the track's left edge to the anchored point. */
  x: number;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

/** Width of one column when the whole run is laid out at once. */
export function fittedEventWidthPx(viewportWidthPx: number, count: number): number {
  if (count <= 0) return MINIMUM_FITTED_EVENT_WIDTH_PX;
  const available = viewportWidthPx - 2 * TRACK_PADDING_PX - Math.max(0, count - 1) * COLUMN_GAP_PX;
  return Math.max(MINIMUM_FITTED_EVENT_WIDTH_PX, available / count);
}

/**
 * The layout at a zoom level, or the fitted layout when `zoom` is `null`.
 *
 * A zoom the viewport no longer allows is clamped rather than rejected, so a
 * resize cannot leave the track in a state nobody asked for.
 */
export function layoutFor(
  zoom: number | null,
  viewportWidthPx: number,
  count: number,
): OverviewLayout {
  const fitted = fittedEventWidthPx(viewportWidthPx, count);
  const ceiling = Math.max(fitted, MAXIMUM_EVENT_WIDTH_PX);
  const eventWidthPx = zoom === null ? fitted : clamp(zoom, fitted, ceiling);
  const pitchPx = eventWidthPx + COLUMN_GAP_PX;
  const contentWidthPx = count * pitchPx - (count > 0 ? COLUMN_GAP_PX : 0) + 2 * TRACK_PADDING_PX;
  return {
    eventWidthPx,
    pitchPx,
    trackWidthPx: Math.max(viewportWidthPx, contentWidthPx),
    canZoomIn: fitted < ceiling,
  };
}

/**
 * The zoom level one wheel step lands on.
 *
 * A step that lands where it started is a step with nowhere to go, and the
 * caller reads that as "leave the wheel to the page" rather than swallowing it.
 * A step that reaches the fitted width returns `null`: the fitted overview is a
 * state of its own, not a very small zoom.
 */
export function zoomAfterWheel(
  zoom: number | null,
  deltaY: number,
  viewportWidthPx: number,
  count: number,
): number | null {
  const fitted = fittedEventWidthPx(viewportWidthPx, count);
  const ceiling = Math.max(fitted, MAXIMUM_EVENT_WIDTH_PX);
  const current = clamp(zoom ?? fitted, fitted, ceiling);
  const next = clamp(current * Math.exp(-deltaY * ZOOM_SENSITIVITY), fitted, ceiling);
  if (next === current) return zoom;
  return next <= fitted * 1.0005 ? null : next;
}

/** The zoom level a button press lands on; `direction` is 1 in, -1 out. */
export function zoomAfterStep(
  zoom: number | null,
  direction: 1 | -1,
  viewportWidthPx: number,
  count: number,
): number | null {
  return zoomAfterWheel(zoom, -direction * ZOOM_STEP_DELTA_Y, viewportWidthPx, count);
}

/** The fractional column under a viewport x, for anchoring a zoom on it. */
export function columnAt(x: number, scrollLeft: number, layout: OverviewLayout): number {
  return (scrollLeft + x - TRACK_PADDING_PX) / layout.pitchPx;
}

/** The scrollLeft that leaves `column` under the same viewport x. */
export function scrollLeftForColumn(
  column: number,
  x: number,
  layout: OverviewLayout,
  viewportWidthPx: number,
): number {
  const maximum = Math.max(0, layout.trackWidthPx - viewportWidthPx);
  return clamp(column * layout.pitchPx - x + TRACK_PADDING_PX, 0, maximum);
}

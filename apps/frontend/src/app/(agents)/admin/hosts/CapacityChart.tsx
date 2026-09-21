'use client';

// One capacity line chart, drawn as plain inline SVG.
//
// There is no chart library in this app and this change does not add one: two
// fixed-scale line charts over a minute grid are a polyline, an axis and a
// tooltip, and a dependency would be a hundred kilobytes and a styling
// vocabulary for that.
//
// What the drawing refuses to do is as deliberate as what it does:
//
//   * a straight segment joins consecutive observed minutes, and nothing is
//     smoothed — a curve through three points invents the two between them;
//   * a minute nobody observed, or a value the source could not measure, ends
//     the segment. The line does not cross it and it is not drawn as zero;
//   * the x axis spans the response's own `since`..`until`, so a gap is as
//     wide as the time it represents rather than being closed up;
//   * the y axis is fixed at 0-100% so the two charts can be read against each
//     other, and a flat line at 3% looks flat.
//
// Everything a reader needs is reachable without seeing the SVG: a title, a
// text summary, and a slider that walks the observed points with the arrow
// keys and announces each one's time and value.

import { useId, useMemo, useState } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react';

import type { AgentHostCapacityPoint, CapacityRange } from '@/lib/api/admin';

import {
  type CapacityMetric,
  CHART_VIEW,
  CHART_Y_TICKS,
  capacitySeries,
  chartGeometry,
  chartSummaryText,
  chartXForTime,
  chartYForValue,
  formatPointTooltip,
  formatUtcAxisTick,
  gapSpans,
  highestObserved,
  historyIsAccumulating,
  nearestPointIndex,
  observedPointCount,
  svgXFromClientX,
} from './capacity';

/** A gap narrower than this is a hairline and its label would be noise. */
const MIN_GAP_LABEL_WIDTH = 24;
/** At most this many `No data` labels per chart, so a broken hour stays legible. */
const MAX_GAP_LABELS = 3;

export interface CapacityChartProps {
  metric: CapacityMetric;
  /** The chart's accessible name, e.g. `CPU usage (%)`. */
  title: string;
  points: AgentHostCapacityPoint[];
  /** The response's own window, so gaps sit at their true width. */
  since: string;
  until: string;
  resolutionSeconds: number;
  range: CapacityRange;
  /** True when the newest observation is stale: nothing is drawn up to now. */
  stale: boolean;
}

export default function CapacityChart({
  metric,
  title,
  points,
  since,
  until,
  resolutionSeconds,
  range,
  stale,
}: CapacityChartProps) {
  const titleId = useId();
  const summaryId = useId();
  const [selection, setSelection] = useState<{ key: string; index: number } | null>(null);

  const series = useMemo(() => capacitySeries(points, metric), [points, metric]);
  const geometry = useMemo(
    () => chartGeometry(series, since, until, resolutionSeconds),
    [series, since, until, resolutionSeconds],
  );
  const gaps = useMemo(() => gapSpans(series, since, until), [series, since, until]);
  const summary = useMemo(
    () => chartSummaryText(metric, series, since, until),
    [metric, series, since, until],
  );

  // Selection is keyed to the series it was made on. A redrawn window (a poll,
  // a range change, the other server) invalidates it without an effect, so a
  // tooltip can never be left pointing at a minute that is no longer on screen.
  const seriesKey = `${metric}:${since}:${until}:${points.length}`;
  const selectedIndex =
    selection !== null && selection.key === seriesKey && selection.index < geometry.points.length
      ? selection.index
      : null;
  const selected = selectedIndex === null ? null : geometry.points[selectedIndex];
  const tooltip = selected === null ? null : formatPointTooltip(metric, selected.point);

  const accumulating = historyIsAccumulating(series, since, resolutionSeconds);
  const observed = observedPointCount(series);
  const highest = highestObserved(series);

  const select = (index: number): void => {
    setSelection({ key: seriesKey, index });
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>): void => {
    const last = geometry.points.length - 1;
    if (last < 0) return;
    // From nothing selected, the first arrow press lands on the newest point:
    // the rightmost arrow then walks backwards into the window.
    const from = selectedIndex ?? last;
    let next: number | null = null;
    if (event.key === 'ArrowLeft') next = from - 1;
    else if (event.key === 'ArrowRight') next = from + 1;
    else if (event.key === 'PageUp') next = from + 10;
    else if (event.key === 'PageDown') next = from - 10;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = last;
    if (next === null) return;
    event.preventDefault();
    select(Math.min(last, Math.max(0, next)));
  };

  const handlePointer = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (geometry.points.length === 0) return;
    const x = svgXFromClientX(event.currentTarget.getBoundingClientRect(), event.clientX);
    const index = nearestPointIndex(geometry.points, x);
    if (index >= 0) select(index);
  };

  // The two ends of the window, and nothing between them: the middle tick sat
  // exactly where an axis title belongs, and a tick and a title on the same
  // column read as one label that wrapped. Two ends are what this axis is for —
  // the window is stated in the panel above it, and the reader is reading a
  // shape, not a value off the axis.
  const labelTicks = [since, until];
  const wideGaps = gaps
    .filter((gap) => gap.endX - gap.startX >= MIN_GAP_LABEL_WIDTH)
    .slice(0, MAX_GAP_LABELS);
  const sinceMs = Date.parse(since);
  const untilMs = Date.parse(until);

  return (
    <figure className="m-0 flex min-w-0 flex-col">
      {/* Title and its figure on the same line, next to each other. Pushed to
          opposite ends of a half-width column, this row put the CPU peak
          directly against the memory chart's title, where it read as the
          memory number. */}
      <figcaption className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-sm font-semibold text-gray-900 dark:text-gray-50">{title}</span>
        <span className="text-[11px] text-gray-500 dark:text-gray-400">
          {highest === null
            ? `· ${observed} observations`
            : `· Highest observed in range: ${highest.toFixed(1)}%`}
        </span>
      </figcaption>

      {accumulating && (
        <p className="mt-1 text-[11px] text-amber-700 dark:text-amber-300">
          History is accumulating
          {observed > 0 ? ` · ${observed} of ${series.length} minutes observed` : ''}
        </p>
      )}

      <div className="relative mt-2">
        <svg
          viewBox={`0 0 ${CHART_VIEW.width} ${CHART_VIEW.height}`}
          // Derived from the view box rather than written twice: a class value
          // here and a constant in capacity.ts drift silently, and the drift
          // shows up as tick labels that no longer line up with the plot.
          style={{ aspectRatio: `${CHART_VIEW.width} / ${CHART_VIEW.height}` }}
          className={`w-full touch-pan-y select-none ${stale ? 'opacity-70' : ''}`}
          role="img"
          aria-labelledby={titleId}
          aria-describedby={summaryId}
        >
          <title id={titleId}>{title}</title>

          {CHART_Y_TICKS.map((tick) => {
            const y = chartYForValue(tick);
            return (
              <g key={tick}>
                <line
                  x1={geometry.plotLeft}
                  x2={geometry.plotRight}
                  y1={y}
                  y2={y}
                  className="stroke-gray-200/80 dark:stroke-white/10"
                  strokeWidth={1}
                />
                <text
                  x={geometry.plotLeft - 6}
                  y={y + 3.5}
                  textAnchor="end"
                  fontSize={10}
                  className="fill-gray-500 dark:fill-gray-400"
                >
                  {tick}%
                </text>
              </g>
            );
          })}

          <line
            x1={geometry.plotLeft}
            x2={geometry.plotLeft}
            y1={geometry.plotTop}
            y2={geometry.plotBottom}
            className="stroke-gray-200 dark:stroke-white/15"
            strokeWidth={1}
          />
          <line
            x1={geometry.plotLeft}
            x2={geometry.plotRight}
            y1={geometry.plotBottom}
            y2={geometry.plotBottom}
            className="stroke-gray-200 dark:stroke-white/15"
            strokeWidth={1}
          />

          {labelTicks.map((tick, index) => (
            <text
              key={tick}
              x={chartXForTime(Date.parse(tick), sinceMs, untilMs)}
              y={geometry.plotBottom + 15}
              textAnchor={
                index === 0 ? 'start' : index === labelTicks.length - 1 ? 'end' : 'middle'
              }
              fontSize={10}
              className="fill-gray-500 dark:fill-gray-400"
            >
              {formatUtcAxisTick(tick, range)}
            </text>
          ))}
          {/* The zone is spelled out rather than implied: an operator reading
              this beside a machine's own logs must not have to guess. It is
              centred under the axis as its title, which is what it is. At the
              right end it landed on a second line under "09:58" and read as a
              tick label that wrapped; left-aligned it landed under the first
              tick and read the same way; in the label column it read as that
              tick's prefix. */}
          <text
            x={(geometry.plotLeft + geometry.plotRight) / 2}
            y={geometry.plotBottom + 27}
            textAnchor="middle"
            fontSize={10}
            className="fill-gray-500 dark:fill-gray-400"
          >
            UTC
          </text>

          {geometry.segments.map((segment) =>
            segment.length === 1 ? (
              // A single observed minute between two gaps is real data, and a
              // one-point polyline would draw nothing at all.
              <circle
                key={segment[0].point.bucketAt}
                cx={segment[0].x}
                cy={segment[0].y}
                r={2.5}
                className={lineClass(stale)}
              />
            ) : (
              // The run is filled down to the baseline before it is stroked.
              // On a fixed 0-100 axis an idle machine's line sits within a few
              // pixels of the baseline, and without the fill a working chart
              // and an empty one look the same at a glance. The polygon takes
              // the segment's own points, so a gap is still a gap: nothing is
              // filled across a minute nobody observed.
              <g key={segment[0].point.bucketAt}>
                <polygon
                  points={[
                    ...segment.map((point) => `${point.x},${point.y}`),
                    `${segment[segment.length - 1].x},${geometry.plotBottom}`,
                    `${segment[0].x},${geometry.plotBottom}`,
                  ].join(' ')}
                  className={areaClass(stale)}
                />
                <polyline
                  points={segment.map((point) => `${point.x},${point.y}`).join(' ')}
                  fill="none"
                  strokeWidth={1.5}
                  strokeLinejoin="round"
                  strokeLinecap="round"
                  className={lineClass(stale)}
                />
              </g>
            ),
          )}

          {wideGaps.map((gap) => (
            <text
              key={gap.key}
              x={(gap.startX + gap.endX) / 2}
              y={geometry.plotTop + 11}
              textAnchor="middle"
              fontSize={9}
              className="fill-gray-400 dark:fill-gray-500"
            >
              No data
            </text>
          ))}

          {selected && (
            <g>
              <line
                x1={selected.x}
                x2={selected.x}
                y1={geometry.plotTop}
                y2={geometry.plotBottom}
                className="stroke-gray-300 dark:stroke-white/25"
                strokeWidth={1}
                strokeDasharray="3 3"
              />
              <circle
                cx={selected.x}
                cy={selected.y}
                r={3.5}
                className="fill-crimson dark:fill-crimson-light"
              />
            </g>
          )}
        </svg>

        {geometry.points.length === 0 && (
          <p className="absolute inset-0 flex items-center justify-center px-4 text-center text-xs text-gray-500 dark:text-gray-400">
            {stale ? 'No data in this range' : 'History is accumulating'}
          </p>
        )}

        {selected && tooltip && (
          // aria-hidden because the slider below announces the same sentence:
          // reading it twice is worse than not reading it here.
          //
          // The bubble is centred on its point except near either edge of the
          // plot, where centring it put half of it outside the panel and the
          // text was clipped mid-word. Switching the anchor by position — not
          // by measuring the bubble — needs no layout pass and cannot disagree
          // with what is painted.
          <div
            aria-hidden
            style={{ left: `${(selected.x / CHART_VIEW.width) * 100}%` }}
            className={`pointer-events-none absolute top-0 whitespace-nowrap rounded-md border border-gray-200 bg-white px-2 py-0.5 text-[11px] text-gray-700 shadow-sm dark:border-white/10 dark:bg-gray-900 dark:text-gray-200 ${tooltipAnchor(
              selected.x,
            )}`}
          >
            {tooltip}
          </div>
        )}

        {geometry.points.length > 0 && (
          <div
            role="slider"
            tabIndex={0}
            aria-label={`${title} point selector`}
            aria-valuemin={0}
            aria-valuemax={geometry.points.length - 1}
            aria-valuenow={selectedIndex ?? 0}
            aria-valuetext={tooltip ?? 'No point selected'}
            onKeyDown={handleKeyDown}
            onFocus={() => {
              if (selectedIndex === null) select(geometry.points.length - 1);
            }}
            onPointerMove={handlePointer}
            onPointerDown={handlePointer}
            className="absolute inset-0 cursor-crosshair rounded-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson dark:focus-visible:ring-crimson-light"
          />
        )}
      </div>

      <p id={summaryId} className="sr-only">
        {summary}
      </p>
    </figure>
  );
}

function lineClass(stale: boolean): string {
  return stale
    ? 'stroke-amber-600/70 dark:stroke-amber-400/70'
    : 'stroke-crimson dark:stroke-crimson-light';
}

/** The wash under a stroked run. Same colour, low enough to read as ground. */
function areaClass(stale: boolean): string {
  return stale ? 'fill-amber-500/10 dark:fill-amber-400/10' : 'fill-crimson/10';
}

/**
 * How far from either edge of the plot the bubble must be to be centred safely.
 *
 * In SVG user units, and deliberately generous: the widest reading this chart
 * draws is a memory tooltip carrying its byte detail, and half of that has to
 * fit between the point and the edge. Being too generous only means the bubble
 * stops centring slightly sooner, which costs nothing.
 */
const TOOLTIP_EDGE = 150;

function tooltipAnchor(x: number): string {
  if (x < CHART_VIEW.left + TOOLTIP_EDGE) return 'translate-x-0';
  if (x > CHART_VIEW.width - CHART_VIEW.right - TOOLTIP_EDGE) return '-translate-x-full';
  return '-translate-x-1/2';
}

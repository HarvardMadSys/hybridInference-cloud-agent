'use client';

// The trajectory: every stored event of the conversation, projected into the
// steps a reader can inspect.
//
// Tool results join their calls; runtime records stay out of the execution
// overview entirely, reachable per turn through Run details. Unknown events
// remain visible; export keeps every row.
// Selecting a row no longer opens an inline panel: the detail goes to the
// right-hand inspector, so the list keeps its place while a step is read.
//
// The overview above the list is those same steps as a picture: one mark per
// step, on its lane, projected either by the order it happened in (`steps`) or
// by the time the platform recorded (`time`). It zooms around the pointer, pans
// with the right button and at the edges, and selects an interval that dims the
// rows outside it. The picture is a way to find a row, never a second list.

import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type KeyboardEvent,
  type PointerEvent,
} from 'react';

import type { AgentJobApi, AgentJobEventApi, AgentThreadEventApi } from '@/lib/api/agents';
import { filterSteps, type TrajectoryStep } from './trajectorySteps';
import { preview, toSessionLog, type TrajectoryLane } from './trajectory';
import { formatDuration } from './TurnFooter';
import { TrajectoryTimeBreakdown } from './TrajectoryTimeBreakdown';
import {
  deriveTimeline,
  focusKeys,
  hasRecordedTiming,
  nearestSpan,
  type DomainRange,
  type TimelineMode,
  type TimelineModel,
} from './trajectoryTimeline';
import {
  activeWindow,
  clampFraction,
  committedRange,
  rangeFraction,
  sameZoom,
  shiftedWindow,
  valueAtFraction,
  viewportAfterWheel,
  viewportWithSpan,
  windowDuration,
  type TimelineViewport,
} from './trajectoryViewport';

const LANES: { key: TrajectoryLane; label: string; tone: string }[] = [
  { key: 'input', label: 'Input', tone: 'bg-sky-400' },
  { key: 'model', label: 'Model', tone: 'bg-violet-400' },
  { key: 'tools', label: 'Tools', tone: 'bg-amber-400' },
  { key: 'other', label: 'Other', tone: 'bg-gray-300 dark:bg-gray-600' },
];
const EMPTY_TIMING_JOBS: AgentJobApi[] = [];
const EMPTY_SUBAGENT_OPTIONS: readonly { value: string; label: string }[] = [];

const ACTOR_TONE: Record<TrajectoryStep['actor'], string> = {
  user: 'bg-sky-50 dark:bg-sky-500/10 text-sky-700 dark:text-sky-400',
  assistant: 'bg-violet-50 dark:bg-violet-500/10 text-violet-700 dark:text-violet-400',
  tool: 'bg-amber-50 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400',
  context: 'bg-emerald-50 dark:bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
  system: 'bg-gray-100 dark:bg-white/5 text-gray-600 dark:text-gray-400',
  runtime: 'bg-gray-100 dark:bg-white/5 text-gray-600 dark:text-gray-400',
};

const STATUS_TONE: Partial<Record<TrajectoryStep['status'], string>> = {
  failed: 'text-rose-600 dark:text-rose-400',
  waiting: 'text-amber-600 dark:text-amber-400',
  not_recorded: 'text-gray-400 dark:text-gray-500',
};

/** One lane row: 20 pixels of rail and 6 of air, the pitch marks are placed on. */
const LANE_PITCH_PX = 26;
const LANE_HEIGHT_PX = 20;
const MARK_HEIGHT_PX = 14;
/**
 * Width of a mark whose interval is a single instant. Everything wider fills
 * the slot its step owns, so a lane reads as the band of work it is.
 */
const POINT_MARK_WIDTH_PX = 2;
/** A click that moved less than this selects; more than this drags a range. */
const MINIMUM_DRAG_PX = 3;
/** While dragging past an edge, the window follows at this strength and speed. */
const EDGE_PAN_ZONE_FRACTION = 0.08;
/** Window widths walked per animation frame, so about 0.6 a second at 60fps. */
const EDGE_PAN_FRAME_FRACTION = 0.01;
const MAXIMUM_EDGE_PAN_PX = 32;

/** A control above the picture: quiet, small, and secondary to it. */
const CONTROL =
  'flex h-5 min-w-[1.25rem] items-center justify-center rounded-md border border-gray-200 px-1 text-[11px] leading-none text-gray-500 transition-colors hover:border-gray-300 hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-40 dark:border-white/10 dark:text-gray-400 dark:hover:border-white/20 dark:hover:text-gray-100';

/** Pointer capture, where the environment has it (jsdom does not). */
function capturePointer(element: HTMLElement, pointerId: number): void {
  try {
    element.setPointerCapture?.(pointerId);
  } catch {
    // A synthetic pointer has no active pointer to capture.
  }
}

/** The step a pointer event landed on, when it landed on a mark. */
function spanKeyAt(target: EventTarget | null): string | null {
  const element =
    target instanceof HTMLElement ? target.closest<HTMLElement>('[data-span-key]') : null;
  return element?.dataset.spanKey ?? null;
}

/** A step key as a selector, with or without the CSS API the browser may lack. */
function escapeKey(key: string): string {
  return typeof CSS !== 'undefined' && typeof CSS.escape === 'function'
    ? CSS.escape(key)
    : key.replace(/["\\]/g, '\\$&');
}

function orderedRange(left: number, right: number): DomainRange {
  return left <= right ? { start: left, end: right } : { start: right, end: left };
}

/**
 * The execution overview: one mark per step, projected onto four lanes.
 *
 * The timeline is not a scroll area: zoom and pan move a window over the
 * projection, and the marks are positioned in it, so a deep zoom never fights
 * the page for the wheel and never depends on a measured width.
 */
function ExecutionOverview({
  model,
  mode,
  onModeChange,
  timed,
  range,
  onRangeChange,
  selectedKey,
  searchMatchKeys,
  onSelect,
}: {
  model: TimelineModel;
  mode: TimelineMode;
  onModeChange: (mode: TimelineMode) => void;
  /** Whether this run recorded any timing the `time` projection could use. */
  timed: boolean;
  range: DomainRange | null;
  onRangeChange: (range: DomainRange | null) => void;
  selectedKey: string | null;
  /** Steps the active search matches, or null without a query. */
  searchMatchKeys: ReadonlySet<string> | null;
  onSelect: (key: string) => void;
}) {
  const lanes = LANES.map((lane) => ({
    ...lane,
    count: model.spans.filter((span) => span.lane === lane.key).length,
  })).filter((lane) => lane.count > 0);
  const laneIndex = new Map(lanes.map((lane, index) => [lane.key, index] as const));

  const trackRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    anchorValue: number;
    anchorClientX: number;
    key: string | null;
  } | null>(null);
  const panRef = useRef<{
    pointerId: number;
    anchorClientX: number;
    /** The window at pointer-down. A pan is measured from there, never from
     * the window the previous move already produced. */
    anchorStart: number;
    span: number;
    moved: boolean;
  } | null>(null);
  /** The edge the pointer is held past while dragging, and at what strength. */
  const edgeRef = useRef<{
    direction: -1 | 0 | 1;
    strength: number;
    fraction: number;
  } | null>(null);
  const edgeFrameRef = useRef<number | null>(null);
  const [viewport, setViewport] = useState<TimelineViewport>(null);
  // The frame loop runs between renders, so it keeps its own copy of the window
  // and of the projection it is walking: a step appended over SSE must reach a
  // gesture that is already in flight.
  const viewportRef = useRef<TimelineViewport>(null);
  const modelRef = useRef<TimelineModel>(model);
  const [animate, setAnimate] = useState(false);
  const [hover, setHover] = useState<{ fraction: number; key: string | null } | null>(null);
  const [draft, setDraft] = useState<DomainRange | null>(null);
  const [panning, setPanning] = useState(false);

  useEffect(() => {
    viewportRef.current = viewport;
  }, [viewport]);

  useEffect(() => {
    modelRef.current = model;
  }, [model]);

  const window_ = activeWindow(viewport, model, model.minimumWindow);
  const windowSpan = windowDuration(window_);
  const fullSpan = windowDuration(model);
  const activeRange = draft ?? range;
  const painted = activeRange === null ? null : rangeFraction(activeRange, window_);
  const selectedSpan =
    selectedKey === null ? null : (model.spans.find((span) => span.key === selectedKey) ?? null);
  const plotHeight = Math.max(
    LANE_HEIGHT_PX,
    lanes.length * LANE_PITCH_PX - (LANE_PITCH_PX - LANE_HEIGHT_PX),
  );

  // The projection lives in its own layer, wider than the track by exactly the
  // zoom: every mark is then positioned by its place in the whole run, and the
  // layer decides which slice of it the reader sees.
  const domainStyle: CSSProperties = {
    left: `${(-(window_.start - model.start) / windowSpan) * 100}%`,
    width: `${(fullSpan / windowSpan) * 100}%`,
  };

  // React registers `wheel` at the root as a passive listener, so zooming is
  // attached by hand: the handler has to be able to preventDefault, or the page
  // scrolls away while the window shrinks.
  useEffect(() => {
    const element = trackRef.current;
    if (element === null) return undefined;
    const onWheel = (event: WheelEvent): void => {
      // A trackpad's horizontal swipe is a pan, and stays native.
      if (Math.abs(event.deltaX) > Math.abs(event.deltaY)) return;
      const rect = element.getBoundingClientRect();
      const fraction = clampFraction((event.clientX - rect.left) / Math.max(1, rect.width));
      const anchor = valueAtFraction(window_, fraction);
      const next = viewportAfterWheel(
        viewport,
        event.deltaY,
        anchor,
        fraction,
        model,
        model.minimumWindow,
      );
      // A step with nowhere to go leaves the wheel to the page.
      if (sameZoom(next, viewport, model)) return;
      event.preventDefault();
      setAnimate(false);
      setViewport(next);
    };
    element.addEventListener('wheel', onWheel, { passive: false });
    return () => {
      element.removeEventListener('wheel', onWheel);
    };
  }, [model, viewport, window_]);

  // A new projection is a new domain: a window from the old one would point at
  // nothing a reader chose.
  useEffect(() => {
    setViewport(null);
    setDraft(null);
    setHover(null);
  }, [mode]);

  // A selection made anywhere — the picture, a hierarchy link, the chat's "View
  // execution" — has to be on screen, so the window follows it. Only a new
  // selection moves the window: an event arriving over SSE rebuilds the steps
  // and would otherwise drag a reader who has looked somewhere else back.
  const followedKey = useRef<string | null>(null);
  useEffect(() => {
    if (selectedSpan === null) {
      followedKey.current = null;
      return;
    }
    if (followedKey.current === selectedSpan.key) return;
    followedKey.current = selectedSpan.key;
    setAnimate(true);
    setViewport((current) => viewportWithSpan(current, selectedSpan, model, model.minimumWindow));
  }, [model, selectedSpan]);

  const commitRange = (next: DomainRange | null): void => {
    setAnimate(false);
    onRangeChange(next);
  };

  const fitAll = (): void => {
    setAnimate(false);
    setViewport(null);
    commitRange(null);
  };

  const fractionAt = (event: PointerEvent<HTMLDivElement>): number => {
    const rect = event.currentTarget.getBoundingClientRect();
    return clampFraction((event.clientX - rect.left) / Math.max(1, rect.width));
  };

  const stopEdgePan = (): void => {
    edgeRef.current = null;
    if (edgeFrameRef.current !== null) {
      cancelAnimationFrame(edgeFrameRef.current);
      edgeFrameRef.current = null;
    }
  };

  /**
   * Walk the window while the pointer is held past an edge.
   *
   * A pointermove only arrives when the pointer moves, so a reader who drags to
   * the edge and stops would otherwise get one step and then nothing, and could
   * never select past what is on screen. The loop runs on frames until the
   * pointer leaves the edge zone or the gesture ends.
   *
   * Every frame reads the projection through a ref rather than the render that
   * started the gesture: a step appended while the pointer is held extends the
   * domain, and a gesture that had already reached the old end has to be able to
   * carry on into it.
   */
  const runEdgePan = (): void => {
    if (edgeFrameRef.current !== null) return;
    const step = (): void => {
      edgeFrameRef.current = null;
      const edge = edgeRef.current;
      if (edge === null || edge.direction === 0) return;
      const current = viewportRef.current;
      if (current === null) return;
      const live = modelRef.current;
      const next = shiftedWindow(
        current,
        -edge.direction * EDGE_PAN_FRAME_FRACTION * edge.strength,
        live,
        live.minimumWindow,
      );
      if (next.start !== current.start) {
        viewportRef.current = next;
        setAnimate(false);
        setViewport(next);
      }
      const drag = dragRef.current;
      if (drag !== null) {
        setDraft(orderedRange(drag.anchorValue, valueAtFraction(next, edge.fraction)));
      }
      // Held at the end of the domain, the loop keeps its frame: the projection
      // can grow under the same gesture.
      edgeFrameRef.current = requestAnimationFrame(step);
    };
    edgeFrameRef.current = requestAnimationFrame(step);
  };

  useEffect(() => stopEdgePan, []);

  const onPointerDown = (event: PointerEvent<HTMLDivElement>): void => {
    if (event.button === 2) {
      panRef.current = {
        pointerId: event.pointerId,
        anchorClientX: event.clientX,
        anchorStart: window_.start,
        span: windowSpan,
        moved: false,
      };
      setAnimate(false);
      setPanning(true);
      capturePointer(event.currentTarget, event.pointerId);
      return;
    }
    if (event.button !== 0) return;
    const fraction = fractionAt(event);
    const anchor = valueAtFraction(window_, fraction);
    const key = spanKeyAt(event.target);
    dragRef.current = {
      pointerId: event.pointerId,
      anchorValue: anchor,
      anchorClientX: event.clientX,
      key,
    };
    setHover({ fraction, key });
    setDraft({ start: anchor, end: anchor });
    capturePointer(event.currentTarget, event.pointerId);
  };

  const onPointerMove = (event: PointerEvent<HTMLDivElement>): void => {
    const fraction = fractionAt(event);
    setHover({ fraction, key: spanKeyAt(event.target) });
    const rect = event.currentTarget.getBoundingClientRect();
    const pan = panRef.current;
    if (pan !== null && pan.pointerId === event.pointerId) {
      if (Math.abs(event.clientX - pan.anchorClientX) >= MINIMUM_DRAG_PX) pan.moved = true;
      if (viewport === null) return;
      // Measured from the window at pointer-down: shifting the window the last
      // move produced would apply the same displacement again on every move.
      setViewport(
        shiftedWindow(
          { start: pan.anchorStart, end: pan.anchorStart + pan.span },
          (event.clientX - pan.anchorClientX) / Math.max(1, rect.width),
          model,
          model.minimumWindow,
        ),
      );
      return;
    }
    const drag = dragRef.current;
    if (drag === null || drag.pointerId !== event.pointerId) return;
    const localX = event.clientX - rect.left;
    const edge = Math.min(MAXIMUM_EDGE_PAN_PX, Math.max(1, rect.width * EDGE_PAN_ZONE_FRACTION));
    const direction: -1 | 0 | 1 =
      viewport === null ? 0 : localX < edge ? -1 : localX > rect.width - edge ? 1 : 0;
    if (direction === 0) {
      stopEdgePan();
    } else {
      const distance = direction < 0 ? edge - localX : localX - (rect.width - edge);
      edgeRef.current = {
        direction,
        strength: Math.max(0.2, clampFraction(distance / edge)),
        fraction,
      };
      runEdgePan();
    }
    setDraft(orderedRange(drag.anchorValue, valueAtFraction(window_, fraction)));
  };

  const onPointerEnd = (event: PointerEvent<HTMLDivElement>): void => {
    stopEdgePan();
    const pan = panRef.current;
    if (pan !== null && pan.pointerId === event.pointerId) {
      panRef.current = null;
      setPanning(false);
      // A right click that did not pan clears the selection, the same gesture
      // the desktop harness uses.
      if (!pan.moved && Math.abs(event.clientX - pan.anchorClientX) < MINIMUM_DRAG_PX) {
        commitRange(null);
      }
      return;
    }
    const drag = dragRef.current;
    if (drag === null || drag.pointerId !== event.pointerId) return;
    dragRef.current = null;
    const fraction = fractionAt(event);
    // The window may have been walked by the edge loop, so read it rather than
    // the render this handler was created in.
    const endWindow = activeWindow(viewportRef.current, model, model.minimumWindow);
    const point = valueAtFraction(endWindow, fraction);
    const selected = orderedRange(drag.anchorValue, point);
    setDraft(null);
    const click = Math.abs(event.clientX - drag.anchorClientX) < MINIMUM_DRAG_PX;
    if (click && drag.key !== null) {
      // A mark is selected, not a range: the reader asked for that step.
      commitRange(null);
      onSelect(drag.key);
      return;
    }
    commitRange(committedRange(drag.anchorValue, point, model, model.minimumWindow));
    if (click) {
      const nearest = nearestSpan(model, selected.start);
      if (nearest !== null) onSelect(nearest.key);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key !== 'Escape') return;
    // Escape cancels the gesture in flight, not only what it has already
    // produced. Without this, a draft interval and the frame loop walking the
    // window would carry on after the reader asked for it to stop, and the
    // release would commit the selection they just cancelled.
    stopEdgePan();
    dragRef.current = null;
    panRef.current = null;
    setPanning(false);
    setDraft(null);
    if (range !== null) {
      event.preventDefault();
      commitRange(null);
      return;
    }
    if (viewport !== null) {
      event.preventDefault();
      setAnimate(false);
      setViewport(null);
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <div className="flex items-center gap-1" role="group" aria-label="Overview projection">
          <button
            type="button"
            aria-pressed={mode === 'steps'}
            title="Project the steps by the order they happened in"
            onClick={() => onModeChange('steps')}
            className={CONTROL}
          >
            Steps
          </button>
          <button
            type="button"
            aria-pressed={mode === 'time'}
            disabled={!timed}
            title={
              timed
                ? 'Project the steps by the time the platform recorded'
                : 'This run recorded no timing to project'
            }
            onClick={() => onModeChange('time')}
            className={CONTROL}
          >
            Time
          </button>
        </div>
      </div>
      <div className="flex gap-4">
        <div className="w-24 shrink-0 space-y-1.5">
          {lanes.map((lane) => (
            <div key={lane.key} className="flex h-5 items-center gap-2">
              <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${lane.tone}`} />
              <span className="text-[11px] font-medium text-gray-600 dark:text-gray-300">
                {lane.label}
              </span>
              <span className="ml-auto text-[10px] tabular-nums text-gray-400 dark:text-gray-500">
                {lane.count}
              </span>
            </div>
          ))}
        </div>
        <div
          ref={trackRef}
          role="group"
          aria-label="Execution overview; scroll to zoom, drag to select"
          tabIndex={0}
          data-panning={panning || undefined}
          onKeyDown={onKeyDown}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerEnd}
          onPointerCancel={onPointerEnd}
          onPointerLeave={() => {
            if (dragRef.current === null && panRef.current === null) setHover(null);
          }}
          onDoubleClick={fitAll}
          onContextMenu={(event) => {
            event.preventDefault();
          }}
          className={`relative min-w-0 flex-1 touch-none select-none overflow-hidden focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-700 focus-visible:ring-offset-2 dark:focus-visible:ring-gray-200 dark:focus-visible:ring-offset-gray-900 ${
            panning ? 'cursor-grabbing' : 'cursor-crosshair'
          }`}
          style={{ height: plotHeight }}
        >
          {lanes.map((lane, index) => (
            <div
              key={lane.key}
              aria-hidden="true"
              className="absolute inset-x-0 rounded-md bg-gray-50 dark:bg-white/[0.03]"
              style={{ top: index * LANE_PITCH_PX, height: LANE_HEIGHT_PX }}
            />
          ))}
          <div
            data-timeline-domain="true"
            data-animate-viewport={animate || undefined}
            className={`absolute inset-y-0 ${
              animate
                ? 'motion-safe:transition-[left,width] motion-safe:duration-200 motion-safe:ease-out'
                : ''
            }`}
            style={domainStyle}
          >
            {model.turns.map((turn) => (
              <span
                key={turn.turnNo}
                aria-hidden="true"
                data-turn-boundary={turn.turnNo}
                className="absolute inset-y-0 w-px bg-gray-200 dark:bg-white/10"
                style={{ left: `${((turn.at - model.start) / fullSpan) * 100}%` }}
              />
            ))}
            {model.spans
              .filter(
                (span) =>
                  span.key === selectedKey ||
                  (span.end >= window_.start && span.start <= window_.end),
              )
              .map((span) => {
                const open = selectedKey === span.key;
                const inRange =
                  activeRange === null ||
                  (span.start <= activeRange.end && span.end >= activeRange.start);
                const matches = searchMatchKeys === null || searchMatchKeys.has(span.key);
                const muted = !inRange || !matches;
                // Every mark is as wide as the interval it stands for: one
                // operation in `steps`, its recorded interval in `time`. A lane
                // therefore reads as the band of work it is, adjacent marks meet
                // at a single transparent pixel, and zooming widens the marks
                // with the projection instead of parting them.
                const share = (span.end - span.start) / fullSpan;
                const width = share > 0 ? `${share * 100}%` : `${POINT_MARK_WIDTH_PX}px`;
                // A point mark at the end of the window would be drawn from the
                // clip edge rightward and be invisible. It is anchored by its
                // right edge instead, without making its recorded duration up.
                const point = span.end <= span.start;
                const atWindowEnd = point && span.start >= window_.end;
                const lane = laneIndex.get(span.lane) ?? 0;
                return (
                  <button
                    key={span.key}
                    type="button"
                    data-span-key={span.key}
                    data-equal-duration={mode === 'steps' ? 'true' : undefined}
                    data-recorded-width={
                      mode !== 'steps' && span.end > span.start ? 'true' : undefined
                    }
                    data-edge-anchored={atWindowEnd ? 'true' : undefined}
                    aria-label={`${span.label} (${span.kind}, step ${span.stepNo})`}
                    aria-pressed={open}
                    title={`${span.label} (${span.kind})${span.durationMs !== null ? ` · ${formatDuration(span.durationMs)}` : ''}`}
                    onClick={() => onSelect(span.key)}
                    className={`absolute rounded-[3px] border-r border-transparent bg-clip-padding transition-opacity hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-700 focus-visible:ring-offset-2 dark:focus-visible:ring-gray-200 dark:focus-visible:ring-offset-gray-900 ${
                      span.isError
                        ? 'bg-rose-400'
                        : LANES.find((entry) => entry.key === span.lane)?.tone
                    } ${
                      open
                        ? 'z-10 opacity-100 ring-1 ring-gray-700 ring-offset-1 dark:ring-gray-200 dark:ring-offset-gray-900'
                        : muted
                          ? 'opacity-20'
                          : selectedKey === null
                            ? 'opacity-80'
                            : 'opacity-50'
                    }`}
                    style={{
                      left: `${((span.start - model.start) / fullSpan) * 100}%`,
                      width,
                      minWidth: POINT_MARK_WIDTH_PX,
                      top: lane * LANE_PITCH_PX + (LANE_HEIGHT_PX - MARK_HEIGHT_PX) / 2,
                      height: MARK_HEIGHT_PX,
                      transform: atWindowEnd ? 'translateX(-100%)' : undefined,
                    }}
                  />
                );
              })}
          </div>
          {painted !== null ? (
            <div
              aria-hidden="true"
              data-timeline-selection="true"
              data-dragging={draft === null ? undefined : 'true'}
              className="pointer-events-none absolute inset-y-0 z-20 border-x border-gray-400/70 bg-gray-900/[0.06] dark:border-gray-300/50 dark:bg-white/[0.06]"
              style={{
                left: `${painted.start * 100}%`,
                width: `${Math.max(0.5, (painted.end - painted.start) * 100)}%`,
              }}
            />
          ) : null}
          {hover !== null && draft === null && hover.key === null ? (
            <div
              aria-hidden="true"
              data-timeline-hover-line="true"
              className="pointer-events-none absolute inset-y-0 z-20 w-px bg-gray-400 dark:bg-gray-500"
              style={{ left: `calc(${hover.fraction * 100}% - 1px)` }}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}

export function TrajectoryView({
  events,
  steps: allSteps,
  query,
  onQueryChange,
  selectedKey,
  onSelectStep,
  onOpenRun,
  focusJobId,
  jobs = EMPTY_TIMING_JOBS,
  subagents = EMPTY_SUBAGENT_OPTIONS,
  subagentFilter = '',
  onSubagentFilterChange,
}: {
  events: (AgentJobEventApi | AgentThreadEventApi)[];
  steps: TrajectoryStep[];
  query: string;
  onQueryChange: (value: string) => void;
  selectedKey: string | null;
  onSelectStep: (key: string) => void;
  onOpenRun: (jobId: string, turnNo: number) => void;
  /** A turn opened from Chat: bring its header into view without selecting it. */
  focusJobId?: string | null;
  jobs?: AgentJobApi[];
  /** The conversation's subagents; `steps` are already narrowed to the chosen one. */
  subagents?: readonly { value: string; label: string }[];
  /** The chosen subagent's option value, or '' for every agent. */
  subagentFilter?: string;
  onSubagentFilterChange?: (value: string) => void;
}) {
  const [mode, setMode] = useState<TimelineMode>('steps');
  const [range, setRange] = useState<DomainRange | null>(null);
  const steps = useMemo(() => allSteps.filter((step) => !step.diagnostic), [allSteps]);
  const visible = useMemo(() => filterSteps(steps, query), [steps, query]);
  const model = useMemo(() => deriveTimeline(steps, mode), [steps, mode]);
  const timed = useMemo(() => hasRecordedTiming(steps), [steps]);
  const searchMatchKeys = useMemo(
    () => (query.trim() === '' ? null : new Set(visible.map((step) => step.key))),
    [query, visible],
  );
  const focused = useMemo(() => (range === null ? null : focusKeys(model, range)), [model, range]);

  useEffect(() => {
    if (!focusJobId) return;
    const node = document.querySelector<HTMLElement>(`[data-turn-job="${focusJobId}"]`);
    node?.scrollIntoView?.({ block: 'nearest' });
  }, [focusJobId]);

  // A selection can come from the overview, from a hierarchy link, or from the
  // chat's "View execution"; in every case the row must end up on screen once.
  // The filter is a dependency only so a row the search was hiding is reached
  // when it comes back: a step arriving over SSE must not drag a reader who has
  // scrolled somewhere else back to the selection.
  const scrolledKey = useRef<string | null>(null);
  useEffect(() => {
    if (!selectedKey) {
      scrolledKey.current = null;
      return;
    }
    if (scrolledKey.current === selectedKey) return;
    const node = document.querySelector<HTMLElement>(`[data-step-key="${escapeKey(selectedKey)}"]`);
    // Not in the list right now: a search is hiding it. Leave the record unset
    // so the row is still brought into view if the filter stops hiding it.
    if (!node) return;
    node.scrollIntoView?.({ block: 'nearest' });
    scrolledKey.current = selectedKey;
  }, [selectedKey, visible]);

  // An interval selected on the picture is a reading position in the list: the
  // first step inside it comes into view, and the rows outside it step back.
  // Only a newly committed interval moves the list: an event arriving over SSE
  // rebuilds the focus set and would otherwise pull a reader who has scrolled
  // elsewhere back to an interval they already read.
  const scrolledRange = useRef<DomainRange | null>(null);
  useEffect(() => {
    if (range === null || focused === null || focused.size === 0) {
      scrolledRange.current = null;
      return;
    }
    if (scrolledRange.current === range) return;
    const first = steps.find((step) => focused.has(step.key));
    if (first === undefined) return;
    const node = document.querySelector<HTMLElement>(`[data-step-key="${escapeKey(first.key)}"]`);
    // Hidden by the search right now: leave the interval unhandled so its first
    // row is still reached when the filter stops hiding it. `visible` is a
    // dependency for exactly that: the query is what brings the row back, and
    // the steps it filters are memoized by the caller.
    if (node === null) return;
    node.scrollIntoView?.({ block: 'nearest' });
    scrolledRange.current = range;
  }, [focused, range, steps, visible]);

  function download() {
    const blob = new Blob([toSessionLog(events)], { type: 'application/x-ndjson' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'session-log.ndjson';
    link.click();
    URL.revokeObjectURL(url);
  }

  if (allSteps.length === 0 && !subagentFilter) {
    return (
      <p className="py-8 text-center text-[13px] text-gray-500 dark:text-gray-400">
        This conversation stored no events. A thread that ran before event capture, or one whose
        first turn never started, has nothing to show here.
      </p>
    );
  }

  return (
    <section aria-label="Trajectory" className="space-y-3">
      <div
        className="sticky top-0 z-10 space-y-2 bg-white pb-2 dark:bg-gray-900"
        data-testid="trajectory-overview-toolbar"
      >
        <div className="rounded-xl border border-gray-200 bg-white shadow-[0_1px_2px_rgba(17,24,39,0.025)] dark:border-white/10 dark:bg-gray-900">
          <div className="flex items-center justify-between gap-3 border-b border-gray-100 px-4 py-3 dark:border-white/5">
            <h2 className="flex items-center gap-2 text-[12px] font-medium text-gray-700 dark:text-gray-200">
              <svg
                aria-hidden="true"
                viewBox="0 0 16 16"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.25"
                strokeLinecap="round"
                className="h-3.5 w-3.5 text-gray-400 dark:text-gray-500"
              >
                <path d="M2.5 2.5v11h11M5.5 5h3M9.5 8h3M5.5 11h2" />
              </svg>
              Execution overview
            </h2>
            <span className="rounded-md bg-gray-50 px-2 py-0.5 text-[10px] tabular-nums text-gray-500 dark:bg-white/5 dark:text-gray-400">
              {steps.length} {steps.length === 1 ? 'step' : 'steps'}
            </span>
          </div>
          <div className="px-4 pb-3 pt-3.5">
            <ExecutionOverview
              model={model}
              mode={mode}
              onModeChange={(next) => {
                if (next === mode) return;
                setMode(next);
                // The two projections count different things — operations and
                // milliseconds — so an interval chosen in one means nothing in
                // the other and must not survive the switch.
                setRange(null);
              }}
              timed={timed}
              range={range}
              onRangeChange={setRange}
              selectedKey={selectedKey}
              searchMatchKeys={searchMatchKeys}
              onSelect={onSelectStep}
            />
          </div>
          <TrajectoryTimeBreakdown jobs={jobs} />
        </div>

        <div className="flex flex-wrap items-center gap-2 sm:flex-nowrap">
          <input
            type="search"
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            placeholder="Search steps"
            aria-label="Search steps"
            className="min-w-0 basis-full rounded-lg border border-gray-200 bg-white px-2.5 py-1.5 text-[13px] text-gray-900 outline-none focus:border-gray-300 dark:border-white/10 dark:bg-gray-950 dark:text-gray-100 sm:flex-1 sm:basis-auto"
          />
          {subagents.length > 0 && onSubagentFilterChange ? (
            <select
              value={subagentFilter}
              onChange={(event) => onSubagentFilterChange(event.target.value)}
              aria-label="Show steps from"
              className="min-w-0 max-w-full shrink rounded-lg border border-gray-200 bg-white px-2 py-1.5 text-[12px] text-gray-700 outline-none focus:border-gray-300 dark:border-white/10 dark:bg-gray-950 dark:text-gray-200 sm:max-w-64"
            >
              <option value="">All agents</option>
              {subagents.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          ) : null}
          {focused !== null ? (
            <button
              type="button"
              onClick={() => setRange(null)}
              className="shrink-0 rounded-lg border border-gray-200 px-2.5 py-1.5 text-[12px] text-gray-600 transition-colors hover:border-gray-300 hover:text-gray-900 dark:border-white/10 dark:text-gray-400 dark:hover:border-white/20 dark:hover:text-gray-100"
            >
              {focused.size} of {steps.length} · Clear
            </button>
          ) : visible.length !== steps.length ? (
            <span className="shrink-0 text-[12px] tabular-nums text-gray-500 dark:text-gray-400">
              {visible.length} of {steps.length}
            </span>
          ) : null}
          <button
            type="button"
            onClick={download}
            title="Download session log"
            className="shrink-0 rounded-lg border border-gray-200 dark:border-white/10 px-2.5 py-1.5 text-[12px] text-gray-600 dark:text-gray-400 transition-colors hover:border-gray-300 dark:hover:border-white/20 hover:text-gray-900 dark:hover:text-gray-100"
          >
            Session log
          </button>
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-white/10">
        {visible.map((step, index) => {
          const open = selectedKey === step.key;
          const startsTurn = index === 0 || visible[index - 1].turnNo !== step.turnNo;
          const labelTone = STATUS_TONE[step.status] ?? 'text-gray-800 dark:text-gray-200';
          const outside = focused !== null && !focused.has(step.key);
          return (
            <div
              key={step.key}
              data-timeline-focus={focused === null ? undefined : outside ? 'outside' : 'inside'}
              className={`border-b border-gray-100 last:border-b-0 dark:border-white/5 ${
                outside ? 'opacity-25' : ''
              }`}
            >
              {startsTurn ? (
                <div
                  data-turn-job={step.jobId}
                  className="flex scroll-mt-80 items-center gap-2 bg-gray-50 px-3 py-1 dark:bg-gray-950"
                >
                  <span className="text-[11px] font-medium uppercase tracking-wide text-gray-400 dark:text-gray-500">
                    Turn {step.turnNo}
                  </span>
                  {step.jobId ? (
                    <button
                      type="button"
                      onClick={() => onOpenRun(step.jobId, step.turnNo)}
                      className="ml-auto rounded px-1.5 py-0.5 text-[11px] text-gray-500 transition-colors hover:bg-white hover:text-gray-900 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100"
                    >
                      Run details ›
                    </button>
                  ) : null}
                </div>
              ) : null}
              <button
                type="button"
                data-step-key={step.key}
                data-parent-key={step.parentKey}
                data-delegation-parent-key={step.delegationParentKey}
                onClick={() => onSelectStep(step.key)}
                aria-current={open ? 'true' : undefined}
                className={`relative flex w-full scroll-mt-80 items-baseline gap-2 py-1.5 pl-5 pr-3 text-left text-[12px] transition-colors hover:bg-gray-50 dark:hover:bg-white/5 ${
                  open ? 'bg-gray-50 dark:bg-gray-950' : ''
                }`}
              >
                {step.kind === 'assistant' ? (
                  <span
                    aria-hidden="true"
                    className="absolute left-1 top-4 h-1 w-1 rounded-full bg-gray-400 dark:bg-gray-500"
                  />
                ) : null}
                <span className="flex w-[76px] shrink-0 justify-end" data-role-slot>
                  <span
                    className={`inline-flex h-[19px] items-center rounded px-1.5 text-[10px] font-semibold uppercase tracking-wide ${step.actor === 'assistant' ? 'w-full justify-center' : ''} ${ACTOR_TONE[step.actor]}`}
                  >
                    {step.actor}
                  </span>
                </span>
                {step.delegationParentKey ? (
                  <span
                    className="shrink-0 text-[10px] text-violet-700 dark:text-violet-300"
                    title="Activity recorded from a delegated agent"
                  >
                    ↳ Subagent
                  </span>
                ) : null}
                {step.kind === 'tool' || step.kind === 'event' ? (
                  <span
                    title={step.toolName ?? step.label}
                    className={`max-w-48 shrink-0 truncate font-medium ${labelTone}`}
                  >
                    {step.label}
                  </span>
                ) : null}
                <span
                  className={`min-w-0 flex-1 truncate ${step.kind === 'tool' || step.kind === 'event' ? 'font-mono text-gray-500 dark:text-gray-400' : 'text-gray-700 dark:text-gray-300'}`}
                >
                  {step.detail ||
                    (step.kind === 'assistant'
                      ? step.toolKeys?.length
                        ? ''
                        : step.streaming
                          ? 'Generating…'
                          : '(no text recorded)'
                      : step.kind === 'user'
                        ? '(no text recorded)'
                        : '')}
                </span>
                {step.kind === 'tool' && (step.result || step.outputPreview) ? (
                  <>
                    <span aria-hidden="true" className="text-gray-400 dark:text-gray-500">
                      →
                    </span>
                    <span className="min-w-0 flex-1 truncate font-mono text-gray-500 dark:text-gray-400">
                      {preview(step.result?.text ?? step.outputPreview) || '(no output)'}
                    </span>
                  </>
                ) : null}
                {step.status === 'failed' ? (
                  <span className="shrink-0 text-[11px] text-rose-600 dark:text-rose-400">
                    Failed
                  </span>
                ) : step.status === 'waiting' ? (
                  <span className="shrink-0 text-[11px] text-amber-600 dark:text-amber-400">
                    {step.kind === 'assistant' ? 'Generating' : 'Waiting'}
                  </span>
                ) : null}
                {step.timing.durationMs !== null ? (
                  <span className="shrink-0 tabular-nums text-gray-400 dark:text-gray-500">
                    {formatDuration(step.timing.durationMs)}
                  </span>
                ) : null}
              </button>
            </div>
          );
        })}
        {visible.length === 0 ? (
          <p className="px-3 py-6 text-center text-[12px] text-gray-500 dark:text-gray-400">
            {steps.length === 0 && subagentFilter
              ? 'No steps were recorded for this subagent.'
              : steps.length === 0
                ? 'No execution steps recorded yet. Runtime records for a turn are in Run details.'
                : `No step matches “${query}”.`}
          </p>
        ) : null}
      </div>
    </section>
  );
}

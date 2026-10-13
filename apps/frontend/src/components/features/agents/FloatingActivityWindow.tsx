'use client';

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type HTMLAttributes,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
} from 'react';

type Box = { left: number; top: number; width: number; height: number };
type Bounds = { width: number; height: number; inset: number };
type Handle = 'move' | 'n' | 'e' | 's' | 'w' | 'ne' | 'se' | 'sw' | 'nw';
type Gesture = { handle: Handle; pointerId: number; x: number; y: number };

const MIN_WIDTH = 280;
const MIN_HEIGHT = 160;
const handles: { handle: Exclude<Handle, 'move'>; label: string; className: string }[] = [
  { handle: 'n', label: 'top', className: 'inset-x-3 -top-1 h-2 cursor-ns-resize' },
  { handle: 'e', label: 'right', className: 'inset-y-3 -right-1 w-2 cursor-ew-resize' },
  { handle: 's', label: 'bottom', className: 'inset-x-3 -bottom-1 h-2 cursor-ns-resize' },
  { handle: 'w', label: 'left', className: 'inset-y-3 -left-1 w-2 cursor-ew-resize' },
  { handle: 'ne', label: 'top right', className: '-right-1 -top-1 h-4 w-4 cursor-nesw-resize' },
  {
    handle: 'se',
    label: 'bottom right',
    className: '-bottom-1 -right-1 h-4 w-4 cursor-nwse-resize',
  },
  { handle: 'sw', label: 'bottom left', className: '-bottom-1 -left-1 h-4 w-4 cursor-nesw-resize' },
  { handle: 'nw', label: 'top left', className: '-left-1 -top-1 h-4 w-4 cursor-nwse-resize' },
];

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

function fit(box: Box, bounds: Bounds): Box {
  const { width: outerWidth, height: outerHeight, inset } = bounds;
  const width = clamp(
    box.width,
    Math.min(MIN_WIDTH, outerWidth - inset * 2),
    outerWidth - inset * 2,
  );
  const height = clamp(
    box.height,
    Math.min(MIN_HEIGHT, outerHeight - inset * 2),
    outerHeight - inset * 2,
  );
  return {
    width,
    height,
    left: clamp(box.left, inset, outerWidth - inset - width),
    top: clamp(box.top, inset, outerHeight - inset - height),
  };
}

function adjust(box: Box, handle: Handle, dx: number, dy: number, bounds: Bounds): Box {
  if (handle === 'move') return fit({ ...box, left: box.left + dx, top: box.top + dy }, bounds);
  const { inset } = bounds;
  const minWidth = Math.min(MIN_WIDTH, bounds.width - inset * 2);
  const minHeight = Math.min(MIN_HEIGHT, bounds.height - inset * 2);
  let { left, top } = box;
  let right = left + box.width;
  let bottom = top + box.height;
  if (handle.includes('w')) left = clamp(left + dx, inset, right - minWidth);
  if (handle.includes('e')) right = clamp(right + dx, left + minWidth, bounds.width - inset);
  if (handle.includes('n')) top = clamp(top + dy, inset, bottom - minHeight);
  if (handle.includes('s')) bottom = clamp(bottom + dy, top + minHeight, bounds.height - inset);
  return { left, top, width: right - left, height: bottom - top };
}

/** Geometry survives hiding the panel; the conversation and composer bound its gestures. */
export function FloatingActivityWindow({
  visible,
  bottom,
  children,
}: {
  visible: boolean;
  bottom: number;
  children: (moveHandleProps: HTMLAttributes<HTMLDivElement>) => ReactNode;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<Box | null>(null);
  const gestureRef = useRef<Gesture | null>(null);
  const [box, setBox] = useState<Box | null>(null);
  const [dragging, setDragging] = useState(false);

  const bounds = useCallback((): Bounds => {
    const width = containerRef.current?.clientWidth || window.innerWidth;
    const height = containerRef.current?.clientHeight || Math.max(1, window.innerHeight - bottom);
    return { width, height, inset: Math.min(16, width / 4, height / 4) };
  }, [bottom]);
  const boundsRef = useRef(bounds);
  boundsRef.current = bounds;

  const apply = useCallback((next: Box) => {
    boxRef.current = next;
    setBox(next);
  }, []);

  const reset = useCallback(() => {
    const available = bounds();
    apply(fit({ left: available.width - 400, top: 16, width: 384, height: 480 }, available));
  }, [apply, bounds]);

  useLayoutEffect(() => {
    if (!visible) return;
    const settle = () => {
      if (boxRef.current) apply(fit(boxRef.current, bounds()));
      else reset();
    };
    settle();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(settle);
    if (containerRef.current) observer?.observe(containerRef.current);
    window.addEventListener('resize', settle);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', settle);
    };
  }, [apply, bounds, reset, visible]);

  useEffect(() => {
    if (!visible || !dragging) {
      gestureRef.current = null;
      if (dragging) setDragging(false);
      return;
    }
    const { body } = document;
    const previousSelect = body.style.userSelect;
    const previousCursor = body.style.cursor;
    const handle = gestureRef.current!.handle;
    body.style.userSelect = 'none';
    body.style.cursor =
      handle === 'move'
        ? 'grabbing'
        : handle === 'n' || handle === 's'
          ? 'ns-resize'
          : handle === 'e' || handle === 'w'
            ? 'ew-resize'
            : handle === 'ne' || handle === 'sw'
              ? 'nesw-resize'
              : 'nwse-resize';
    const move = (event: globalThis.PointerEvent) => {
      const gesture = gestureRef.current;
      if (!gesture || event.pointerId !== gesture.pointerId || !boxRef.current) return;
      apply(
        adjust(
          boxRef.current,
          gesture.handle,
          event.clientX - gesture.x,
          event.clientY - gesture.y,
          boundsRef.current(),
        ),
      );
      gesture.x = event.clientX;
      gesture.y = event.clientY;
    };
    const end = (event?: globalThis.PointerEvent) => {
      if (event && event.pointerId !== gestureRef.current?.pointerId) return;
      gestureRef.current = null;
      setDragging(false);
    };
    const blur = () => end();
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
    window.addEventListener('blur', blur);
    return () => {
      gestureRef.current = null;
      body.style.userSelect = previousSelect;
      body.style.cursor = previousCursor;
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', end);
      window.removeEventListener('pointercancel', end);
      window.removeEventListener('blur', blur);
    };
  }, [apply, dragging, visible]);

  const start = (event: PointerEvent<HTMLElement>, handle: Handle) => {
    if (event.button !== 0 || gestureRef.current) return;
    event.preventDefault();
    event.currentTarget.focus();
    gestureRef.current = { handle, pointerId: event.pointerId, x: event.clientX, y: event.clientY };
    setDragging(true);
  };

  const keyDown = (event: KeyboardEvent<HTMLElement>, handle: Handle) => {
    const step = event.shiftKey ? 64 : 16;
    const dx = event.key === 'ArrowLeft' ? -step : event.key === 'ArrowRight' ? step : 0;
    const dy = event.key === 'ArrowUp' ? -step : event.key === 'ArrowDown' ? step : 0;
    if ((!dx && !dy) || !boxRef.current) return;
    event.preventDefault();
    apply(adjust(boxRef.current, handle, dx, dy, bounds()));
  };

  if (!visible) return null;

  return (
    <div
      ref={containerRef}
      className="pointer-events-none absolute inset-x-0 top-0 z-10"
      style={{ bottom }}
    >
      <aside
        id="job-activity-window"
        role="region"
        aria-label="Tasks and subagents"
        className="pointer-events-auto absolute flex flex-col rounded-xl bg-white/95 shadow-xl ring-1 ring-gray-200/90 backdrop-blur-sm dark:bg-gray-900/95 dark:ring-white/10"
        style={
          box ?? {
            right: 16,
            top: 16,
            width: 'min(384px, calc(100% - 32px))',
            height: 'min(480px, calc(100% - 32px))',
          }
        }
      >
        {children({
          role: 'button',
          tabIndex: 0,
          'aria-label': 'Move tasks and subagents',
          title: 'Drag to move · Arrow keys to move · Double-click to reset',
          className:
            'min-w-0 flex-1 cursor-grab touch-none select-none rounded outline-none focus-visible:bg-gray-100/60 active:cursor-grabbing dark:focus-visible:bg-white/5',
          onPointerDown: (event) => start(event, 'move'),
          onKeyDown: (event) => keyDown(event, 'move'),
          onDoubleClick: reset,
        })}
        {handles.map(({ handle, label, className }) => (
          <button
            key={handle}
            type="button"
            aria-label={`Resize tasks and subagents from ${label}`}
            title="Drag to resize · Arrow keys to resize"
            className={`absolute z-10 touch-none rounded outline-none focus-visible:bg-gray-200/50 dark:focus-visible:bg-white/10 ${className}`}
            onPointerDown={(event) => start(event, handle)}
            onKeyDown={(event) => keyDown(event, handle)}
          />
        ))}
        <svg
          aria-hidden="true"
          className="pointer-events-none absolute bottom-1 right-1 h-3 w-3 text-gray-400 dark:text-gray-500"
          viewBox="0 0 12 12"
          fill="none"
          stroke="currentColor"
        >
          <path d="M4 10 10 4M7 10l3-3" />
        </svg>
      </aside>
    </div>
  );
}

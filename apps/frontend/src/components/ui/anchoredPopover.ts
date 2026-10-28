'use client';

import { useLayoutEffect, useState, type CSSProperties, type RefObject } from 'react';

function sameStyle(a: CSSProperties, b: CSSProperties): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]) as Set<keyof CSSProperties>;
  for (const key of keys) if (a[key] !== b[key]) return false;
  return true;
}

/** Before it is measured the popup is fixed, out of flow, and invisible. */
const UNPLACED: CSSProperties = { position: 'fixed', top: 0, left: 0, visibility: 'hidden' };

export interface AnchoredPopoverOptions {
  /** Space between the trigger and the popup. */
  gap?: number;
  /** Distance kept from the viewport edges. */
  margin?: number;
  /** Tallest the popup may be; it scrolls inside beyond this. */
  maxHeight?: number;
  /** Below this much room underneath, open above if there is more room there. */
  preferredHeight?: number;
  /** Fixed popup width; by default at least the trigger's, up to `maxWidth`. */
  width?: number;
  maxWidth?: number;
}

/** Write a placement to the live element before React's next commit. */
function paint(
  element: HTMLElement,
  next: CSSProperties,
  geometry: { minWidth: number; maxW: number; height: number; width?: number },
) {
  const { minWidth, maxW, height, width } = geometry;
  element.style.position = 'fixed';
  element.style.left = `${next.left as number}px`;
  element.style.top = `${next.top as number}px`;
  element.style.minWidth = `${minWidth}px`;
  element.style.maxWidth = `${maxW}px`;
  element.style.maxHeight = `${height}px`;
  if (width !== undefined) element.style.width = `${minWidth}px`;
  element.style.visibility = 'visible';
}

/**
 * Places a `position: fixed` popup against its trigger: below it, or above
 * when there is more room there, never wider or taller than the viewport, and
 * moved back inside it when the trigger is near an edge. Follows the trigger
 * on scroll (of any ancestor) and resize while `open`.
 *
 * Fixed positioning lets the popup escape a table's overflow or a dialog's
 * clipping without a global z-index scheme or a scroll lock.
 */
export function useAnchoredPopover(
  open: boolean,
  triggerRef: RefObject<HTMLElement | null>,
  popoverRef: RefObject<HTMLElement | null>,
  {
    gap = 4,
    margin = 8,
    maxHeight = 320,
    preferredHeight = 240,
    width,
    maxWidth = 384,
  }: AnchoredPopoverOptions = {},
): CSSProperties {
  const [style, setStyle] = useState<CSSProperties>(UNPLACED);

  useLayoutEffect(() => {
    if (!open) {
      setStyle(UNPLACED);
      return;
    }

    /**
     * Find the popup's containing-block origin from what is actually in the
     * DOM, never from a position a React update has not committed yet:
     * `rect = origin + style coordinate` for a fixed box, so subtracting its
     * current `left`/`top` gives the origin exactly. A scroll event can then
     * re-place without mistaking its own uncommitted delta for an ancestor
     * transform (which caused a full-delta overshoot on every wheel step).
     */
    const origin = () => {
      const element = popoverRef.current;
      if (!element) return { x: 0, y: 0 };
      const landed = element.getBoundingClientRect();
      return {
        x: landed.left - (Number.parseFloat(element.style.left) || 0),
        y: landed.top - (Number.parseFloat(element.style.top) || 0),
      };
    };

    const place = () => {
      const trigger = triggerRef.current;
      const element = popoverRef.current;
      if (!trigger || !element) return;
      const rect = trigger.getBoundingClientRect();
      const viewport = window.visualViewport;
      const left0 = viewport?.offsetLeft ?? 0;
      const top0 = viewport?.offsetTop ?? 0;
      const viewportWidth = viewport?.width ?? window.innerWidth;
      const viewportHeight = viewport?.height ?? window.innerHeight;
      const base = origin();

      const widest = Math.max(0, viewportWidth - margin * 2);
      const minWidth = Math.min(width ?? Math.max(rect.width, 176), widest);
      const maxW = Math.min(width ?? Math.max(maxWidth, minWidth), widest);
      // The popup's own width once its content is laid out, to keep it on
      // screen; before the first layout, assume the widest it may be.
      const measuredLength = element.offsetWidth || maxW;
      const actual = Math.min(Math.max(measuredLength, minWidth), maxW);
      const intendedLeft = Math.max(
        left0 + margin,
        Math.min(rect.left, left0 + viewportWidth - actual - margin),
      );

      const below = Math.max(0, top0 + viewportHeight - rect.bottom - gap - margin);
      const above = Math.max(0, rect.top - top0 - gap - margin);
      const placeBelow = below >= preferredHeight || below >= above;
      const height = Math.min(maxHeight, placeBelow ? below : above);
      // Measure the natural height against the available one. Always position
      // by `top`: one coordinate makes the containing-block origin exact,
      // where a `bottom` would mix in a stale height.
      const natural = element.offsetHeight || height;
      const intendedTop = placeBelow
        ? rect.bottom + gap
        : rect.top - gap - Math.min(natural, height);
      // The first placement is made while the panel is still hidden, so the
      // corrected position and visibility commit together before any paint.
      const next: CSSProperties = {
        position: 'fixed',
        left: intendedLeft - base.x,
        top: intendedTop - base.y,
        minWidth,
        maxWidth: maxW,
        ...(width !== undefined ? { width: minWidth } : {}),
        maxHeight: height,
        visibility: 'visible',
      };
      // Paint this frame. A scroll event fires before the frame's paint, but a
      // React state update commits in a later task; writing the same values to
      // the live element closes that one-frame follow lag without a loop. The
      // state below keeps React's model identical, so later renders are no-ops.
      paint(element, next, { minWidth, maxW, height, width });
      setStyle((current) => (sameStyle(current, next) ? current : next));
    };
    // The page or a container scrolling moves the trigger; the popup's own
    // list scrolling does not, and re-placing then would only churn.
    const onScroll = (event: Event) => {
      if (event.target instanceof Node && popoverRef.current?.contains(event.target)) return;
      place();
    };
    place();
    // Async options, a late search field or a wrapping label change the
    // popup's size after it opens; follow it so the height stays bounded.
    const observer =
      typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => place());
    if (popoverRef.current) observer?.observe(popoverRef.current);
    if (triggerRef.current) observer?.observe(triggerRef.current);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', onScroll, true);
    window.visualViewport?.addEventListener('resize', place);
    window.visualViewport?.addEventListener('scroll', place);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', place);
      window.removeEventListener('scroll', onScroll, true);
      window.visualViewport?.removeEventListener('resize', place);
      window.visualViewport?.removeEventListener('scroll', place);
    };
  }, [open, triggerRef, popoverRef, gap, margin, maxHeight, preferredHeight, width, maxWidth]);

  return style;
}

/**
 * Where a popup opened from `trigger` should be rendered: inside the nearest
 * modal dialog (so the dialog's focus and click handling still contain it),
 * else the body. Cloud Agent themes through the `.dark` class, so a portalled
 * menu keeps the page's tokens without a separate wrapper.
 */
export function popoverContainer(trigger: HTMLElement | null): HTMLElement | null {
  if (typeof document === 'undefined') return null;
  return trigger?.closest<HTMLElement>('[aria-modal="true"]') ?? document.body;
}

/** Scroll only the list to show an option, so a menu never moves the page. */
export function keepInView(list: HTMLElement | null, option: HTMLElement | null) {
  const viewport = list?.getBoundingClientRect();
  if (!list || !option || !viewport) return;
  const bounds = option.getBoundingClientRect();
  if (bounds.top < viewport.top) list.scrollTop -= viewport.top - bounds.top;
  else if (bounds.bottom > viewport.bottom) list.scrollTop += bounds.bottom - viewport.bottom;
}

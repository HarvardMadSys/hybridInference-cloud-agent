'use client';

import { useLayoutEffect, type RefObject } from 'react';

const EDGE = 8;
const GAP = 4;
const MAX_HEIGHT = 320;
/** Long names may widen the menu past a narrow trigger, up to this. */
const MAX_WIDTH = 448;

function viewportBox() {
  const viewport = window.visualViewport;
  const left = (viewport?.offsetLeft ?? 0) + EDGE;
  const top = (viewport?.offsetTop ?? 0) + EDGE;
  return {
    left,
    top,
    right: left + (viewport?.width ?? window.innerWidth) - 2 * EDGE,
    bottom: top + (viewport?.height ?? window.innerHeight) - 2 * EDGE,
  };
}

function place(trigger: HTMLElement, panel: HTMLElement) {
  const view = viewportBox();
  const bounds = trigger.getBoundingClientRect();
  const room = Math.max(0, view.right - view.left);
  const style = panel.style;
  style.minWidth = `${Math.min(bounds.width, room)}px`;
  style.maxWidth = `${Math.min(Math.max(bounds.width, MAX_WIDTH), room)}px`;
  style.maxHeight = `${MAX_HEIGHT}px`;
  const natural = panel.offsetHeight;
  const below = view.bottom - bounds.bottom - GAP;
  const above = bounds.top - view.top - GAP;
  const placeBelow = below >= natural || below >= above;
  const height = Math.max(0, Math.min(MAX_HEIGHT, placeBelow ? below : above));
  style.maxHeight = `${height}px`;
  const top = placeBelow ? bounds.bottom + GAP : bounds.top - GAP - Math.min(natural, height);
  const left = Math.max(view.left, Math.min(bounds.left, view.right - panel.offsetWidth));
  style.top = `${top}px`;
  style.left = `${left}px`;
  // A transformed or filtered ancestor moves the origin of `position: fixed`.
  // Measure where the menu landed and correct by the difference.
  const landed = panel.getBoundingClientRect();
  if (landed.top !== top) style.top = `${2 * top - landed.top}px`;
  if (landed.left !== left) style.left = `${2 * left - landed.left}px`;
}

/**
 * Keep an open select menu beside its trigger and inside the visual viewport,
 * opening upward when there is more room above, and following scrolls of any
 * container the trigger sits in.
 */
export function useSelectPlacement(
  open: boolean,
  trigger: RefObject<HTMLElement>,
  panel: RefObject<HTMLElement>,
  revision: unknown,
) {
  useLayoutEffect(() => {
    if (!open) return;
    function update() {
      if (trigger.current && panel.current) place(trigger.current, panel.current);
    }
    function onScroll(event: Event) {
      if (!panel.current?.contains(event.target as Node)) update();
    }
    update();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
    if (trigger.current) observer?.observe(trigger.current);
    window.addEventListener('resize', update);
    window.addEventListener('scroll', onScroll, true);
    window.visualViewport?.addEventListener('resize', update);
    window.visualViewport?.addEventListener('scroll', update);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', update);
      window.removeEventListener('scroll', onScroll, true);
      window.visualViewport?.removeEventListener('resize', update);
      window.visualViewport?.removeEventListener('scroll', update);
    };
  }, [open, trigger, panel, revision]);
}

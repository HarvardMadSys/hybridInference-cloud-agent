'use client';

import { useLayoutEffect, useState, type CSSProperties, type RefObject } from 'react';

/** Portal past the toolbar's scroller, while staying inside the visual viewport. */
export function usePickerLayout(
  open: boolean,
  trigger: RefObject<HTMLButtonElement>,
  panel: RefObject<HTMLDivElement>,
  searchable: boolean,
  count: number,
) {
  const [style, setStyle] = useState<CSSProperties>();
  useLayoutEffect(() => {
    if (!open) return;
    function update() {
      if (!trigger.current) return;
      const viewport = window.visualViewport;
      const left = (viewport?.offsetLeft ?? 0) + 8;
      const top = (viewport?.offsetTop ?? 0) + 8;
      const right = left + (viewport?.width ?? window.innerWidth) - 16;
      const bottom = top + (viewport?.height ?? window.innerHeight) - 16;
      const bounds = trigger.current.getBoundingClientRect();
      const width = Math.min(searchable ? 320 : 240, right - left);
      const above = bounds.top - top - 8;
      const below = bottom - bounds.bottom - 8;
      const desired = Math.min(384, 44 + (searchable ? 46 : 0) + count * 36);
      const placeBelow = above < desired && below > above;
      setStyle({
        width,
        left: Math.max(left, Math.min(bounds.left, right - width)),
        maxHeight: Math.max(0, Math.min(384, placeBelow ? below : above)),
        ...(placeBelow
          ? { top: Math.max(top, bounds.bottom + 8) }
          : { bottom: window.innerHeight - Math.min(bottom, bounds.top - 8) }),
      });
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
  }, [open, trigger, panel, searchable, count]);
  return style;
}

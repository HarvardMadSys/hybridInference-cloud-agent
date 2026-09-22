'use client';

import { useLayoutEffect, useState, type CSSProperties, type RefObject } from 'react';

/** Keep the anchored panel inside the visible conversation, including on resize. */
export function useComposerMenuLayout(open: boolean, panel: RefObject<HTMLDivElement>) {
  const [style, setStyle] = useState<CSSProperties>();
  useLayoutEffect(() => {
    if (!open) return;
    const anchor = panel.current?.offsetParent;
    if (!(anchor instanceof HTMLElement)) return;
    function update() {
      const viewport = window.visualViewport;
      let top = (viewport?.offsetTop ?? 0) + 8;
      let bottom = (viewport?.offsetTop ?? 0) + (viewport?.height ?? window.innerHeight) - 8;
      for (let parent = anchor!.parentElement; parent; parent = parent.parentElement) {
        if (
          parent.tagName === 'MAIN' ||
          /(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)
        ) {
          const bounds = parent.getBoundingClientRect();
          top = Math.max(top, bounds.top + 8);
          bottom = Math.min(bottom, bounds.bottom - 8);
        }
      }
      const bounds = anchor!.getBoundingClientRect();
      const above = bounds.top - top - 8;
      const below = bottom - bounds.bottom - 8;
      const placeBelow = above < 120 && below > above;
      const maxHeight = Math.max(0, Math.floor(placeBelow ? below : above));
      setStyle({
        maxHeight,
        ...(placeBelow ? { top: 'calc(100% + 8px)', bottom: 'auto' } : {}),
      });
    }
    update();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
    observer?.observe(anchor);
    window.addEventListener('resize', update);
    window.addEventListener('scroll', update, true);
    window.visualViewport?.addEventListener('resize', update);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', update);
      window.removeEventListener('scroll', update, true);
      window.visualViewport?.removeEventListener('resize', update);
    };
  }, [open, panel]);
  return style;
}

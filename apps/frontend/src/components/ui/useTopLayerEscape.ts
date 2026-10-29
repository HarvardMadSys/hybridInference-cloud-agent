'use client';

import { useEffect, useRef, type RefObject } from 'react';

/**
 * The open popups, innermost last. Escape belongs to the top one only: an
 * account menu's submenu closes before the account menu, and neither lets the
 * page's own Escape shortcut fire.
 */
const stack: symbol[] = [];

/**
 * Let an open popup own Escape before anything behind it.
 *
 * A pointer press leaves focus on the trigger in Chromium even when a layout
 * effect moves it into the list, so a key handler on the panel alone can miss
 * Escape entirely. Listening during the capture phase makes the innermost
 * popup close and restore its trigger's focus, and stops the page's own
 * Escape shortcut (Stop, close panel, close dialog) from also firing.
 */
export function useTopLayerEscape(
  open: boolean,
  /** Returns false when a nested layer consumed the key and stayed open. */
  close: () => boolean | void,
  triggerRef: RefObject<HTMLElement | null>,
) {
  const closeRef = useRef(close);
  closeRef.current = close;
  useEffect(() => {
    if (!open) return;
    const token = Symbol('popup');
    stack.push(token);
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape' || event.isComposing || event.keyCode === 229) return;
      if (stack.at(-1) !== token) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (closeRef.current() === false) return;
      triggerRef.current?.focus();
    }
    window.addEventListener('keydown', onKeyDown, true);
    return () => {
      const index = stack.lastIndexOf(token);
      if (index >= 0) stack.splice(index, 1);
      window.removeEventListener('keydown', onKeyDown, true);
    };
  }, [open, triggerRef]);
}

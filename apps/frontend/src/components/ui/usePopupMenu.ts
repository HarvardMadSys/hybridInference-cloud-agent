'use client';

import { useEffect, useLayoutEffect, useRef, type KeyboardEvent, type RefObject } from 'react';
import { useAnchoredPopover, type AnchoredPopoverOptions } from './anchoredPopover';
import { useTopLayerEscape } from './useTopLayerEscape';

const ITEM =
  '[role="menuitem"]:not(:disabled), [role="menuitemradio"]:not(:disabled), [role="menuitemcheckbox"]:not(:disabled)';

function items(menu: HTMLElement | null): HTMLElement[] {
  return menu ? [...menu.querySelectorAll<HTMLElement>(ITEM)] : [];
}

/**
 * The keyboard and dismissal contract every popup menu shares:
 *
 * - opens on a menu button's ArrowDown/ArrowUp, focusing the first/last row;
 * - arrows, Home and End move through enabled rows and wrap;
 * - Escape closes only this menu (captured before page-level Escape handlers)
 *   and restores focus to its trigger;
 * - Tab closes it and resumes the page's tab order from the trigger;
 * - a pointer or focus outside closes it without taking focus back.
 *
 * Placement is the same anchored popover every selection popup uses, so a
 * menu never leaves the viewport and follows its trigger while open.
 */
export function usePopupMenu({
  open,
  onOpenChange,
  triggerRef,
  menuRef,
  placement,
  initialFocus = 'first',
  onEscape,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  triggerRef: RefObject<HTMLElement | null>;
  menuRef: RefObject<HTMLElement | null>;
  placement?: AnchoredPopoverOptions;
  initialFocus?: 'first' | 'last' | 'selected';
  /** Runs before closing, for a surface with more than one layer. */
  onEscape?: () => boolean;
}) {
  const style = useAnchoredPopover(open, triggerRef, menuRef, placement);
  const placed = style.visibility !== 'hidden';
  const listeners = useRef({ onOpenChange, onEscape, triggerRef });
  listeners.current = { onOpenChange, onEscape, triggerRef };
  const opened = useRef(false);
  const initial = useRef<'first' | 'last' | 'selected'>(initialFocus);

  useLayoutEffect(() => {
    if (!open) {
      opened.current = false;
      initial.current = initialFocus;
      return;
    }
    // The panel starts invisible until placement runs. A browser cannot focus
    // a hidden row, so wait for the placed style; placement itself can change
    // with the content's measured height.
    if (!placed || opened.current) return;
    opened.current = true;
    const rows = items(menuRef.current);
    const checked = menuRef.current?.querySelector<HTMLElement>(
      '[role="menuitemradio"][aria-checked="true"], [role="menuitemcheckbox"][aria-checked="true"]',
    );
    const target =
      initial.current === 'last'
        ? rows.at(-1)
        : initial.current === 'selected'
          ? (checked ?? rows[0])
          : rows[0];
    target?.focus();
  }, [open, placed, initialFocus, menuRef]);

  useEffect(() => {
    if (!open) return;
    function close(restoreFocus: boolean) {
      listeners.current.onOpenChange(false);
      if (restoreFocus) listeners.current.triggerRef.current?.focus();
    }
    function outside(event: Event) {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (menuRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      close(false);
    }
    document.addEventListener('pointerdown', outside);
    document.addEventListener('focusin', outside);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('focusin', outside);
    };
  }, [open, menuRef, triggerRef]);

  // Escape belongs to the top menu, and never reaches the page's own shortcut.
  useTopLayerEscape(
    open,
    () => {
      if (listeners.current.onEscape?.()) return false;
      listeners.current.onOpenChange(false);
    },
    triggerRef,
  );

  function onKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    if (event.key === 'Escape') {
      // Handled by the captured window listener; stop it before React's root.
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (event.key === 'Tab') {
      listeners.current.onOpenChange(false);
      listeners.current.triggerRef.current?.focus();
      return;
    }
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const rows = items(menuRef.current);
    if (!rows.length) return;
    const current = rows.indexOf(document.activeElement as HTMLElement);
    const next =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? rows.length - 1
          : (current + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
    rows[next]?.focus();
  }

  /**
   * The trigger half of the contract: a menu button opens with ArrowDown on
   * its first row and ArrowUp on its last. It is a stable handler, and it
   * leaves every other key to the trigger's own behavior.
   */
  function triggerKeyDown(event: KeyboardEvent<HTMLElement>) {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    event.preventDefault();
    if (open) {
      initial.current = event.key === 'ArrowUp' ? 'last' : 'first';
      opened.current = false;
      const rows = items(menuRef.current);
      (event.key === 'ArrowUp' ? rows.at(-1) : rows[0])?.focus();
      return;
    }
    initial.current = event.key === 'ArrowUp' ? 'last' : 'first';
    listeners.current.onOpenChange(true);
  }

  return { style, onKeyDown, triggerKeyDown };
}

'use client';

import { useEffect, useLayoutEffect, useRef, type RefObject } from 'react';

const FOCUSABLE = 'button, input:not([type="hidden"]), select, textarea, a[href], [tabindex]';
const dialogs: HTMLElement[] = [];
const isolated = new Map<HTMLElement, { count: number; previous: string | null }>();
let previousOverflow = '';

function visible(element: HTMLElement): boolean {
  if (element.matches(':disabled') || element.closest('[hidden], [inert], [aria-hidden="true"]'))
    return false;
  for (let ancestor: HTMLElement | null = element; ancestor; ancestor = ancestor.parentElement) {
    const style = getComputedStyle(ancestor);
    if (
      style.display === 'none' ||
      style.visibility === 'hidden' ||
      style.visibility === 'collapse'
    )
      return false;
  }
  return true;
}

function available(element: HTMLElement): boolean {
  return element.tabIndex >= 0 && visible(element);
}

function controls(panel: HTMLElement): HTMLElement[] {
  const candidates = [...panel.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(available);
  return candidates.filter((element) => {
    if (!(element instanceof HTMLInputElement) || element.type !== 'radio' || !element.name)
      return true;
    const group = candidates.filter(
      (candidate): candidate is HTMLInputElement =>
        candidate instanceof HTMLInputElement &&
        candidate.type === 'radio' &&
        candidate.name === element.name &&
        candidate.form === element.form,
    );
    return element === (group.find((candidate) => candidate.checked) ?? group[0]);
  });
}

/** Shared focus, keyboard ownership, and background isolation for modal surfaces. */
export function useModalDialog(
  panel: RefObject<HTMLElement>,
  {
    open = true,
    busy = false,
    onClose,
    initialFocus,
    returnFocus,
  }: {
    open?: boolean;
    busy?: boolean;
    onClose: () => void;
    initialFocus?: string;
    /** Use a known opener when pointer activation did not move browser focus. */
    returnFocus?: RefObject<HTMLElement>;
  },
): void {
  const options = useRef({ busy, onClose, initialFocus, returnFocus });
  options.current = { busy, onClose, initialFocus, returnFocus };

  useEffect(() => {
    const node = panel.current;
    if (!open || !node) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousTabIndex = node.getAttribute('tabindex');
    if (previousTabIndex === null) node.tabIndex = -1;
    if (!dialogs.length) previousOverflow = document.body.style.overflow;
    dialogs.push(node);
    document.body.style.overflow = 'hidden';
    const background: HTMLElement[] = [];
    // Keep the overlay's backdrop interactive, while excluding surrounding UI
    // at every ancestor level. This also works for non-portaled dialogs.
    for (let branch = node.parentElement; branch?.parentElement; branch = branch.parentElement) {
      for (const sibling of branch.parentElement.children) {
        if (sibling === branch || !(sibling instanceof HTMLElement)) continue;
        const held = isolated.get(sibling);
        if (held) held.count += 1;
        else isolated.set(sibling, { count: 1, previous: sibling.getAttribute('inert') });
        sibling.setAttribute('inert', '');
        background.push(sibling);
      }
      if (branch.parentElement === document.body) break;
    }
    const isTop = () => dialogs.at(-1) === node;
    const focus = () => {
      const requested = options.current.initialFocus
        ? node.querySelector<HTMLElement>(options.current.initialFocus)
        : null;
      (requested && available(requested) ? requested : (controls(node)[0] ?? node)).focus();
    };
    focus();
    const contain = (event: FocusEvent) => {
      if (isTop() && !node.contains(event.target as Node)) focus();
    };
    const keyboard = (event: KeyboardEvent) => {
      if (!isTop() || (event.key !== 'Escape' && event.key !== 'Tab')) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (event.key === 'Escape') {
        if (!options.current.busy) options.current.onClose();
        return;
      }
      const items = controls(node);
      if (!items.length) {
        node.focus();
        return;
      }
      const at = items.indexOf(document.activeElement as HTMLElement);
      const next =
        at < 0
          ? event.shiftKey
            ? items.length - 1
            : 0
          : (at + (event.shiftKey ? -1 : 1) + items.length) % items.length;
      items[next].focus();
    };
    document.addEventListener('focusin', contain);
    window.addEventListener('keydown', keyboard, true);
    return () => {
      document.removeEventListener('focusin', contain);
      window.removeEventListener('keydown', keyboard, true);
      const index = dialogs.indexOf(node);
      if (index >= 0) dialogs.splice(index, 1);
      for (const sibling of background) {
        const held = isolated.get(sibling);
        if (!held || --held.count > 0) continue;
        if (held.previous === null) sibling.removeAttribute('inert');
        else sibling.setAttribute('inert', held.previous);
        isolated.delete(sibling);
      }
      if (!dialogs.length) document.body.style.overflow = previousOverflow;
      if (previousTabIndex === null) node.removeAttribute('tabindex');
      const target = options.current.returnFocus?.current ?? previous;
      const remaining = dialogs.at(-1);
      // Release isolation before restoring focus. A remaining nested surface
      // still owns focus if its parent is removed first.
      if (target?.isConnected && visible(target) && (!remaining || remaining.contains(target)))
        target.focus();
      else if (remaining) (controls(remaining)[0] ?? remaining).focus();
    };
  }, [open, panel]);

  useLayoutEffect(() => {
    const node = panel.current;
    if (!open || !node || dialogs.at(-1) !== node) return;
    const active = document.activeElement;
    if (!node.contains(active) || (active instanceof HTMLElement && !visible(active)))
      (controls(node)[0] ?? node).focus();
  });
}

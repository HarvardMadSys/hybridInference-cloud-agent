// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { createRef, useRef, useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  MENU_ACTIONS,
  MENU_POSITIONED,
  MENU_ROW,
  MENU_ROW_DESTRUCTIVE,
  MENU_ROW_SELECTED,
  MENU_SURFACE,
  menuOptionClass,
  menuRowClass,
} from './menuStyles';
import { usePopupMenu } from './usePopupMenu';

afterEach(cleanup);

function Menu({
  onEscape,
  onOpenChange = vi.fn(),
}: {
  onEscape?: () => boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const popup = usePopupMenu({
    open,
    onOpenChange: (next) => {
      onOpenChange(next);
      setOpen(next);
    },
    triggerRef,
    menuRef,
    onEscape,
  });
  return (
    <div>
      <button
        ref={triggerRef}
        type="button"
        onClick={() => setOpen(true)}
        onKeyDown={popup.triggerKeyDown}
      >
        Open
      </button>
      {open ? (
        <div
          ref={menuRef}
          role="menu"
          aria-label="Actions"
          style={popup.style}
          className={`${MENU_SURFACE} ${MENU_POSITIONED}`}
          onKeyDown={popup.onKeyDown}
        >
          <button role="menuitem" type="button" tabIndex={-1}>
            First
          </button>
          <button role="menuitem" type="button" tabIndex={-1} disabled>
            Middle
          </button>
          <button role="menuitem" type="button" tabIndex={-1} className={MENU_ROW_DESTRUCTIVE}>
            Last
          </button>
        </div>
      ) : null}
    </div>
  );
}

function openMenu() {
  fireEvent.click(screen.getByRole('button', { name: 'Open' }));
  return screen.getByRole('menu', { name: 'Actions' });
}

describe('usePopupMenu', () => {
  it('focuses the first enabled row, skips disabled rows, and wraps on arrows', () => {
    render(<Menu />);
    const menu = openMenu();
    expect(screen.getByRole('menuitem', { name: 'First' })).toHaveFocus();

    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(screen.getByRole('menuitem', { name: 'Last' })).toHaveFocus();
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(screen.getByRole('menuitem', { name: 'First' })).toHaveFocus();
    fireEvent.keyDown(menu, { key: 'End' });
    expect(screen.getByRole('menuitem', { name: 'Last' })).toHaveFocus();
    fireEvent.keyDown(menu, { key: 'Home' });
    expect(screen.getByRole('menuitem', { name: 'First' })).toHaveFocus();
  });

  it('owns Escape, closes only this menu, and restores the trigger', () => {
    const page = vi.fn();
    window.addEventListener('keydown', page);
    render(<Menu />);
    const menu = openMenu();
    fireEvent.keyDown(menu, { key: 'Escape' });
    window.removeEventListener('keydown', page);
    expect(page).not.toHaveBeenCalled();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Open' })).toHaveFocus();
  });

  it('lets a layered menu consume Escape before closing its outer layer', () => {
    const onEscape = vi.fn(() => true);
    render(<Menu onEscape={onEscape} />);
    fireEvent.keyDown(openMenu(), { key: 'Escape' });
    expect(onEscape).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('menu', { name: 'Actions' })).toBeInTheDocument();
  });

  it('closes on Tab and on an outside pointer without restoring focus', () => {
    render(<Menu />);
    const menu = openMenu();
    fireEvent.keyDown(menu, { key: 'Tab' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();

    openMenu();
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('opens from the trigger with ArrowDown on the first row and ArrowUp on the last', () => {
    render(<Menu />);
    const trigger = screen.getByRole('button', { name: 'Open' });
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    expect(screen.getByRole('menuitem', { name: 'First' })).toHaveFocus();

    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    fireEvent.keyDown(trigger, { key: 'ArrowUp' });
    expect(screen.getByRole('menuitem', { name: 'Last' })).toHaveFocus();
  });

  it('leaves other keys on the trigger untouched', () => {
    render(<Menu />);
    const trigger = screen.getByRole('button', { name: 'Open' });
    expect(fireEvent.keyDown(trigger, { key: 'Enter' })).toBe(true);
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });

  it('keeps its keyboard handlers stable for an empty menu', () => {
    const triggerRef = createRef<HTMLButtonElement>();
    const menuRef = createRef<HTMLDivElement>();
    const { result } = renderHook(() =>
      usePopupMenu({
        open: false,
        onOpenChange: vi.fn(),
        triggerRef,
        menuRef,
      }),
    );
    expect(result.current.style.visibility).toBe('hidden');
  });
});

describe('shared menu rows', () => {
  it('gives plain rows hover and focus feedback on the semantic surface', () => {
    expect(MENU_ROW).not.toContain('gray');
    expect(MENU_ROW).not.toContain('red-');
    // Anchors and role=menuitem rows are not form controls, so `:enabled`
    // would leave them unhighlighted.
    expect(MENU_ROW).not.toContain('enabled:');
    expect(MENU_ROW).toContain('hover:bg-surface-hover');
    expect(MENU_ROW).toContain('focus:bg-surface-hover');
    expect(MENU_ROW).toContain('aria-disabled:hover:bg-transparent');
    expect(MENU_ROW).toContain('aria-disabled:opacity-50');
    expect(MENU_SURFACE).toContain('bg-surface-raised');
    expect(MENU_POSITIONED).toContain('fixed');
    expect(MENU_ACTIONS).toContain('overflow-y-auto');
  });

  it('keeps selected and destructive states from being overridden by the plain hover', () => {
    expect(MENU_ROW_SELECTED).toContain('bg-accent-subtle');
    expect(MENU_ROW_SELECTED).toContain('hover:bg-accent-subtle');
    expect(MENU_ROW_SELECTED).not.toContain('hover:bg-surface-hover');
    expect(MENU_ROW_DESTRUCTIVE).toContain('text-status-error');
    expect(MENU_ROW_DESTRUCTIVE).not.toContain('hover:bg-surface-hover');
    expect(MENU_ROW_DESTRUCTIVE).toContain('hover:bg-status-error/10');
  });

  it('keeps listbox options state-driven without hover or focus tint', () => {
    const active = menuOptionClass(false, true, false);
    const idle = menuOptionClass(false, false, false);
    expect(active).toContain('bg-surface-hover');
    expect(active).not.toContain('hover:bg-surface-hover');
    expect(active).not.toContain('focus:bg-surface-hover');
    expect(idle).not.toContain('hover:bg-surface-hover');
    // Selected options keep their accent, and destructive/plain actions keep theirs.
    expect(menuOptionClass(true, true, false)).toContain('bg-accent-subtle');
    expect(MENU_ROW).toContain('hover:bg-surface-hover');
    expect(MENU_ROW_DESTRUCTIVE).toContain('hover:bg-status-error/10');
  });

  it('marks a disabled row unclickable and dimmed, and leaves enabled rows unaffected', () => {
    const disabled = menuRowClass({ disabled: true });
    expect(disabled).toContain('cursor-not-allowed');
    expect(disabled).toContain('opacity-50');
    expect(menuRowClass({ disabled: true, selected: true })).not.toContain('bg-accent-subtle');
    expect(menuRowClass({ active: true })).toContain('bg-surface-hover');
  });
});

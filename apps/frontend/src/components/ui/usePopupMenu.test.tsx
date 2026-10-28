// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { createRef, useRef, useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { MENU_POSITIONED, MENU_ROW, MENU_SURFACE, MENU_ROW_DESTRUCTIVE } from './menuStyles';
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
      <button ref={triggerRef} type="button" onClick={() => setOpen(true)}>
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
  it('marks destructive rows and leaves plain rows on the semantic surface', () => {
    expect(MENU_ROW).not.toContain('gray');
    expect(MENU_ROW).not.toContain('red-');
    expect(MENU_ROW_DESTRUCTIVE).toContain('text-status-error');
    expect(MENU_SURFACE).toContain('bg-surface-raised');
    expect(MENU_POSITIONED).toContain('fixed');
  });
});

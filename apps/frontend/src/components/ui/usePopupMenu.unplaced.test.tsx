// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen } from '@testing-library/react';
import { act, useRef } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

// Force the real first frame: the panel is rendered before placement has
// measured it, so it is still visibility:hidden.
vi.mock('./anchoredPopover', async (importOriginal) => {
  const original = await importOriginal<typeof import('./anchoredPopover')>();
  return {
    ...original,
    useAnchoredPopover: () => ({ position: 'fixed', top: 0, left: 0, visibility: 'hidden' }),
  };
});

const { usePopupMenu } = await import('./usePopupMenu');

afterEach(cleanup);

function Menu() {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const popup = usePopupMenu({
    open: true,
    onOpenChange: () => {},
    triggerRef,
    menuRef,
  });
  return (
    <>
      <button ref={triggerRef} type="button">
        Open
      </button>
      <div ref={menuRef} role="menu" aria-label="Actions" style={popup.style}>
        <button role="menuitem" type="button" tabIndex={-1}>
          Row
        </button>
      </div>
    </>
  );
}

describe('usePopupMenu before placement', () => {
  it('leaves focus on the trigger rather than failing to focus a hidden row', () => {
    const { container } = render(<Menu />);
    const trigger = screen.getByRole('button', { name: 'Open' });
    act(() => trigger.focus());
    const menu = container.querySelector('[role="menu"]')!;
    expect(menu).toHaveStyle({ visibility: 'hidden' });
    expect(menu.querySelector('[role="menuitem"]')).not.toHaveFocus();
    expect(trigger).toHaveFocus();
  });
});

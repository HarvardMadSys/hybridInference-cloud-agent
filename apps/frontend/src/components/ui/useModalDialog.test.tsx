// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useModalDialog } from './useModalDialog';

function Modal({
  title,
  busy = false,
  onClose,
  children,
}: {
  title: string;
  busy?: boolean;
  onClose: () => void;
  children?: ReactNode;
}) {
  const panel = useRef<HTMLDivElement>(null);
  useModalDialog(panel, { busy, onClose });
  return createPortal(
    <div data-testid={`${title}-overlay`}>
      <div ref={panel} role="dialog" aria-label={title} aria-modal="true">
        <button disabled={busy} onClick={onClose}>
          Close {title}
        </button>
        {children}
      </div>
    </div>,
    document.body,
  );
}

afterEach(() => {
  cleanup();
  document.body.style.overflow = '';
});

describe('shared modal ownership', () => {
  it('keeps focus on the panel when busy disables every control', () => {
    const close = vi.fn();
    const { rerender } = render(<Modal title="Saving" onClose={close} />);
    expect(screen.getByRole('button', { name: 'Close Saving' })).toHaveFocus();

    rerender(<Modal title="Saving" busy onClose={close} />);
    const dialog = screen.getByRole('dialog', { name: 'Saving' });
    expect(dialog).toHaveFocus();
    fireEvent.keyDown(window, { key: 'Tab' });
    expect(dialog).toHaveFocus();
    fireEvent.keyDown(window, { key: 'Tab', shiftKey: true });
    expect(dialog).toHaveFocus();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(close).not.toHaveBeenCalled();

    rerender(<Modal title="Saving" onClose={close} />);
    fireEvent.keyDown(window, { key: 'Tab' });
    expect(screen.getByRole('button', { name: 'Close Saving' })).toHaveFocus();
  });

  it('restores nested isolation, focus, and the original inert and overflow values', () => {
    function Nested() {
      const [parent, setParent] = useState(false);
      const [child, setChild] = useState(false);
      return (
        <>
          <button onClick={() => setParent(true)}>Open parent</button>
          {createPortal(
            <div
              data-testid="already-inert"
              ref={(node) => node?.setAttribute('inert', 'original')}
            >
              Unavailable background
            </div>,
            document.body,
          )}
          {parent && (
            <Modal title="Parent" onClose={() => setParent(false)}>
              <button onClick={() => setChild(true)}>Open child</button>
              {child && <Modal title="Child" onClose={() => setChild(false)} />}
            </Modal>
          )}
        </>
      );
    }
    document.body.style.overflow = 'scroll';
    const { container } = render(<Nested />);
    const opener = screen.getByRole('button', { name: 'Open parent' });
    opener.focus();
    fireEvent.click(opener);
    expect(container).toHaveAttribute('inert');
    expect(document.body.style.overflow).toBe('hidden');

    const parent = screen.getByRole('dialog', { name: 'Parent' });
    const childOpener = within(parent).getByRole('button', { name: 'Open child' });
    childOpener.focus();
    fireEvent.click(childOpener);
    expect(screen.getByTestId('Parent-overlay')).toHaveAttribute('inert');
    expect(screen.getByRole('button', { name: 'Close Child' })).toHaveFocus();
    // Even a programmatic focus attempt cannot leave the top dialog.
    opener.focus();
    expect(screen.getByRole('button', { name: 'Close Child' })).toHaveFocus();

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog', { name: 'Child' })).not.toBeInTheDocument();
    expect(screen.getByTestId('Parent-overlay')).not.toHaveAttribute('inert');
    expect(container).toHaveAttribute('inert');
    expect(childOpener).toHaveFocus();
    expect(document.body.style.overflow).toBe('hidden');

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(container).not.toHaveAttribute('inert');
    expect(screen.getByTestId('already-inert')).toHaveAttribute('inert', 'original');
    expect(opener).toHaveFocus();
    expect(document.body.style.overflow).toBe('scroll');
  });

  it('skips CSS-hidden descendants and follows native radio tab groups', () => {
    render(
      <Modal title="Choices" onClose={vi.fn()}>
        <div style={{ display: 'none' }}>
          <button>Hidden action</button>
        </div>
        <input type="radio" name="choice" aria-label="First" />
        <input type="radio" name="choice" aria-label="Selected" defaultChecked />
        <button>Last action</button>
      </Modal>,
    );
    fireEvent.keyDown(window, { key: 'Tab' });
    expect(screen.getByRole('radio', { name: 'Selected' })).toHaveFocus();
    fireEvent.keyDown(window, { key: 'Tab' });
    expect(screen.getByRole('button', { name: 'Last action' })).toHaveFocus();
    fireEvent.keyDown(window, { key: 'Tab' });
    expect(screen.getByRole('button', { name: 'Close Choices' })).toHaveFocus();
  });
});

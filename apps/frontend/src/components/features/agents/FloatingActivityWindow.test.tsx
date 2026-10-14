// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ActivityWindow } from './ActivityWindow';
import { FloatingActivityWindow } from './FloatingActivityWindow';

function Harness({ visible = true, bottom = 100 }: { visible?: boolean; bottom?: number }) {
  return (
    <FloatingActivityWindow visible={visible} bottom={bottom}>
      {(moveHandleProps, maximize) => (
        <ActivityWindow
          moveHandleProps={moveHandleProps}
          maximize={maximize}
          overview={{
            taskActivity: {
              kind: 'tasks',
              title: 'Task progress',
              tasks: [{ subject: 'Review the panel', blockedBy: [], blocks: [] }],
            },
            subagents: [],
          }}
          turnNo={1}
          attemptNo={1}
          onClose={vi.fn()}
          onOpenSubagent={vi.fn()}
        />
      )}
    </FloatingActivityWindow>
  );
}

function panel() {
  return screen.getByRole('region', { name: 'Tasks and subagents' });
}

function box() {
  const { left, top, width, height } = panel().style;
  return {
    left: parseFloat(left),
    top: parseFloat(top),
    width: parseFloat(width),
    height: parseFloat(height),
  };
}

function moveHandle() {
  return screen.getByRole('button', { name: 'Move tasks and subagents' });
}

function resizeHandle(edge: string) {
  return screen.getByRole('button', { name: `Resize tasks and subagents from ${edge}` });
}

function drag(handle: HTMLElement, dx: number, dy: number) {
  fireEvent.pointerDown(handle, { button: 0, pointerId: 1, clientX: 400, clientY: 200 });
  fireEvent.pointerMove(window, { pointerId: 1, clientX: 400 + dx, clientY: 200 + dy });
  fireEvent.pointerUp(window, { pointerId: 1 });
}

describe('FloatingActivityWindow', () => {
  beforeEach(() => {
    window.innerWidth = 1000;
    window.innerHeight = 800;
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it('moves from the title while keeping the panel inside the available area', () => {
    render(<Harness />);
    expect(box()).toEqual({ left: 600, top: 16, width: 384, height: 480 });
    drag(moveHandle(), -200, 80);
    expect(box()).toEqual({ left: 400, top: 96, width: 384, height: 480 });
    drag(moveHandle(), -2000, 2000);
    expect(box()).toEqual({ left: 16, top: 204, width: 384, height: 480 });
    drag(moveHandle(), 2000, -2000);
    expect(box()).toEqual({ left: 600, top: 16, width: 384, height: 480 });
    expect(moveHandle()).toHaveFocus();
  });

  it.each([
    ['left', -40, 0, { left: 360, top: 116, width: 424, height: 480 }],
    ['right', 40, 0, { left: 400, top: 116, width: 424, height: 480 }],
    ['top', 0, -40, { left: 400, top: 76, width: 384, height: 520 }],
    ['bottom', 0, 40, { left: 400, top: 116, width: 384, height: 520 }],
    ['top left', -40, -40, { left: 360, top: 76, width: 424, height: 520 }],
    ['top right', 40, -40, { left: 400, top: 76, width: 424, height: 520 }],
    ['bottom left', -40, 40, { left: 360, top: 116, width: 424, height: 520 }],
    ['bottom right', 40, 40, { left: 400, top: 116, width: 424, height: 520 }],
  ] as const)('resizes from %s while anchoring the opposite edges', (edge, dx, dy, expected) => {
    render(<Harness />);
    drag(moveHandle(), -200, 100);
    drag(resizeHandle(edge), dx, dy);
    expect(box()).toEqual(expected);
  });

  it('enforces minimum sizes and fits small screens and changing composer height', () => {
    const view = render(<Harness />);
    drag(resizeHandle('bottom left'), 2000, -2000);
    expect(box()).toEqual({ left: 704, top: 16, width: 280, height: 160 });
    window.innerWidth = 240;
    window.innerHeight = 240;
    fireEvent(window, new Event('resize'));
    expect(box()).toEqual({ left: 16, top: 16, width: 208, height: 108 });
    view.rerender(<Harness bottom={160} />);
    expect(box()).toEqual({ left: 16, top: 16, width: 208, height: 48 });
  });

  it('measures the conversation container and reacts when sibling panes take space', () => {
    let width = 700;
    let height = 600;
    let settle = () => {};
    const disconnect = vi.fn();
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(() => width);
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockImplementation(() => height);
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(callback: () => void) {
          settle = callback;
        }
        observe = vi.fn();
        disconnect = disconnect;
      },
    );
    const view = render(<Harness />);
    expect(box()).toEqual({ left: 300, top: 16, width: 384, height: 480 });
    width = 350;
    height = 400;
    // The same event a ResizeObserver delivers when the workspace opens.
    act(() => settle());
    expect(box()).toEqual({ left: 16, top: 16, width: 318, height: 368 });
    view.unmount();
    expect(disconnect).toHaveBeenCalled();
  });

  it('preserves geometry when hidden and restores defaults with a title double click', () => {
    const view = render(<Harness />);
    drag(moveHandle(), -200, 100);
    drag(resizeHandle('bottom right'), 40, 40);
    const adjusted = box();
    view.rerender(<Harness visible={false} />);
    expect(screen.queryByRole('region', { name: 'Tasks and subagents' })).not.toBeInTheDocument();
    view.rerender(<Harness />);
    expect(box()).toEqual(adjusted);
    fireEvent.doubleClick(moveHandle());
    expect(box()).toEqual({ left: 600, top: 16, width: 384, height: 480 });
  });

  it('supports keyboard moving and resizing and keeps content and close outside the drag handle', () => {
    render(<Harness />);
    fireEvent.keyDown(moveHandle(), { key: 'ArrowLeft', shiftKey: true });
    fireEvent.keyDown(moveHandle(), { key: 'ArrowDown' });
    expect(box()).toEqual({ left: 536, top: 32, width: 384, height: 480 });
    fireEvent.keyDown(resizeHandle('bottom right'), { key: 'ArrowRight' });
    fireEvent.keyDown(resizeHandle('bottom right'), { key: 'ArrowDown', shiftKey: true });
    expect(box()).toEqual({ left: 536, top: 32, width: 400, height: 544 });
    const previous = box();
    drag(within(panel()).getByRole('button', { name: 'Close tasks and subagents' }), -100, 100);
    drag(within(panel()).getByRole('region', { name: 'Tasks and subagents details' }), -100, 100);
    expect(box()).toEqual(previous);
  });

  it('maximizes over the available area and restores the floating geometry', () => {
    const view = render(<Harness />);
    drag(moveHandle(), -200, 100);
    const floating = box();
    fireEvent.click(screen.getByRole('button', { name: 'Maximize tasks and subagents' }));
    expect(panel()).toHaveAttribute('data-maximized', 'true');
    expect(panel().style).toMatchObject({
      left: '16px',
      top: '16px',
      right: '16px',
      bottom: '16px',
    });
    expect(panel().style.width).toBe('');
    expect(screen.queryByRole('button', { name: 'Move tasks and subagents' })).toBeNull();
    expect(screen.queryByRole('button', { name: /^Resize tasks and subagents/ })).toBeNull();

    // Escape restores; hiding keeps the maximized state for the next open.
    const restore = screen.getByRole('button', { name: 'Restore tasks and subagents' });
    fireEvent.keyDown(restore, { key: 'Escape' });
    expect(panel()).not.toHaveAttribute('data-maximized');
    expect(box()).toEqual(floating);
    fireEvent.click(screen.getByRole('button', { name: 'Maximize tasks and subagents' }));
    view.rerender(<Harness visible={false} />);
    view.rerender(<Harness />);
    expect(panel()).toHaveAttribute('data-maximized', 'true');

    fireEvent.doubleClick(screen.getByText('Tasks & subagents'));
    expect(box()).toEqual(floating);
    fireEvent.click(screen.getByRole('button', { name: 'Maximize tasks and subagents' }));
    fireEvent.click(screen.getByRole('button', { name: 'Restore tasks and subagents' }));
    expect(box()).toEqual(floating);
    expect(moveHandle()).toBeInTheDocument();
  });

  it.each(['pointercancel', 'blur', 'hide', 'unmount'])(
    'releases drag styles and listeners on %s',
    (end) => {
      document.body.style.cursor = 'crosshair';
      document.body.style.userSelect = 'text';
      const view = render(<Harness />);
      fireEvent.pointerDown(moveHandle(), { button: 0, pointerId: 7, clientX: 400, clientY: 200 });
      expect(document.body.style.cursor).toBe('grabbing');
      expect(document.body.style.userSelect).toBe('none');
      fireEvent.pointerMove(window, { pointerId: 8, clientX: 300, clientY: 300 });
      expect(box().left).toBe(600);
      // Reflow during a drag must not interrupt the pointer's ownership.
      view.rerender(<Harness bottom={120} />);
      fireEvent.pointerMove(window, { pointerId: 7, clientX: 300, clientY: 300 });
      expect(box().left).toBe(500);
      if (end === 'hide') view.rerender(<Harness visible={false} />);
      else if (end === 'unmount') view.unmount();
      else
        fireEvent(
          window,
          end === 'blur' ? new Event('blur') : new PointerEvent('pointercancel', { pointerId: 7 }),
        );
      expect(document.body.style.cursor).toBe('crosshair');
      expect(document.body.style.userSelect).toBe('text');
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    },
  );
});

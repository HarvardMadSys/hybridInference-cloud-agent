// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ConversationTurnNavigationEntry } from './conversationTurnNavigation';
import { BackToBottom, TurnRail } from './TurnNavigator';

function entry(
  key: string,
  displayNo: number,
  overrides: Partial<ConversationTurnNavigationEntry> = {},
): ConversationTurnNavigationEntry {
  return {
    key,
    jobId: key,
    displayNo,
    promptPreview: `Question ${displayNo}`,
    responsePreview: `Answer ${displayNo}`,
    ...overrides,
  };
}

const ENTRIES = [entry('t1', 1), entry('t2', 2), entry('t3', 3, { state: 'running' })];

afterEach(cleanup);

describe('TurnRail', () => {
  it('stays out of the way when there is only one turn to navigate', () => {
    render(<TurnRail entries={[entry('t1', 1)]} activeKey="t1" onNavigate={vi.fn()} />);
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument();
  });

  it('puts one named mark on the rail per turn', () => {
    render(<TurnRail entries={ENTRIES} activeKey="t2" onNavigate={vi.fn()} />);
    const rail = screen.getByRole('navigation', { name: 'Conversation turns' });
    expect(within(rail).getAllByRole('button')).toHaveLength(3);
    expect(within(rail).getByRole('button', { name: 'Go to turn 2' })).toHaveAttribute(
      'aria-current',
      'true',
    );
    expect(within(rail).getByRole('button', { name: 'Go to turn 1' })).not.toHaveAttribute(
      'aria-current',
    );
  });

  it('reports the turn a mark addresses, and nothing else', () => {
    const onNavigate = vi.fn();
    render(<TurnRail entries={ENTRIES} activeKey="t1" onNavigate={onNavigate} />);
    fireEvent.click(screen.getByRole('button', { name: 'Go to turn 3' }));
    expect(onNavigate).toHaveBeenCalledTimes(1);
    expect(onNavigate).toHaveBeenCalledWith('t3');
  });

  it('associates a focused turn with its preview without moving the transcript', () => {
    const onNavigate = vi.fn();
    render(<TurnRail entries={ENTRIES} activeKey="t1" onNavigate={onNavigate} />);
    const mark = screen.getByRole('button', { name: 'Go to turn 3' });
    fireEvent.focus(mark);
    const preview = screen.getByRole('tooltip');
    expect(mark).toHaveAttribute('aria-describedby', preview.id);
    expect(preview).toHaveTextContent('Question 3');
    expect(preview).toHaveTextContent('Answer 3');
    expect(preview).toHaveTextContent('Running');
    expect(onNavigate).not.toHaveBeenCalled();
  });

  it('walks a single tab stop with the arrow keys and keeps navigation for a commit', () => {
    const onNavigate = vi.fn();
    render(<TurnRail entries={ENTRIES} activeKey="t1" onNavigate={onNavigate} />);
    const marks = screen.getAllByRole('button');
    fireEvent.focus(marks[2]);
    const tabbable = marks.filter((mark) => mark.getAttribute('tabindex') === '0');
    expect(tabbable).toHaveLength(1);
    expect(tabbable[0]).toHaveAccessibleName('Go to turn 3');

    act(() => marks[0].focus());
    fireEvent.keyDown(marks[0], { key: 'ArrowDown' });
    expect(marks[1]).toHaveFocus();
    expect(onNavigate).not.toHaveBeenCalled();
    fireEvent.keyDown(marks[1], { key: 'End' });
    expect(marks[2]).toHaveFocus();
    fireEvent.keyDown(marks[2], { key: 'Home' });
    expect(marks[0]).toHaveFocus();
    fireEvent.keyDown(marks[0], { key: 'ArrowUp' });
    expect(marks[0]).toHaveFocus();
  });

  it('leaves the tab stop alone while the rail itself holds focus', () => {
    // The reader arrowed to a mark; the reading position then moves (they
    // wheeled the transcript, or a new turn arrived). Moving the tab stop would
    // silently unfocus the mark under them.
    const { rerender } = render(<TurnRail entries={ENTRIES} activeKey="t1" onNavigate={vi.fn()} />);
    const marks = screen.getAllByRole('button');
    marks[0].focus();
    fireEvent.keyDown(marks[0], { key: 'ArrowDown' });
    expect(marks[1]).toHaveFocus();

    rerender(<TurnRail entries={ENTRIES} activeKey="t3" onNavigate={vi.fn()} />);
    expect(marks[1]).toHaveFocus();
    expect(marks[1]).toHaveAttribute('tabindex', '0');

    // Arrow keys still move from the mark the reader is on, not from the new
    // reading position.
    fireEvent.keyDown(marks[1], { key: 'ArrowDown' });
    expect(marks[2]).toHaveFocus();
  });

  it('keeps one tab stop when the list shrinks under the focus index', () => {
    const { rerender } = render(<TurnRail entries={ENTRIES} activeKey="t1" onNavigate={vi.fn()} />);
    const marks = screen.getAllByRole('button');
    marks[2].focus();

    rerender(<TurnRail entries={ENTRIES.slice(0, 2)} activeKey="t1" onNavigate={vi.fn()} />);
    const remaining = screen.getAllByRole('button');
    expect(remaining.filter((mark) => mark.getAttribute('tabindex') === '0')).toHaveLength(1);
  });

  it('closes a hovered preview on Escape without letting the key escape', () => {
    // Escape stops the running task everywhere else in this view. A preview is
    // not a reason to stop a run, so the rail claims the key while one is open.
    const onStop = vi.fn();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented) onStop();
    };
    window.addEventListener('keydown', onKeyDown);
    try {
      render(<TurnRail entries={ENTRIES} activeKey="t1" onNavigate={vi.fn()} />);
      fireEvent.pointerOver(screen.getByRole('button', { name: 'Go to turn 2' }));
      expect(screen.getByRole('tooltip')).toBeInTheDocument();

      fireEvent.keyDown(document, { key: 'Escape' });

      expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
      expect(onStop).not.toHaveBeenCalled();
    } finally {
      window.removeEventListener('keydown', onKeyDown);
    }
  });

  it('does not claim Escape once the preview has gone', () => {
    vi.useFakeTimers();
    const onStop = vi.fn();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented) onStop();
    };
    window.addEventListener('keydown', onKeyDown);
    try {
      render(<TurnRail entries={ENTRIES} activeKey="t1" onNavigate={vi.fn()} />);
      const mark = screen.getByRole('button', { name: 'Go to turn 2' });
      fireEvent.pointerOver(mark);
      fireEvent.pointerOut(mark, { relatedTarget: document.body });
      act(() => {
        vi.advanceTimersByTime(500);
      });
      expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();

      fireEvent.keyDown(document, { key: 'Escape' });
      expect(onStop).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener('keydown', onKeyDown);
      vi.useRealTimers();
    }
  });

  it('keeps the preview while the pointer crosses to the card', () => {
    // The card is not a child of the mark, so reaching it is a leave followed
    // by an enter. Closing on the leave alone is what made the card unreadable
    // with a mouse.
    vi.useFakeTimers();
    try {
      render(<TurnRail entries={ENTRIES} activeKey="t1" onNavigate={vi.fn()} />);
      const mark = screen.getByRole('button', { name: 'Go to turn 2' });
      fireEvent.pointerOver(mark);
      fireEvent.pointerOut(mark, { relatedTarget: document.body });

      const card = screen.getByRole('tooltip');
      fireEvent.pointerOver(card);
      act(() => {
        vi.advanceTimersByTime(500);
      });

      expect(screen.getByRole('tooltip')).toHaveTextContent('Question 2');
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not preview a turn that has no preview to give', () => {
    render(
      <TurnRail
        entries={[entry('t1', 1), entry('t2', 2, { promptPreview: '', responsePreview: '' })]}
        activeKey="t1"
        onNavigate={vi.fn()}
      />,
    );
    fireEvent.focus(screen.getByRole('button', { name: 'Go to turn 2' }));
    expect(screen.getByRole('tooltip')).toHaveTextContent('No message text');
  });
});

describe('BackToBottom', () => {
  it('offers the way back, and says when there is something new', () => {
    const onReturn = vi.fn();
    const { rerender } = render(<BackToBottom hasNewContent={false} onReturn={onReturn} />);
    expect(screen.getByRole('button', { name: /Back to bottom/ })).toBeInTheDocument();
    rerender(<BackToBottom hasNewContent onReturn={onReturn} />);
    const button = screen.getByRole('button', { name: /New content/ });
    fireEvent.click(button);
    expect(onReturn).toHaveBeenCalledTimes(1);
  });
});

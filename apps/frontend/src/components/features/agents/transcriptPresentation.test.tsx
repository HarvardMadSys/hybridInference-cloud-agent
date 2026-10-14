// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  TranscriptPresentationControl,
  transcriptPresentationKey,
  useTranscriptPresentation,
  type TranscriptPresentation,
} from './transcriptPresentation';

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function DisplayControl() {
  const [value, onChange] = useState<TranscriptPresentation>('standard');
  return <TranscriptPresentationControl value={value} onChange={onChange} />;
}

describe('activity display menu', () => {
  it('explains the modes and updates the current selection without leaving chat', () => {
    render(<DisplayControl />);
    const trigger = screen.getByRole('button', { name: 'Activity: Standard' });
    fireEvent.click(trigger);
    expect(screen.getByRole('menu', { name: 'Show in chat' })).toHaveAccessibleDescription(
      'Only changes the display, not how the agent runs.',
    );
    expect(screen.getByRole('menuitemradio', { name: 'Compact' })).toHaveAccessibleDescription(
      'Group completed steps; expand when needed.',
    );
    expect(screen.getByRole('menuitemradio', { name: 'Standard' })).toHaveFocus();
    fireEvent.click(screen.getByRole('menuitemradio', { name: 'Detailed' }));
    expect(trigger).toHaveAccessibleName('Activity: Detailed');
    expect(trigger).toHaveFocus();
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    fireEvent.click(trigger);
    expect(screen.getByRole('menuitemradio', { name: 'Detailed' })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.getByRole('menuitemradio', { name: 'Detailed' })).toHaveFocus();
  });

  it('supports keyboard navigation and consumes Escape before run cancellation', () => {
    render(<DisplayControl />);
    const trigger = screen.getByRole('button', { name: 'Activity: Standard' });
    fireEvent.keyDown(trigger, { key: 'ArrowDown' });
    const menu = screen.getByRole('menu');
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(screen.getByRole('menuitemradio', { name: 'Detailed' })).toHaveFocus();
    fireEvent.keyDown(menu, { key: 'ArrowDown' });
    expect(screen.getByRole('menuitemradio', { name: 'Compact' })).toHaveFocus();
    fireEvent.keyDown(menu, { key: 'End' });
    expect(screen.getByRole('menuitemradio', { name: 'Detailed' })).toHaveFocus();
    fireEvent.keyDown(menu, { key: 'Home' });
    expect(screen.getByRole('menuitemradio', { name: 'Compact' })).toHaveFocus();
    fireEvent.keyDown(menu, { key: 'ArrowUp' });
    expect(screen.getByRole('menuitemradio', { name: 'Detailed' })).toHaveFocus();

    const cancelRun = vi.fn();
    window.addEventListener('keydown', cancelRun);
    try {
      fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
      expect(cancelRun).not.toHaveBeenCalled();
      expect(screen.queryByRole('menu')).not.toBeInTheDocument();
      expect(trigger).toHaveFocus();
      fireEvent.keyDown(trigger, { key: 'Escape' });
      expect(cancelRun).toHaveBeenCalledOnce();
    } finally {
      window.removeEventListener('keydown', cancelRun);
    }
  });

  it('dismisses on outside interaction and returns Tab navigation to the trigger', () => {
    render(
      <>
        <DisplayControl />
        <button type="button">Next control</button>
      </>,
    );
    const trigger = screen.getByRole('button', { name: 'Activity: Standard' });
    fireEvent.click(trigger);
    fireEvent.pointerDown(screen.getByRole('button', { name: 'Next control' }));
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    fireEvent.click(trigger);
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Tab' });
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    fireEvent.click(trigger);
    act(() => screen.getByRole('button', { name: 'Next control' }).focus());
    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
  });
});

describe('conversation work-detail preferences', () => {
  it('cannot collide when identifiers contain delimiter punctuation', () => {
    expect(transcriptPresentationKey('a.b', 'c')).not.toBe(transcriptPresentationKey('a', 'b.c'));
    expect(transcriptPresentationKey('a:b', 'c')).not.toBe(transcriptPresentationKey('a', 'b:c'));
  });
  it('restores an account and thread choice after remount', () => {
    const first = renderHook(() => useTranscriptPresentation('alice', 'thread'));
    act(() => first.result.current[1]('compact'));
    expect(localStorage.getItem(transcriptPresentationKey('alice', 'thread'))).toBe('compact');
    first.unmount();
    const restored = renderHook(() => useTranscriptPresentation('alice', 'thread'));
    expect(restored.result.current[0]).toBe('compact');
  });

  it('keeps conversation and account choices separate across navigation', () => {
    localStorage.setItem(transcriptPresentationKey('alice', 'first'), 'detailed');
    const { result, rerender } = renderHook(
      ({ user, thread }) => useTranscriptPresentation(user, thread),
      { initialProps: { user: 'alice', thread: 'first' } },
    );
    expect(result.current[0]).toBe('detailed');
    const staleUpdate = result.current[1];
    rerender({ user: 'alice', thread: 'second' });
    expect(result.current[0]).toBe('standard');
    act(() => staleUpdate('compact'));
    expect(result.current[0]).toBe('standard');
    act(() => result.current[1]('compact'));
    rerender({ user: 'bob', thread: 'second' });
    expect(result.current[0]).toBe('standard');
    expect(localStorage.getItem(transcriptPresentationKey('alice', 'first'))).toBe('detailed');
    expect(localStorage.getItem(transcriptPresentationKey('alice', 'second'))).toBe('compact');
  });

  it('uses the default for malformed values and still changes mode with blocked storage', () => {
    localStorage.setItem(transcriptPresentationKey('alice', 'thread'), 'not-a-mode');
    const { result } = renderHook(() => useTranscriptPresentation('alice', 'thread'));
    expect(result.current[0]).toBe('standard');
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    act(() => result.current[1]('detailed'));
    expect(result.current[0]).toBe('detailed');
  });
});

// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { transcriptPresentationKey, useTranscriptPresentation } from './transcriptPresentation';

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
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

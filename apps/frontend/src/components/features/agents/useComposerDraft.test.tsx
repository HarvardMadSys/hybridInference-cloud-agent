// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { composerDraftStorageKey, useComposerDraft } from './useComposerDraft';

beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('composer drafts', () => {
  it('restores exact text after a composer remounts', () => {
    const first = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    act(() => first.result.current.setValue('  Continue this task\nwith these details.  '));
    first.unmount();
    const next = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    expect(next.result.current.value).toBe('  Continue this task\nwith these details.  ');
  });

  it('isolates accounts and chats immediately and restores a draft on return', () => {
    const hook = renderHook((scope) => useComposerDraft(scope), {
      initialProps: { userId: 'alice', threadId: 'first' },
    });
    act(() => hook.result.current.setValue('Alice’s first draft'));
    const firstSetter = hook.result.current.setValue;
    hook.rerender({ userId: 'bob', threadId: 'first' });
    expect(hook.result.current.value).toBe('');
    act(() => hook.result.current.setValue('Bob’s draft'));
    act(() => firstSetter((value) => `${value}, updated`));
    expect(hook.result.current.value).toBe('Bob’s draft');
    hook.rerender({ userId: 'alice', threadId: 'second' });
    expect(hook.result.current.value).toBe('');
    hook.rerender({ userId: 'alice', threadId: 'first' });
    expect(hook.result.current.value).toBe('Alice’s first draft, updated');
    hook.rerender({ userId: 'bob', threadId: 'first' });
    expect(hook.result.current.value).toBe('Bob’s draft');
  });

  it('captures same-event functional updates and clears only a successfully sent draft', () => {
    const { result } = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    act(() => {
      result.current.setValue('Start');
      result.current.setValue((value) => `${value}\nagain`);
      const sent = result.current.capture();
      expect(sent.value).toBe('Start\nagain');
      result.current.acknowledge(sent);
    });
    expect(result.current.value).toBe('');
    const restored = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    expect(restored.result.current.value).toBe('');
  });

  it('keeps typing made while a send is pending, including an edit back to the same text', () => {
    const { result } = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    act(() => result.current.setValue('First message'));
    const sent = result.current.capture();
    act(() => result.current.setValue('Next message'));
    act(() => result.current.acknowledge(sent));
    expect(result.current.value).toBe('Next message');
    act(() => result.current.setValue('First message'));
    act(() => result.current.acknowledge(sent));
    expect(result.current.value).toBe('First message');
  });

  it('acknowledges an old chat without clearing the chat opened during the request', () => {
    const hook = renderHook((scope) => useComposerDraft(scope), {
      initialProps: { userId: 'alice', threadId: 'first' },
    });
    act(() => hook.result.current.setValue('Sent from first chat'));
    const sent = hook.result.current.capture();
    hook.rerender({ userId: 'alice', threadId: 'second' });
    act(() => hook.result.current.setValue('Second chat draft'));
    act(() => hook.result.current.acknowledge(sent));
    expect(hook.result.current.value).toBe('Second chat draft');
    hook.rerender({ userId: 'alice', threadId: 'first' });
    expect(hook.result.current.value).toBe('');
  });

  it('preserves edits made by another composer while a request is pending', () => {
    const first = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    act(() => first.result.current.setValue('First message'));
    const sent = first.result.current.capture();
    const second = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    act(() => second.result.current.setValue('Draft from another tab'));
    act(() => first.result.current.acknowledge(sent));
    expect(first.result.current.value).toBe('Draft from another tab');
    expect(second.result.current.value).toBe('Draft from another tab');
  });

  it('does not clear a new copy of the same text after a sent draft was cleared and recreated', () => {
    const first = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    act(() => first.result.current.setValue('Repeat this message'));
    const sent = first.result.current.capture();
    const second = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    act(() => second.result.current.acknowledge(second.result.current.capture()));
    second.unmount();
    const third = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    act(() => third.result.current.setValue('Repeat this message'));
    act(() => first.result.current.acknowledge(sent));
    expect(first.result.current.value).toBe('Repeat this message');
    expect(third.result.current.value).toBe('Repeat this message');
  });

  it('clears a composer opened again while its send was out, once the send lands', () => {
    const left = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    act(() => left.result.current.setValue('Send this once'));
    const sent = left.result.current.capture();
    const { acknowledge } = left.result.current;
    left.unmount();
    const reopened = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    const other = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'other' }));
    act(() => other.result.current.setValue('Send this once'));
    expect(reopened.result.current.value).toBe('Send this once');
    act(() => acknowledge(sent));
    expect(reopened.result.current.value).toBe('');
    expect(other.result.current.value).toBe('Send this once');
  });

  it('clears a composer opened again when its send lands before the composer listens', () => {
    const left = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    act(() => left.result.current.setValue('Send this once'));
    const sent = left.result.current.capture();
    const { acknowledge } = left.result.current;
    left.unmount();
    let landed = false;
    const reopened = renderHook(() => {
      const draft = useComposerDraft({ userId: 'alice', threadId: 'chat' });
      // The send answers after this render read the draft, and before the
      // composer's effects have run.
      if (!landed) {
        landed = true;
        acknowledge(sent);
      }
      return draft;
    });
    expect(reopened.result.current.value).toBe('');
  });

  it('keeps what a composer opened again typed while the send it showed was out', () => {
    const left = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    act(() => left.result.current.setValue('Send this once'));
    const sent = left.result.current.capture();
    const { acknowledge } = left.result.current;
    left.unmount();
    const reopened = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    act(() => reopened.result.current.setValue('Typed after coming back'));
    act(() => acknowledge(sent));
    expect(reopened.result.current.value).toBe('Typed after coming back');
  });

  it('works in memory when browser storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('Storage denied');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('Storage denied');
    });
    const hook = renderHook((scope) => useComposerDraft(scope), {
      initialProps: { userId: 'alice', threadId: 'first' },
    });
    act(() => hook.result.current.setValue('Still editable'));
    hook.rerender({ userId: 'alice', threadId: 'second' });
    expect(hook.result.current.value).toBe('');
    hook.rerender({ userId: 'alice', threadId: 'first' });
    expect(hook.result.current.value).toBe('Still editable');
    act(() => hook.result.current.acknowledge(hook.result.current.capture()));
    expect(hook.result.current.value).toBe('');
  });

  it('ignores malformed saved data and never stores anonymous drafts under a shared account', () => {
    localStorage.setItem(composerDraftStorageKey('alice', 'chat'), '{broken');
    const restored = renderHook(() => useComposerDraft({ userId: 'alice', threadId: 'chat' }));
    expect(restored.result.current.value).toBe('');
    const write = vi.spyOn(Storage.prototype, 'setItem');
    const anonymous = renderHook(() => useComposerDraft({ threadId: 'chat' }));
    act(() => anonymous.result.current.setValue('Private to this mounted composer'));
    expect(anonymous.result.current.value).toBe('Private to this mounted composer');
    expect(write).not.toHaveBeenCalled();
  });
});

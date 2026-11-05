// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import {
  DEFAULT_KEYBOARD_SHORTCUTS,
  matchesShortcut,
  parseKeyboardShortcuts,
} from '@/lib/keyboardShortcuts';
import { onPlatform } from '@/lib/platformTestSupport';
import { KeyboardShortcutsSettings } from './KeyboardShortcutsSettings';
import { keyboardShortcutsStorageKey, useKeyboardShortcuts } from './keyboardShortcutPreferences';

const auth = vi.hoisted(() => ({ userId: 'shortcuts-user' as string | undefined }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: auth.userId ? { id: auth.userId } : null } }),
}));

let accountNumber = 0;
beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  auth.userId = `shortcuts-user-${++accountNumber}`;
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function renderSettings() {
  return render(
    <LocaleProvider>
      <KeyboardShortcutsSettings />
    </LocaleProvider>,
  );
}

function record(action = 'Toggle task list') {
  fireEvent.click(screen.getByRole('button', { name: `Change shortcut for ${action}` }));
  return screen.getByRole('textbox', { name: `New shortcut for ${action}` });
}

describe('keyboard shortcut settings', () => {
  it('records a chord, updates active subscribers, persists, disables, and resets', async () => {
    const view = renderSettings();
    const monitor = renderHook(() => useKeyboardShortcuts(auth.userId));
    expect(screen.getByRole('button', { name: 'Reset to defaults' })).toBeDisabled();
    fireEvent.keyDown(record(), { key: 'B', ctrlKey: true, shiftKey: true });
    expect(monitor.result.current[0].sidebar).toEqual({ key: 'b', shift: true });
    expect(
      JSON.parse(localStorage.getItem(keyboardShortcutsStorageKey(auth.userId!))!).sidebar,
    ).toEqual({ key: 'b', shift: true });
    await waitFor(() =>
      expect(
        screen.getByRole('button', { name: 'Change shortcut for Toggle task list' }),
      ).toHaveFocus(),
    );
    view.unmount();
    renderSettings();
    expect(
      screen.getByRole('button', { name: 'Change shortcut for Toggle task list' }),
    ).toHaveTextContent('Ctrl + Shift + B');
    fireEvent.click(screen.getByRole('button', { name: 'Disable shortcut for Toggle task list' }));
    expect(monitor.result.current[0].sidebar).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Reset to defaults' }));
    expect(monitor.result.current[0]).toEqual(DEFAULT_KEYBOARD_SHORTCUTS);
  });

  it('keeps the old shortcut when a candidate conflicts, is reserved, or has no modifier', () => {
    renderSettings();
    const input = record();
    fireEvent.keyDown(input, { key: 'k', ctrlKey: true });
    expect(screen.getByRole('alert')).toHaveTextContent('Already used by “Search chats”');
    fireEvent.keyDown(input, { key: 'w', ctrlKey: true });
    expect(screen.getByRole('alert')).toHaveTextContent('reserved');
    fireEvent.keyDown(input, { key: 'g' });
    expect(screen.getByRole('alert')).toHaveTextContent('Use Ctrl with a letter');
    // Off Apple platforms the Windows key is not the modifier shortcuts use.
    fireEvent.keyDown(input, { key: 'g', metaKey: true });
    expect(screen.getByRole('alert')).toHaveTextContent('Use Ctrl with a letter');
    expect(localStorage.getItem(keyboardShortcutsStorageKey(auth.userId!))).toBeNull();
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Change shortcut for Toggle task list' }),
    ).toHaveTextContent('Ctrl + B');
  });

  it('records and names Command on a Mac, where Control edits text', () => {
    onPlatform('MacIntel');
    const view = renderSettings();
    expect(screen.getByText(/press Command with a letter/)).toBeInTheDocument();
    expect(screen.getByText('⌘ + Enter')).toBeInTheDocument();
    const input = record();
    fireEvent.keyDown(input, { key: 'g', ctrlKey: true });
    expect(screen.getByRole('alert')).toHaveTextContent('Use Command with a letter');
    expect(localStorage.getItem(keyboardShortcutsStorageKey(auth.userId!))).toBeNull();
    fireEvent.keyDown(input, { key: 'g', metaKey: true });
    expect(
      JSON.parse(localStorage.getItem(keyboardShortcutsStorageKey(auth.userId!))!).sidebar,
    ).toEqual({ key: 'g', shift: false });
    view.unmount();
    renderSettings();
    expect(
      screen.getByRole('button', { name: 'Change shortcut for Toggle task list' }),
    ).toHaveTextContent('⌘ + G');
  });

  it('ignores IME and repeated keys, and lets Tab leave the recorder', () => {
    renderSettings();
    const input = record();
    fireEvent.keyDown(input, { key: 'g', ctrlKey: true, isComposing: true });
    fireEvent.keyDown(input, { key: 'g', ctrlKey: true, keyCode: 229 });
    fireEvent.keyDown(input, { key: 'g', ctrlKey: true, repeat: true });
    expect(localStorage.getItem(keyboardShortcutsStorageKey(auth.userId!))).toBeNull();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(fireEvent.keyDown(input, { key: 'Tab' })).toBe(true);
    fireEvent.blur(input);
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('does not let a capture-phase app shortcut run while recording', () => {
    const triggered = vi.fn();
    const listener = (event: KeyboardEvent) => {
      if (matchesShortcut(event, DEFAULT_KEYBOARD_SHORTCUTS.workspace)) triggered();
    };
    window.addEventListener('keydown', listener, true);
    try {
      renderSettings();
      fireEvent.keyDown(record(), { key: 'j', ctrlKey: true });
      expect(triggered).not.toHaveBeenCalled();
      expect(screen.getByRole('alert')).toHaveTextContent('Toggle workspace');
    } finally {
      window.removeEventListener('keydown', listener, true);
    }
  });

  it('provides Chinese labels and built-in chat shortcuts', () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    renderSettings();
    expect(screen.getByRole('region', { name: '键盘快捷键' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '修改“搜索聊天”的快捷键' })).toBeInTheDocument();
    expect(screen.getByText('Shift + Enter')).toBeInTheDocument();
    expect(screen.getByText('聊天内置快捷键')).toBeInTheDocument();
  });

  it('disables editing without an account', () => {
    auth.userId = undefined;
    renderSettings();
    expect(screen.getByRole('button', { name: 'Change shortcut for Search chats' })).toBeDisabled();
  });
});

describe('account-scoped shortcut preferences', () => {
  it('follows other tabs, clears, and account changes without retaining an old account writer', () => {
    const firstUser = auth.userId!;
    const monitor = renderHook(({ id }) => useKeyboardShortcuts(id), {
      initialProps: { id: firstUser },
    });
    const next = { ...DEFAULT_KEYBOARD_SHORTCUTS, search: null };
    act(() => {
      localStorage.setItem(keyboardShortcutsStorageKey(firstUser), JSON.stringify(next));
      window.dispatchEvent(
        new StorageEvent('storage', {
          key: keyboardShortcutsStorageKey(firstUser),
          newValue: JSON.stringify(next),
          storageArea: localStorage,
        }),
      );
    });
    expect(monitor.result.current[0].search).toBeNull();
    const staleWriter = monitor.result.current[1];
    monitor.rerender({ id: 'different-shortcuts-user' });
    expect(monitor.result.current[0]).toEqual(DEFAULT_KEYBOARD_SHORTCUTS);
    act(() => staleWriter(DEFAULT_KEYBOARD_SHORTCUTS));
    expect(
      JSON.parse(localStorage.getItem(keyboardShortcutsStorageKey(firstUser))!).search,
    ).toBeNull();
    monitor.rerender({ id: firstUser });
    expect(monitor.result.current[0].search).toBeNull();
    act(() => {
      localStorage.clear();
      window.dispatchEvent(new StorageEvent('storage', { key: null, storageArea: localStorage }));
    });
    expect(monitor.result.current[0]).toEqual(DEFAULT_KEYBOARD_SHORTCUTS);
  });

  it('keeps changes usable for this visit when browser storage is blocked', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    const monitor = renderHook(() => useKeyboardShortcuts(auth.userId));
    act(() => monitor.result.current[1]({ ...DEFAULT_KEYBOARD_SHORTCUTS, workspace: null }));
    expect(monitor.result.current[0].workspace).toBeNull();
    monitor.unmount();
    const remounted = renderHook(() => useKeyboardShortcuts(auth.userId));
    expect(remounted.result.current[0].workspace).toBeNull();
  });

  it.each([
    'broken json',
    '[]',
    '{"search":{"key":"b","shift":false}}',
    '{"sidebar":{"key":"w","shift":false}}',
  ])('recovers safely from invalid stored preferences: %s', (raw) => {
    expect(parseKeyboardShortcuts(raw)).toEqual(DEFAULT_KEYBOARD_SHORTCUTS);
  });
});

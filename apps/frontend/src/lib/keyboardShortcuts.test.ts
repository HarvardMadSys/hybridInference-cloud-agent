// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import {
  DEFAULT_KEYBOARD_SHORTCUTS,
  chordAriaLabel,
  chordLabel,
  hasPrimaryModifier,
  isApplePlatform,
  matchesShortcut,
  shortcutAriaLabel,
  shortcutLabel,
} from './keyboardShortcuts';
import { onPlatform } from './platformTestSupport';

type Hinted = Navigator & { userAgentData?: { platform: string } };

afterEach(() => {
  delete (navigator as Hinted).userAgentData;
});

function press(init: KeyboardEventInit): KeyboardEvent {
  return new KeyboardEvent('keydown', { bubbles: true, cancelable: true, ...init });
}

/** Chromium's user-agent client hint, which jsdom does not have. */
function hint(platform: string) {
  Object.defineProperty(navigator, 'userAgentData', { value: { platform }, configurable: true });
}

describe('the primary modifier', () => {
  it.each([
    ['MacIntel', true],
    ['iPhone', true],
    ['iPad', true],
    ['Win32', false],
    ['Linux x86_64', false],
    ['', false],
  ])('reads %j as Apple: %s', (platform, apple) => {
    onPlatform(platform);
    expect(isApplePlatform()).toBe(apple);
  });

  it('prefers the user-agent hint where the browser gives one', () => {
    onPlatform('Win32');
    hint('macOS');
    expect(isApplePlatform()).toBe(true);
    onPlatform('MacIntel');
    hint('Windows');
    expect(isApplePlatform()).toBe(false);
    hint('');
    expect(isApplePlatform()).toBe(true);
  });

  it('is Command alone on a Mac and Control alone elsewhere', () => {
    onPlatform('MacIntel');
    expect(hasPrimaryModifier({ metaKey: true, ctrlKey: false })).toBe(true);
    expect(hasPrimaryModifier({ metaKey: false, ctrlKey: true })).toBe(false);
    expect(hasPrimaryModifier({ metaKey: true, ctrlKey: true })).toBe(false);
    onPlatform('Win32');
    expect(hasPrimaryModifier({ metaKey: false, ctrlKey: true })).toBe(true);
    expect(hasPrimaryModifier({ metaKey: true, ctrlKey: false })).toBe(false);
    expect(hasPrimaryModifier({ metaKey: true, ctrlKey: true })).toBe(false);
  });
});

describe('app shortcuts', () => {
  it('leave Control+K and Control+B to text fields on a Mac', () => {
    onPlatform('MacIntel');
    // Kill to the end of the line, and back one character.
    expect(
      matchesShortcut(press({ key: 'k', ctrlKey: true }), DEFAULT_KEYBOARD_SHORTCUTS.search),
    ).toBe(false);
    expect(
      matchesShortcut(press({ key: 'b', ctrlKey: true }), DEFAULT_KEYBOARD_SHORTCUTS.sidebar),
    ).toBe(false);
    expect(
      matchesShortcut(press({ key: 'k', metaKey: true }), DEFAULT_KEYBOARD_SHORTCUTS.search),
    ).toBe(true);
    expect(
      matchesShortcut(press({ key: 'b', metaKey: true }), DEFAULT_KEYBOARD_SHORTCUTS.sidebar),
    ).toBe(true);
  });

  it('answer to Control, not the Windows key, elsewhere', () => {
    onPlatform('Win32');
    expect(
      matchesShortcut(press({ key: 'k', ctrlKey: true }), DEFAULT_KEYBOARD_SHORTCUTS.search),
    ).toBe(true);
    expect(
      matchesShortcut(press({ key: 'k', metaKey: true }), DEFAULT_KEYBOARD_SHORTCUTS.search),
    ).toBe(false);
  });

  it('are labelled with the modifier they answer to', () => {
    const binding = { key: 'b', shift: true };
    expect(shortcutLabel(binding, true)).toBe('⌘ + Shift + B');
    expect(shortcutLabel(DEFAULT_KEYBOARD_SHORTCUTS.search!, false)).toBe('Ctrl + K');
    expect(shortcutAriaLabel(binding, true)).toBe('Meta+Shift+B');
    expect(shortcutAriaLabel(DEFAULT_KEYBOARD_SHORTCUTS.search, false)).toBe('Control+K');
    expect(shortcutAriaLabel(null, true)).toBeUndefined();
    expect(chordLabel('Enter', true)).toBe('⌘ + Enter');
    expect(chordLabel('Enter', false)).toBe('Ctrl + Enter');
    expect(chordAriaLabel('Enter', true)).toBe('Meta+Enter');
    expect(chordAriaLabel('Enter', false)).toBe('Control+Enter');
  });
});

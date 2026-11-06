// @vitest-environment jsdom
import type { KeyboardEvent } from 'react';
import { expect, it } from 'vitest';
import { onPlatform } from '@/lib/platformTestSupport';
import { isComposerSubmit, isSteerChord } from './composerKeys';

function enter(modifiers: Partial<Record<'metaKey' | 'ctrlKey' | 'shiftKey', boolean>>) {
  return {
    key: 'Enter',
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    ...modifiers,
    nativeEvent: { isComposing: false, keyCode: 13 },
  } as unknown as KeyboardEvent<HTMLElement>;
}

it.each([
  ['MacIntel', { metaKey: true }, { ctrlKey: true }],
  ['Win32', { ctrlKey: true }, { metaKey: true }],
])(
  'steers on %s with %j+Enter only; %j+Enter is still a plain send',
  (platform, primary, other) => {
    onPlatform(platform);
    expect(isSteerChord(enter(primary))).toBe(true);
    expect(isSteerChord(enter(other))).toBe(false);
    expect(isSteerChord(enter({ ...primary, shiftKey: true }))).toBe(false);
    expect(isComposerSubmit(enter(other))).toBe(true);
  },
);

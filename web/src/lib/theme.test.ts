// @vitest-environment jsdom
import { describe, expect, it, beforeEach } from 'vitest';
import {
  DARK_CLASS,
  DEFAULT_THEME_PREFERENCE,
  THEME_STORAGE_KEY,
  applyTheme,
  isThemePreference,
  readStoredTheme,
  resolveTheme,
  themeBootScript,
  writeStoredTheme,
} from './theme';

function storage(initial: Record<string, string> = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => void map.set(key, value),
    read: (key: string) => map.get(key) ?? null,
  };
}

describe('resolveTheme', () => {
  it('takes an explicit choice over the system setting', () => {
    expect(resolveTheme('light', true)).toBe('light');
    expect(resolveTheme('dark', false)).toBe('dark');
  });

  it('follows the system setting only for `system`', () => {
    expect(resolveTheme('system', true)).toBe('dark');
    expect(resolveTheme('system', false)).toBe('light');
  });
});

describe('stored preference', () => {
  it('round-trips', () => {
    const store = storage();
    writeStoredTheme('dark', store);
    expect(store.read(THEME_STORAGE_KEY)).toBe('dark');
    expect(readStoredTheme(store)).toBe('dark');
  });

  it('falls back to the default for a value it does not recognise', () => {
    expect(readStoredTheme(storage({ [THEME_STORAGE_KEY]: 'neon' }))).toBe(
      DEFAULT_THEME_PREFERENCE,
    );
  });

  it('survives storage that throws', () => {
    const blocked = {
      getItem: () => {
        throw new Error('blocked');
      },
      setItem: () => {
        throw new Error('blocked');
      },
    };
    expect(readStoredTheme(blocked)).toBe(DEFAULT_THEME_PREFERENCE);
    expect(() => writeStoredTheme('dark', blocked)).not.toThrow();
  });

  it('keeps `system` as `system` rather than flattening it to what the OS says now', () => {
    // Persisting the *resolved* value would silently convert "follow my OS"
    // into "always light" the first time it was saved during the day.
    const store = storage();
    writeStoredTheme('system', store);
    expect(store.read(THEME_STORAGE_KEY)).toBe('system');
  });
});

describe('isThemePreference', () => {
  it('accepts the three preferences and nothing else', () => {
    expect(['light', 'dark', 'system'].every(isThemePreference)).toBe(true);
    expect([null, undefined, '', 'Dark', 42].some(isThemePreference)).toBe(false);
  });
});

describe('applyTheme', () => {
  it('drives both the Tailwind class and the native colour scheme', () => {
    const root = document.createElement('html');
    applyTheme('dark', root);
    expect(root.classList.contains(DARK_CLASS)).toBe(true);
    expect(root.style.colorScheme).toBe('dark');
    applyTheme('light', root);
    expect(root.classList.contains(DARK_CLASS)).toBe(false);
    expect(root.style.colorScheme).toBe('light');
  });
});

describe('themeBootScript', () => {
  beforeEach(() => {
    document.documentElement.className = '';
    window.localStorage.clear();
  });

  function boot() {
    // eslint-disable-next-line no-eval
    eval(themeBootScript());
  }

  it('paints dark before React runs when that is the stored choice', () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'dark');
    boot();
    expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(true);
  });

  it('paints light for a stored light choice even when the OS is dark', () => {
    window.matchMedia = (() => ({ matches: true })) as unknown as typeof window.matchMedia;
    window.localStorage.setItem(THEME_STORAGE_KEY, 'light');
    boot();
    expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(false);
  });

  it('follows the OS when the stored choice is `system`', () => {
    window.matchMedia = (() => ({ matches: true })) as unknown as typeof window.matchMedia;
    window.localStorage.setItem(THEME_STORAGE_KEY, 'system');
    boot();
    expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(true);
  });

  it('does not throw when storage is unavailable', () => {
    // A boot script that throws takes the rest of <head> with it.
    const original = Object.getOwnPropertyDescriptor(window, 'localStorage');
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      get() {
        throw new Error('blocked');
      },
    });
    expect(boot).not.toThrow();
    if (original) Object.defineProperty(window, 'localStorage', original);
  });

  it('agrees with the module it was generated from', () => {
    // The script is hand-written JS; this is what stops it drifting from
    // `readStoredTheme`/`resolveTheme` when either changes.
    expect(themeBootScript()).toContain(JSON.stringify(THEME_STORAGE_KEY));
    expect(themeBootScript()).toContain(JSON.stringify(DARK_CLASS));
  });
});

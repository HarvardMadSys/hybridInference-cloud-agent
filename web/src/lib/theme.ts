/**
 * Theme preference: what the user picked, and what that resolves to now.
 *
 * Two values, deliberately kept apart. The *preference* is tri-state and is
 * what we persist; `system` is a standing instruction to follow the OS, not a
 * third colour scheme, so it has to survive a reload as `system` rather than
 * being flattened to whatever the OS happened to say at the time. The
 * *resolved* theme is binary and is what gets painted.
 *
 * Everything here is pure so the no-flash boot script (`themeBootScript`) and
 * the React provider can share one definition of "which class goes on <html>"
 * instead of drifting into two.
 */

export const THEME_STORAGE_KEY = 'agents.theme';

export const THEME_PREFERENCES = ['light', 'dark', 'system'] as const;
export type ThemePreference = (typeof THEME_PREFERENCES)[number];

/** The two schemes a preference can resolve to. */
export type ResolvedTheme = 'light' | 'dark';

export const DEFAULT_THEME_PREFERENCE: ThemePreference = 'system';

export const DARK_MEDIA_QUERY = '(prefers-color-scheme: dark)';

/** Tailwind is configured `darkMode: 'class'`, so this is the whole contract. */
export const DARK_CLASS = 'dark';

export function isThemePreference(value: unknown): value is ThemePreference {
  return typeof value === 'string' && (THEME_PREFERENCES as readonly string[]).includes(value);
}

export function resolveTheme(
  preference: ThemePreference,
  systemPrefersDark: boolean,
): ResolvedTheme {
  if (preference === 'system') return systemPrefersDark ? 'dark' : 'light';
  return preference;
}

/**
 * Read the stored preference.
 *
 * Storage can be blocked outright (Safari's "block all cookies", some embedded
 * webviews), and the stored value can be anything if someone hand-edits it, so
 * both failures fall back to the default rather than throwing on boot.
 */
export function readStoredTheme(storage?: Pick<Storage, 'getItem'>): ThemePreference {
  try {
    const store = storage ?? window.localStorage;
    const raw = store.getItem(THEME_STORAGE_KEY);
    return isThemePreference(raw) ? raw : DEFAULT_THEME_PREFERENCE;
  } catch {
    return DEFAULT_THEME_PREFERENCE;
  }
}

export function writeStoredTheme(
  preference: ThemePreference,
  storage?: Pick<Storage, 'setItem'>,
): void {
  try {
    const store = storage ?? window.localStorage;
    store.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    // A preference that cannot be persisted still applies to this tab.
  }
}

export function systemPrefersDark(): boolean {
  try {
    return window.matchMedia(DARK_MEDIA_QUERY).matches;
  } catch {
    // `matchMedia` is absent in some test environments and old webviews.
    return false;
  }
}

/** Put (or remove) the class Tailwind keys `dark:` off of. */
export function applyTheme(theme: ResolvedTheme, root?: HTMLElement): void {
  const element = root ?? document.documentElement;
  element.classList.toggle(DARK_CLASS, theme === 'dark');
  // Lets the browser paint native widgets — scrollbars, form controls, the
  // spellcheck underline — in the matching scheme. Without it a dark page
  // keeps light scrollbars.
  element.style.colorScheme = theme;
}

/**
 * The script that runs before first paint.
 *
 * Without it the document renders light, then the provider's effect flips it —
 * a white flash on every load for dark users. It is inlined into <head>, so it
 * must be self-contained and must not throw: a boot script that throws takes
 * the rest of the document's head with it.
 *
 * Built from the same constants as the module above so the two cannot drift.
 */
export function themeBootScript(): string {
  return [
    '(function(){try{',
    `var p=localStorage.getItem(${JSON.stringify(THEME_STORAGE_KEY)});`,
    `if(p!=='light'&&p!=='dark')p=${JSON.stringify(DEFAULT_THEME_PREFERENCE)};`,
    `var d=p==='dark'||(p==='system'&&matchMedia(${JSON.stringify(DARK_MEDIA_QUERY)}).matches);`,
    `document.documentElement.classList.toggle(${JSON.stringify(DARK_CLASS)},d);`,
    "document.documentElement.style.colorScheme=d?'dark':'light';",
    '}catch(e){}})()',
  ].join('');
}

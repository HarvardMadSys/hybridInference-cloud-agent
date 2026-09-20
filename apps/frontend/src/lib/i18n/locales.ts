/**
 * Which languages the UI ships, and how a first-time visitor gets one.
 *
 * Kept separate from the dictionaries so the boot script, the provider and the
 * settings picker can agree on the list without importing every translation.
 */

export const LOCALE_STORAGE_KEY = 'agents.locale';

export const LOCALES = ['en', 'zh-CN'] as const;
export type Locale = (typeof LOCALES)[number];

export const DEFAULT_LOCALE: Locale = 'en';

/**
 * The name of each language *in that language* — a reader who has landed in
 * the wrong one still has to be able to find their way out, and "Chinese
 * (Simplified)" is no help to someone who cannot read the English around it.
 */
export const LOCALE_LABELS: Record<Locale, string> = {
  en: 'English',
  'zh-CN': '简体中文',
};

/** The `lang` attribute for each locale, for screen readers and hyphenation. */
export const LOCALE_HTML_LANG: Record<Locale, string> = {
  en: 'en',
  'zh-CN': 'zh-Hans',
};

export function isLocale(value: unknown): value is Locale {
  return typeof value === 'string' && (LOCALES as readonly string[]).includes(value);
}

/**
 * Pick a shipped locale for someone who has never chosen one.
 *
 * Matches on the primary subtag rather than the full tag, so `zh-TW`, `zh-HK`
 * and bare `zh` all land on Simplified Chinese instead of silently falling
 * through to English. That is the better wrong answer of the two: a Traditional
 * reader gets Chinese they can mostly read and a visible way to switch, where
 * English gives them neither.
 */
export function negotiateLocale(preferred: readonly string[]): Locale {
  for (const tag of preferred) {
    if (isLocale(tag)) return tag;
    const primary = tag.split('-')[0]?.toLowerCase();
    const match = LOCALES.find((locale) => locale.split('-')[0].toLowerCase() === primary);
    if (match) return match;
  }
  return DEFAULT_LOCALE;
}

export function readStoredLocale(storage?: Pick<Storage, 'getItem'>): Locale | null {
  try {
    const store = storage ?? window.localStorage;
    const raw = store.getItem(LOCALE_STORAGE_KEY);
    return isLocale(raw) ? raw : null;
  } catch {
    return null;
  }
}

export function writeStoredLocale(locale: Locale, storage?: Pick<Storage, 'setItem'>): void {
  try {
    const store = storage ?? window.localStorage;
    store.setItem(LOCALE_STORAGE_KEY, locale);
  } catch {
    // Not persisting the choice still leaves it applied to this tab.
  }
}

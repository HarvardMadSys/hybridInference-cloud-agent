/**
 * Which languages the UI ships, and how a first-time visitor gets one.
 *
 * Kept separate from the dictionaries so the boot script, the provider and the
 * settings picker can agree on the list without importing every translation.
 */

export const LOCALE_STORAGE_KEY = 'agents.locale';
/** When this browser's language was chosen (ISO time), for an account to compare. */
export const LOCALE_CHOSEN_AT_KEY = 'agents.locale.chosen_at';
/** Whose choice it is: the account it was made in, or empty when made signed out. */
export const LOCALE_OWNER_KEY = 'agents.locale.owner';
/** `true` once the choice is an account's stored one, applied here or saved from here. */
export const LOCALE_STORED_KEY = 'agents.locale.stored';

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

export function writeStoredLocale(
  locale: Locale,
  storage?: Pick<Storage, 'setItem'>,
  chosenAt: string = new Date().toISOString(),
): void {
  try {
    const store = storage ?? window.localStorage;
    store.setItem(LOCALE_STORAGE_KEY, locale);
    store.setItem(LOCALE_CHOSEN_AT_KEY, chosenAt);
    // A fresh choice on this browser, nobody's yet: the account sync claims it.
    store.setItem(LOCALE_OWNER_KEY, '');
    store.setItem(LOCALE_STORED_KEY, 'false');
  } catch {
    // Not persisting the choice still leaves it applied to this tab.
  }
}

/** When the stored language was chosen, or null when it never was here. */
export function readLocaleChosenAt(storage?: Pick<Storage, 'getItem'>): string | null {
  try {
    const raw = (storage ?? window.localStorage).getItem(LOCALE_CHOSEN_AT_KEY);
    return raw && !Number.isNaN(Date.parse(raw)) ? raw : null;
  } catch {
    return null;
  }
}

/**
 * The browser's language choice as an account sync sees it, after
 * HybridInference's `LocaleChoice`: when it was made, in which account (empty
 * when signed out), and whether it is already an account's stored choice.
 * Only a choice not yet stored, made signed out or in the same account, may be
 * carried into an account: one account's language never spreads to another
 * account signed in on the same browser.
 */
export interface LocaleChoice {
  chosenAt: string | null;
  owner: string;
  stored: boolean;
}

export function readLocaleChoice(storage?: Pick<Storage, 'getItem'>): LocaleChoice {
  try {
    const store = storage ?? window.localStorage;
    return {
      chosenAt: readLocaleChosenAt(store),
      owner: store.getItem(LOCALE_OWNER_KEY) ?? '',
      stored: store.getItem(LOCALE_STORED_KEY) === 'true',
    };
  } catch {
    return { chosenAt: null, owner: '', stored: false };
  }
}

/** Record whose the browser's choice is now, and when it was made if that changed. */
export function markLocaleChoice(
  choice: { chosenAt?: string | null; owner: string; stored: boolean },
  storage?: Pick<Storage, 'setItem'>,
): void {
  try {
    const store = storage ?? window.localStorage;
    if (choice.chosenAt) store.setItem(LOCALE_CHOSEN_AT_KEY, choice.chosenAt);
    store.setItem(LOCALE_OWNER_KEY, choice.owner);
    store.setItem(LOCALE_STORED_KEY, choice.stored ? 'true' : 'false');
  } catch {
    // Without it the next sign-in treats the choice as nobody's stored one.
  }
}

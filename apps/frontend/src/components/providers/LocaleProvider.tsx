'use client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import {
  DEFAULT_LOCALE,
  LOCALE_HTML_LANG,
  negotiateLocale,
  readStoredLocale,
  writeStoredLocale,
  type Locale,
} from '@/lib/i18n/locales';
import { translate, type Translator } from '@/lib/i18n/translate';

interface LocaleContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  /** Bound to the current locale, so callers pass a key and get a string. */
  t: Translator;
}

const LocaleContext = createContext<LocaleContextValue>({
  locale: DEFAULT_LOCALE,
  setLocale: () => {},
  t: (key, vars) => translate(DEFAULT_LOCALE, key, vars),
});

export function LocaleProvider({ children }: { children: React.ReactNode }) {
  // Same reason as the theme: the server cannot know, so both it and the first
  // client render use the default and the effect corrects it. Unlike the theme
  // there is no boot-script trick available — a script can set `lang`, but it
  // cannot retranslate markup React has not rendered yet. In practice the
  // window is invisible: everything inside the shell waits on `ProtectedRoute`,
  // which resolves after mount, so the corrected locale is in place before any
  // real text appears.
  const [locale, setLocaleState] = useState<Locale>(DEFAULT_LOCALE);

  useEffect(() => {
    const stored = readStoredLocale();
    setLocaleState(stored ?? negotiateLocale(navigator.languages ?? [navigator.language]));
  }, []);

  // Keep the document in sync for anything that reads the language off the DOM
  // rather than off React: screen readers announcing in the wrong accent,
  // hyphenation, and the browser's own translate prompt.
  useEffect(() => {
    document.documentElement.lang = LOCALE_HTML_LANG[locale];
  }, [locale]);

  const setLocale = useCallback((next: Locale) => {
    setLocaleState(next);
    writeStoredLocale(next);
  }, []);

  const t = useCallback<Translator>((key, vars) => translate(locale, key, vars), [locale]);

  const value = useMemo(() => ({ locale, setLocale, t }), [locale, setLocale, t]);

  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

export function useLocale(): LocaleContextValue {
  return useContext(LocaleContext);
}

/** Sugar for the common case — a component that only needs to translate. */
export function useT(): Translator {
  return useContext(LocaleContext).t;
}

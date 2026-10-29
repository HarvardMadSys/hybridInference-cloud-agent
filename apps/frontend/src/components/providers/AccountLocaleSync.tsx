'use client';
// The interface language follows a local account from browser to browser, as
// HybridInference keeps it with the account (`PUT /user/preferences/locale`).
// Signing in applies the account's saved language; choosing another while
// signed in saves it, and the account keeps the most recent choice.
import { useEffect, useRef } from 'react';
import { saveAccountLocale } from '@/lib/api/accountSelf';
import { isLocale } from '@/lib/i18n/locales';
import { useAuth } from './AuthProvider';
import { useLocale } from './LocaleProvider';

export function AccountLocaleSync() {
  const { state } = useAuth();
  const { locale, setLocale } = useLocale();
  const user = state.user?.auth_mode === 'local' ? state.user : null;
  const applied = useRef<string | null>(null);
  const saved = useRef<string | null>(null);
  useEffect(() => {
    if (!user) {
      applied.current = null;
      return;
    }
    if (applied.current !== user.id) {
      applied.current = user.id;
      const kept = user.identity?.locale ?? null;
      saved.current = kept;
      if (kept && isLocale(kept) && kept !== locale) {
        setLocale(kept);
        return;
      }
    }
    if (saved.current === locale) return;
    saved.current = locale;
    void saveAccountLocale(locale)
      .then((answer) => {
        saved.current = answer.locale;
        if (answer.locale && isLocale(answer.locale) && answer.locale !== locale)
          setLocale(answer.locale);
      })
      .catch(() => undefined);
  }, [user, locale, setLocale]);
  return null;
}

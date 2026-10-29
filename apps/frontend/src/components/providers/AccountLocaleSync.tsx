'use client';
// The interface language follows a local account from browser to browser, as
// HybridInference keeps it with the account (`PUT /user/preferences/locale`).
// At sign-in the most recent choice wins: a language chosen on this browser
// while signed out, after the account's own choice, is carried into the
// account with its time (the server keeps whichever is newer); otherwise the
// account's language applies here. Choosing another while signed in saves it.
import { useEffect, useRef } from 'react';
import { saveAccountLocale } from '@/lib/api/accountSelf';
import { isLocale, readLocaleChosenAt, writeLocaleChosenAt } from '@/lib/i18n/locales';
import { useAuth } from './AuthProvider';
import { useLocale } from './LocaleProvider';

function newer(mine: string | null, theirs: string | null | undefined): boolean {
  return mine !== null && (!theirs || Date.parse(mine) > Date.parse(theirs));
}

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
    let chosenAt: string | null = null;
    if (applied.current !== user.id) {
      applied.current = user.id;
      const kept = user.identity?.locale ?? null;
      const keptAt = user.identity?.locale_chosen_at ?? null;
      saved.current = kept;
      const mine = readLocaleChosenAt();
      if (kept && isLocale(kept) && !(newer(mine, keptAt) && locale !== kept)) {
        if (kept !== locale) setLocale(kept);
        // This browser now shows the account's choice, made when the account says.
        if (keptAt) writeLocaleChosenAt(keptAt);
        return;
      }
      chosenAt = mine;
    } else {
      chosenAt = readLocaleChosenAt();
    }
    if (saved.current === locale) return;
    saved.current = locale;
    void (chosenAt ? saveAccountLocale(locale, chosenAt) : saveAccountLocale(locale))
      .then((answer) => {
        saved.current = answer.locale;
        if (answer.locale && isLocale(answer.locale) && answer.locale !== locale)
          setLocale(answer.locale);
        if (answer.chosen_at) writeLocaleChosenAt(answer.chosen_at);
      })
      .catch(() => undefined);
  }, [user, locale, setLocale]);
  return null;
}

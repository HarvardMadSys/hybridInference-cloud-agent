'use client';
// The interface language follows a local account from browser to browser, as
// HybridInference keeps it with the account (`PUT /user/preferences/locale`)
// and plans the sync from its `LocaleChoice {owner, stored}`. At sign-in the
// browser's choice is carried into the account only when it is nobody's
// stored choice yet, was made signed out or in this same account, and is
// newer than the account's (the server keeps whichever is newer); otherwise
// the account's language applies here and the browser records it as that
// account's. Several accounts on one browser therefore never overwrite each
// other's language. Choosing another while signed in saves it.
import { useEffect, useRef } from 'react';
import { saveAccountLocale } from '@/lib/api/accountSelf';
import {
  isLocale,
  markLocaleChoice,
  readLocaleChoice,
  type Locale,
  type LocaleChoice,
} from '@/lib/i18n/locales';
import { useAuth } from './AuthProvider';
import { useLocale } from './LocaleProvider';

function newer(mine: string | null, theirs: string | null | undefined): boolean {
  return mine !== null && (!theirs || Date.parse(mine) > Date.parse(theirs));
}

/** Whether the browser's choice belongs in this account rather than to another. */
function carriable(choice: LocaleChoice, accountId: string): boolean {
  return !choice.stored && (choice.owner === '' || choice.owner === accountId);
}

type Identity = { locale?: string | null; locale_chosen_at?: string | null } | undefined;

/**
 * What a sign-in does: apply the account's language (`apply`), save the
 * browser's into the account (`save`, with its time when it is carried), or
 * leave both alone.
 */
function planSignIn(
  accountId: string,
  identity: Identity,
  locale: string,
  choice: LocaleChoice,
): { apply: { locale: Locale; chosenAt: string | null } } | { save: string | null } | null {
  const kept = identity?.locale ?? null;
  const keptAt = identity?.locale_chosen_at ?? null;
  const mine = choice.chosenAt !== null && carriable(choice, accountId);
  const carry = mine && newer(choice.chosenAt, keptAt) && locale !== kept;
  if (kept && isLocale(kept) && !carry) return { apply: { locale: kept, chosenAt: keptAt } };
  // Nothing of this account's to apply: keep its language unset rather than
  // give it a choice another account on this browser made.
  if (choice.chosenAt !== null && !mine) return null;
  return { save: carry ? choice.chosenAt : null };
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
      saved.current = user.identity?.locale ?? null;
      const plan = planSignIn(user.id, user.identity, locale, readLocaleChoice());
      if (plan === null) return;
      if ('apply' in plan) {
        if (plan.apply.locale !== locale) setLocale(plan.apply.locale);
        // This browser now shows the account's own choice, made when it says.
        markLocaleChoice({ chosenAt: plan.apply.chosenAt, owner: user.id, stored: true });
        return;
      }
      chosenAt = plan.save;
    } else if (saved.current !== locale) {
      // A change made while signed in is this account's own choice.
      chosenAt = readLocaleChoice().chosenAt;
      markLocaleChoice({ owner: user.id, stored: false });
    }
    if (saved.current === locale) return;
    saved.current = locale;
    void (chosenAt ? saveAccountLocale(locale, chosenAt) : saveAccountLocale(locale))
      .then((answer) => {
        saved.current = answer.locale;
        if (answer.locale && isLocale(answer.locale) && answer.locale !== locale)
          setLocale(answer.locale);
        markLocaleChoice({ chosenAt: answer.chosen_at, owner: user.id, stored: true });
      })
      .catch(() => undefined);
  }, [user, locale, setLocale]);
  return null;
}

// @vitest-environment jsdom
// Review L1 (the reviewer's repro opus-locale.test.tsx): two accounts signed
// in on one browser keep their own languages. The browser records whose its
// choice is, as HybridInference's LocaleChoice {owner, stored}, and carries
// into an account only a choice made signed out or in that same account.
import { act, cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as self from '@/lib/api/accountSelf';
import { LocaleProvider, useLocale } from '@/components/providers/LocaleProvider';
import { AccountLocaleSync } from '@/components/providers/AccountLocaleSync';
import type { Locale } from '@/lib/i18n/locales';

const auth = vi.hoisted(() => ({ user: null as Record<string, unknown> | null }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: auth.user } }),
}));
vi.mock('@/lib/api/accountSelf', () => ({ saveAccountLocale: vi.fn() }));

let shown = '';
let choose: (locale: Locale) => void = () => undefined;
function Show() {
  const { locale, setLocale } = useLocale();
  shown = locale;
  choose = setLocale;
  return null;
}
const tree = () => (
  <LocaleProvider>
    <AccountLocaleSync />
    <Show />
  </LocaleProvider>
);
const A = {
  id: 'A',
  auth_mode: 'local',
  identity: { locale: 'zh-CN', locale_chosen_at: '2026-10-29T10:00:00.000Z' },
};
const B = {
  id: 'B',
  auth_mode: 'local',
  identity: { locale: 'en', locale_chosen_at: '2026-10-29T09:00:00.000Z' },
};

async function signIn(view: ReturnType<typeof render>, user: Record<string, unknown> | null) {
  auth.user = user;
  await act(async () => {
    view.rerender(tree());
  });
}

beforeEach(() => {
  vi.mocked(self.saveAccountLocale).mockImplementation(async (locale, chosenAt) => ({
    locale,
    chosen_at: chosenAt ?? '2026-10-29T12:00:00.000Z',
  }));
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  window.localStorage.clear();
  auth.user = null;
});

describe('two accounts on one browser', () => {
  it('B keeps its own older choice after A signed in here, and A keeps its own', async () => {
    auth.user = A;
    const view = render(tree());
    await waitFor(() => expect(shown).toBe('zh-CN'));
    await signIn(view, null);
    await signIn(view, B);
    await waitFor(() => expect(shown).toBe('en'));
    // Switching back and forth applies each account's language and saves nothing.
    await signIn(view, A);
    await waitFor(() => expect(shown).toBe('zh-CN'));
    expect(self.saveAccountLocale).not.toHaveBeenCalled();
  });

  it('carries a choice made in A into A only, never into B', async () => {
    auth.user = A;
    const view = render(tree());
    await waitFor(() => expect(shown).toBe('zh-CN'));
    act(() => choose('en'));
    await waitFor(() => expect(self.saveAccountLocale).toHaveBeenCalledTimes(1));
    expect(vi.mocked(self.saveAccountLocale).mock.calls[0][0]).toBe('en');
    await signIn(view, null);
    await signIn(view, {
      ...B,
      identity: { locale: 'zh-CN', locale_chosen_at: B.identity.locale_chosen_at },
    });
    await waitFor(() => expect(shown).toBe('zh-CN'));
    expect(self.saveAccountLocale).toHaveBeenCalledTimes(1);
  });

  it('does not give an account without a language the one another account chose', async () => {
    auth.user = A;
    const view = render(tree());
    await waitFor(() => expect(shown).toBe('zh-CN'));
    await signIn(view, null);
    await signIn(view, { id: 'C', auth_mode: 'local', identity: { locale: null } });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(self.saveAccountLocale).not.toHaveBeenCalled();
  });

  it('still carries a choice made signed out into the account that signs in next', async () => {
    const view = render(tree());
    act(() => choose('zh-CN'));
    await signIn(view, B);
    await waitFor(() => expect(self.saveAccountLocale).toHaveBeenCalledTimes(1));
    const [locale, chosenAt] = vi.mocked(self.saveAccountLocale).mock.calls[0];
    expect(locale).toBe('zh-CN');
    expect(Date.parse(chosenAt ?? '')).toBeGreaterThan(Date.parse(B.identity.locale_chosen_at));
  });
});

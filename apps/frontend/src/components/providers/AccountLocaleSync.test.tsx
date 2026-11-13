// @vitest-environment jsdom
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as self from '@/lib/api/accountSelf';
import { LOCALE_CHOSEN_AT_KEY } from '@/lib/i18n/locales';
import { AccountLocaleSync } from './AccountLocaleSync';

const state = vi.hoisted(() => ({
  user: { id: 'acct', identity: { locale: 'zh-CN' } } as Record<string, unknown> | null,
  locale: 'en',
  setLocale: vi.fn(),
}));
vi.mock('./AuthProvider', () => ({ useAuth: () => ({ state: { user: state.user } }) }));
vi.mock('./LocaleProvider', () => ({
  useLocale: () => ({ locale: state.locale, setLocale: state.setLocale }),
}));
vi.mock('@/lib/api/accountSelf', () => ({ saveAccountLocale: vi.fn() }));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  window.localStorage.clear();
});

describe('the language follows the account', () => {
  it('applies the account’s saved language when it signs in', () => {
    render(<AccountLocaleSync />);
    expect(state.setLocale).toHaveBeenCalledWith('zh-CN');
    expect(self.saveAccountLocale).not.toHaveBeenCalled();
  });
  it('saves the browser’s language for an account that has none', async () => {
    state.user = { id: 'other', identity: { locale: null } };
    vi.mocked(self.saveAccountLocale).mockResolvedValue({ locale: 'en', chosen_at: null });
    render(<AccountLocaleSync />);
    await waitFor(() => expect(self.saveAccountLocale).toHaveBeenCalledWith('en'));
  });
  it('carries a language chosen signed out, after the account’s, into the account', async () => {
    window.localStorage.setItem(LOCALE_CHOSEN_AT_KEY, '2026-10-29T10:00:05.000Z');
    state.user = {
      id: 'carried',
      identity: { locale: 'en', locale_chosen_at: '2026-10-29T10:00:00.000Z' },
    };
    state.locale = 'zh-CN';
    vi.mocked(self.saveAccountLocale).mockResolvedValue({
      locale: 'zh-CN',
      chosen_at: '2026-10-29T10:00:05.000Z',
    });
    render(<AccountLocaleSync />);
    await waitFor(() =>
      expect(self.saveAccountLocale).toHaveBeenCalledWith('zh-CN', '2026-10-29T10:00:05.000Z'),
    );
    expect(state.setLocale).not.toHaveBeenCalled();
  });
  it('takes the account’s language over an older choice on this browser', () => {
    window.localStorage.setItem(LOCALE_CHOSEN_AT_KEY, '2026-10-29T09:00:00.000Z');
    state.user = {
      id: 'older',
      identity: { locale: 'en', locale_chosen_at: '2026-10-29T10:00:00.000Z' },
    };
    state.locale = 'zh-CN';
    render(<AccountLocaleSync />);
    expect(state.setLocale).toHaveBeenCalledWith('en');
    expect(self.saveAccountLocale).not.toHaveBeenCalled();
    expect(window.localStorage.getItem(LOCALE_CHOSEN_AT_KEY)).toBe('2026-10-29T10:00:00.000Z');
  });
});

// @vitest-environment jsdom
import { cleanup, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import * as self from '@/lib/api/accountSelf';
import { AccountLocaleSync } from './AccountLocaleSync';

const state = vi.hoisted(() => ({
  user: { id: 'acct', auth_mode: 'local', identity: { locale: 'zh-CN' } } as Record<
    string,
    unknown
  > | null,
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
});

describe('the language follows the account', () => {
  it('applies the account’s saved language when it signs in', () => {
    render(<AccountLocaleSync />);
    expect(state.setLocale).toHaveBeenCalledWith('zh-CN');
    expect(self.saveAccountLocale).not.toHaveBeenCalled();
  });
  it('saves the browser’s language for an account that has none', async () => {
    state.user = { id: 'other', auth_mode: 'local', identity: { locale: null } };
    vi.mocked(self.saveAccountLocale).mockResolvedValue({ locale: 'en', chosen_at: null });
    render(<AccountLocaleSync />);
    await waitFor(() => expect(self.saveAccountLocale).toHaveBeenCalledWith('en'));
  });
});

// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// The login page is the account sign-in form, whatever the deployment: there
// is no other authority to send a visitor to, and an outage reading the sign-in
// settings says so instead of guessing.

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import * as api from '@/lib/api/accounts';

vi.mock('next/navigation', () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock('@/lib/auth/sessionSync', () => ({ announceSession: vi.fn() }));
vi.mock('@/lib/api/accounts', () => ({
  getAuthConfig: vi.fn(),
  getAuthOptions: vi.fn(() => new Promise(() => {})),
  listBrowserAccounts: vi.fn(() => new Promise(() => {})),
  loginAccount: vi.fn(),
}));

import LoginPage from '@/components/features/auth/LoginPageClient';

describe('the login page', () => {
  afterEach(() => {
    cleanup();
    vi.mocked(api.getAuthConfig).mockReset();
  });

  it('is the account sign-in form, with no other authority to go to', async () => {
    vi.mocked(api.getAuthConfig).mockResolvedValue({ signup_enabled: true });
    render(<LoginPage />);
    expect(await screen.findByLabelText('Password')).toBeInTheDocument();
    expect(
      screen
        .queryAllByRole('link')
        .some((link) => /session\/login|authorize/.test(link.getAttribute('href') ?? '')),
    ).toBe(false);
  });

  it('shows the form at once from the settings the server read for the first document', () => {
    vi.mocked(api.getAuthConfig).mockReturnValue(new Promise(() => {}));
    render(<LoginPage initialConfig={{ signup_enabled: false }} />);
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
  });

  it('says the sign-in settings could not be read and offers a retry', async () => {
    vi.mocked(api.getAuthConfig)
      .mockRejectedValueOnce(new Error('Cannot load sign-in options.'))
      .mockResolvedValueOnce({ signup_enabled: false });
    render(<LoginPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    expect(await screen.findByLabelText('Password')).toBeInTheDocument();
  });
});

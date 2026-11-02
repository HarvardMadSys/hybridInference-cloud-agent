// @vitest-environment jsdom
// Review F1/F4: with Turnstile configured, every phone code (sign-in, binding,
// the owner's own proof, sign-up) is asked for with a token, and each token is
// used once: after every request a fresh challenge replaces it. The widget is
// a stand-in for Cloudflare's script, which the page loads only in a browser.
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '@/lib/api/accounts';
import * as self from '@/lib/api/accountSelf';
import type { AccountIdentity, AuthOptions } from '@/lib/api/accounts';
import { AccountSecurity } from './AccountSecurity';
import { LocalLogin } from './LocalLogin';
import { SignupPage } from './SignupPage';

vi.mock('next/navigation', () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock('@/lib/auth/sessionSync', () => ({ announceSession: vi.fn() }));
vi.mock('@/lib/api/accounts', () => ({
  getAuthOptions: vi.fn(),
  signupAccount: vi.fn(),
  signupPhone: vi.fn(),
  sendPhoneCode: vi.fn(),
  loginAccount: vi.fn(),
  loginPhone: vi.fn(),
}));
vi.mock('@/lib/api/accountSelf', () => ({
  getOwnAccount: vi.fn(),
  changePassword: vi.fn(),
  requestEmailBind: vi.fn(),
  bindPhone: vi.fn(),
  deleteOwnAccount: vi.fn(),
}));

const OPTIONS: AuthOptions = {
  signup_enabled: true,
  phone_signup_enabled: true,
  email_delivery: true,
  require_email_verification: true,
  phone: { enabled: true, default_country_code: '86', allowed_prefixes: ['+86'] },
  turnstile_site_key: 'site-key',
  terms_url: '',
  terms_text: 'Be kind.',
  setup_required: false,
  max_accounts_per_browser: 5,
};
const PHONE_ONLY: AccountIdentity = {
  locale: null,
  email: null,
  email_verified: false,
  login_name: null,
  phone: '+8613800138000',
  has_password: false,
  identity_verified: true,
  user_name: null,
  label: '+8613800138000',
};

/** Every challenge rendered, newest last; `solve` answers the newest. */
const challenges: { callback: (token: string) => void; sitekey: string }[] = [];
function solve(token: string) {
  act(() => challenges[challenges.length - 1].callback(token));
}
function fill(label: string | RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

beforeEach(() => {
  vi.resetAllMocks();
  challenges.length = 0;
  vi.mocked(api.getAuthOptions).mockResolvedValue(OPTIONS);
  vi.mocked(api.sendPhoneCode).mockResolvedValue({ ok: true, retry_after: 0 });
  window.turnstile = {
    render: (_element, options) => {
      challenges.push(options as (typeof challenges)[number]);
      return `widget-${challenges.length}`;
    },
    remove: () => undefined,
  };
});
afterEach(() => {
  cleanup();
  delete window.turnstile;
});

describe('phone codes with Turnstile on', () => {
  it('signs in by phone only with a token, and a fresh one for every code', async () => {
    render(<LocalLogin signupEnabled />);
    fireEvent.click(await screen.findByRole('tab', { name: 'Phone code' }));
    fill('Phone number', '+8613800138000');
    await waitFor(() => expect(challenges).toHaveLength(1));
    expect(challenges[0].sitekey).toBe('site-key');
    const send = screen.getByRole('button', { name: 'Send code' });
    expect(send).toBeDisabled();
    solve('token-1');
    fireEvent.click(send);
    await waitFor(() =>
      expect(api.sendPhoneCode).toHaveBeenCalledWith('+8613800138000', 'login', 'token-1'),
    );
    // The spent token is gone: a new challenge, and no code until it is solved.
    await waitFor(() => expect(challenges).toHaveLength(2));
    const resend = screen.getByRole('button', { name: 'Send again' });
    expect(resend).toBeDisabled();
    solve('token-2');
    fireEvent.click(resend);
    await waitFor(() =>
      expect(api.sendPhoneCode).toHaveBeenLastCalledWith('+8613800138000', 'login', 'token-2'),
    );
  });

  it('proves a phone-only owner with a token, and binds a number with one', async () => {
    vi.mocked(self.getOwnAccount).mockResolvedValue(PHONE_ONLY);
    render(<AccountSecurity />);
    expect(await screen.findByText('Set a password')).toBeInTheDocument();
    // Each place that sends a code shows its own challenge.
    await waitFor(() => expect(challenges.length).toBeGreaterThanOrEqual(2));
    const [first] = screen.getAllByRole('button', { name: 'Send code' });
    expect(first).toBeDisabled();
    act(() => challenges[0].callback('owner-token'));
    fireEvent.click(first);
    await waitFor(() =>
      expect(api.sendPhoneCode).toHaveBeenCalledWith(
        '+8613800138000',
        'set_password',
        'owner-token',
      ),
    );
  });

  it('binds a number with a token', async () => {
    vi.mocked(self.getOwnAccount).mockResolvedValue({
      ...PHONE_ONLY,
      email: 'me@example.edu',
      email_verified: true,
      has_password: true,
      phone: null,
      label: 'me@example.edu',
    });
    render(<AccountSecurity />);
    await screen.findByLabelText('Phone number');
    fill('Phone number', '13800138000');
    await waitFor(() => expect(challenges).toHaveLength(1));
    solve('bind-token');
    fireEvent.click(screen.getByRole('button', { name: 'Send code' }));
    await waitFor(() =>
      expect(api.sendPhoneCode).toHaveBeenCalledWith('13800138000', 'bind', 'bind-token'),
    );
  });
});

describe('sign-up with Turnstile on', () => {
  it('retries an email sign-up with a fresh token after a refusal', async () => {
    vi.mocked(api.signupAccount)
      .mockRejectedValueOnce(new Error('The check failed. Try again.'))
      .mockResolvedValueOnce({ ok: true });
    render(<SignupPage />);
    fireEvent.click(await screen.findByRole('checkbox'));
    fill('How will you use Cloud Agent?', 'Thesis');
    fill('Email', 'ada@example.edu');
    fill('Password', 'Goodpass123');
    fill('Confirm password', 'Goodpass123');
    await waitFor(() => expect(challenges).toHaveLength(1));
    solve('signup-1');
    const form = () => screen.getByRole('button', { name: 'Create account' }).closest('form')!;
    fireEvent.submit(form());
    expect(await screen.findByText('The check failed. Try again.')).toBeInTheDocument();
    expect(vi.mocked(api.signupAccount).mock.calls[0][2]?.turnstile_token).toBe('signup-1');
    await waitFor(() => expect(challenges).toHaveLength(2));
    expect(screen.getByRole('button', { name: 'Create account' })).toBeDisabled();
    solve('signup-2');
    fireEvent.submit(form());
    await waitFor(() => expect(api.signupAccount).toHaveBeenCalledTimes(2));
    expect(vi.mocked(api.signupAccount).mock.calls[1][2]?.turnstile_token).toBe('signup-2');
  });

  it('keeps the phone form after a code, and asks for a new token to send again', async () => {
    render(<SignupPage />);
    fireEvent.click(await screen.findByRole('checkbox'));
    fireEvent.click(screen.getByRole('tab', { name: 'Phone number' }));
    fill('Phone number', '13800138000');
    await waitFor(() => expect(challenges).toHaveLength(1));
    expect(screen.getByRole('button', { name: 'Send code' })).toBeDisabled();
    solve('phone-1');
    fireEvent.click(screen.getByRole('button', { name: 'Send code' }));
    await waitFor(() =>
      expect(api.sendPhoneCode).toHaveBeenCalledWith('13800138000', 'signup', 'phone-1'),
    );
    await waitFor(() => expect(challenges).toHaveLength(2));
    expect(screen.getByLabelText('Phone number')).toHaveValue('13800138000');
    expect(screen.getByRole('button', { name: 'Send again' })).toBeDisabled();
  });
});

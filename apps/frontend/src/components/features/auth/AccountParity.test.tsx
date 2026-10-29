// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '@/lib/api/accounts';
import * as self from '@/lib/api/accountSelf';
import type { AccountIdentity, AuthOptions } from '@/lib/api/accounts';
import { SignupPage } from './SignupPage';
import { LocalLogin } from './LocalLogin';
import { BrowserAccounts } from './BrowserAccounts';
import { AccountSecurity } from './AccountSecurity';
import { FirstRunSetup } from './FirstRunSetup';

vi.mock('next/navigation', () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock('@/lib/auth/sessionSync', () => ({ announceSession: vi.fn() }));
vi.mock('@/lib/api/accounts', () => ({
  getAuthOptions: vi.fn(),
  signupAccount: vi.fn(),
  signupPhone: vi.fn(),
  sendPhoneCode: vi.fn(),
  loginAccount: vi.fn(),
  loginPhone: vi.fn(),
  listBrowserAccounts: vi.fn(),
  switchAccount: vi.fn(),
  removeBrowserAccount: vi.fn(),
  signOutEverywhereHere: vi.fn(),
  createSetupAdmin: vi.fn(),
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
  turnstile_site_key: '',
  terms_url: 'https://lab.example.edu/terms',
  terms_text: '',
  setup_required: false,
  max_accounts_per_browser: 5,
};
const IDENTITY: AccountIdentity = {
  locale: null,
  email: 'me@example.edu',
  email_verified: true,
  login_name: null,
  phone: null,
  has_password: true,
  identity_verified: true,
  user_name: 'Me',
  label: 'me@example.edu',
};
const assign = vi.fn();
function fill(label: string | RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.getAuthOptions).mockResolvedValue(OPTIONS);
  Object.defineProperty(window, 'location', { value: { assign }, writable: true });
});
afterEach(cleanup);

describe('sign-up', () => {
  it('asks for the Terms first and sends the name, use case and agreement with the account', async () => {
    vi.mocked(api.signupAccount).mockResolvedValue({ ok: true });
    render(<SignupPage />);
    const button = await screen.findByRole('button', { name: 'Create account' });
    expect(button).toBeDisabled();
    expect(screen.getByRole('link', { name: 'Terms of Service' })).toHaveAttribute(
      'href',
      'https://lab.example.edu/terms',
    );
    fill('Name', 'Ada Lovelace');
    fill('How will you use Cloud Agent?', 'Thesis experiments');
    fireEvent.click(screen.getByRole('checkbox'));
    fill('Email', 'ada@example.edu');
    fill('Password', 'Goodpass123');
    fill('Confirm password', 'Goodpass123');
    fireEvent.submit(screen.getByRole('button', { name: 'Create account' }).closest('form')!);
    await waitFor(() =>
      expect(api.signupAccount).toHaveBeenCalledWith('ada@example.edu', 'Goodpass123', {
        user_name: 'Ada Lovelace',
        use_case: 'Thesis experiments',
        accepted_tos: true,
        turnstile_token: undefined,
      }),
    );
  });
  it('signs up with a phone code when the deployment offers it', async () => {
    vi.mocked(api.sendPhoneCode).mockResolvedValue({ ok: true, retry_after: 60 });
    vi.mocked(api.signupPhone).mockResolvedValue({ ok: true, requires_approval: true });
    render(<SignupPage />);
    fireEvent.click(await screen.findByRole('checkbox'));
    fireEvent.click(screen.getByRole('tab', { name: 'Phone number' }));
    fill('Phone number', '13800138000');
    fireEvent.click(screen.getByRole('button', { name: 'Send code' }));
    await waitFor(() =>
      expect(api.sendPhoneCode).toHaveBeenCalledWith('13800138000', 'signup', undefined),
    );
    expect(await screen.findByRole('button', { name: 'Send again in 60s' })).toBeDisabled();
    fill('Verification code', '123456');
    fireEvent.submit(screen.getByRole('button', { name: 'Create account' }).closest('form')!);
    expect(await screen.findByText(/waits for an administrator/)).toBeInTheDocument();
  });
  it('says sign-up is closed when neither method is open', async () => {
    vi.mocked(api.getAuthOptions).mockResolvedValue({
      ...OPTIONS,
      signup_enabled: false,
      phone_signup_enabled: false,
    });
    render(<SignupPage />);
    expect(await screen.findByText('Account registration unavailable')).toBeInTheDocument();
  });
});

describe('sign-in', () => {
  it('signs in with a phone code and points to first-run setup when it is pending', async () => {
    vi.mocked(api.getAuthOptions).mockResolvedValue({ ...OPTIONS, setup_required: true });
    vi.mocked(api.loginPhone).mockResolvedValue({
      id: 'acct',
      agent_access: { allowed: true, permissions: ['agent.use'] },
    } as never);
    render(<LocalLogin signupEnabled />);
    expect(
      await screen.findByRole('link', { name: 'Create the first administrator' }),
    ).toHaveAttribute('href', '/setup/administrator');
    fireEvent.click(screen.getByRole('tab', { name: 'Phone code' }));
    fill('Phone number', '+8613800138000');
    fill('Verification code', '654321');
    fireEvent.submit(screen.getByRole('button', { name: 'Sign in' }).closest('form')!);
    await waitFor(() => expect(api.loginPhone).toHaveBeenCalledWith('+8613800138000', '654321'));
    expect(assign).toHaveBeenCalledWith('/');
  });
});

describe('the accounts on this browser', () => {
  it('switches to a signed-in account and marks the one that is signed out', async () => {
    vi.mocked(api.listBrowserAccounts).mockResolvedValue({
      active_account_id: 'a',
      max_accounts: 5,
      accounts: [
        {
          id: 'a',
          label: 'a@example.edu',
          user_name: null,
          active: true,
          signed_in: true,
          agent_access: true,
        },
        {
          id: 'b',
          label: 'b@example.edu',
          user_name: null,
          active: false,
          signed_in: true,
          agent_access: false,
        },
        {
          id: 'c',
          label: 'c@example.edu',
          user_name: null,
          active: false,
          signed_in: false,
          agent_access: null,
        },
      ],
    });
    vi.mocked(api.switchAccount).mockResolvedValue({ id: 'b' } as never);
    render(<BrowserAccounts />);
    expect(await screen.findByText('Signed in · no Agent access')).toBeInTheDocument();
    expect(screen.getByText('Signed out')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Switch' }));
    await waitFor(() => expect(api.switchAccount).toHaveBeenCalledWith('b'));
    expect(assign).toHaveBeenCalledWith('/');
  });
});

describe('account security', () => {
  it('changes the password with the current one', async () => {
    vi.mocked(self.getOwnAccount).mockResolvedValue(IDENTITY);
    vi.mocked(self.changePassword).mockResolvedValue({ ok: true });
    render(<AccountSecurity />);
    await screen.findByText('Change password');
    // Every section asks for the current password; the first is the password form's.
    fireEvent.change(screen.getAllByLabelText('Current password')[0], {
      target: { value: 'Oldpass123' },
    });
    fill('Password', 'Newpass123');
    fill('Confirm password', 'Newpass123');
    fireEvent.click(screen.getByRole('button', { name: 'Save password' }));
    await waitFor(() =>
      expect(self.changePassword).toHaveBeenCalledWith({
        old_password: 'Oldpass123',
        phone_code: undefined,
        new_password: 'Newpass123',
      }),
    );
  });
  it('confirms a phone-only account with a code to its own number', async () => {
    vi.mocked(self.getOwnAccount).mockResolvedValue({
      ...IDENTITY,
      email: null,
      email_verified: false,
      has_password: false,
      phone: '+8613800138000',
      label: '+8613800138000',
    });
    render(<AccountSecurity />);
    expect(await screen.findByText('Set a password')).toBeInTheDocument();
    expect(
      screen.getAllByText(/A code will be sent to your verified number/).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByLabelText('Current password')).toBeNull();
  });
  it('deletes only once the identifier is typed, and never for an administrator', async () => {
    vi.mocked(self.getOwnAccount).mockResolvedValue(IDENTITY);
    vi.mocked(self.deleteOwnAccount).mockResolvedValue({ ok: true });
    const { unmount } = render(<AccountSecurity />);
    const button = await screen.findByRole('button', { name: 'Delete my account' });
    expect(button).toBeDisabled();
    fill('Type me@example.edu to confirm', 'ME@example.edu');
    const proofs = screen.getAllByLabelText('Current password');
    fireEvent.change(proofs[proofs.length - 1], { target: { value: 'Oldpass123' } });
    fireEvent.click(screen.getByRole('button', { name: 'Delete my account' }));
    await waitFor(() =>
      expect(self.deleteOwnAccount).toHaveBeenCalledWith('ME@example.edu', {
        password: 'Oldpass123',
      }),
    );
    unmount();
    render(<AccountSecurity isAdmin />);
    expect(await screen.findByText(/cannot delete itself/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete my account' })).toBeNull();
  });
});

describe('first-run setup', () => {
  it('creates the administrator with the setup code and opens administration', async () => {
    vi.mocked(api.getAuthOptions).mockResolvedValue({ ...OPTIONS, setup_required: true });
    vi.mocked(api.createSetupAdmin).mockResolvedValue({ id: 'root' } as never);
    render(<FirstRunSetup />);
    fill('Setup code', 'ABCD-EFGH-JKLM');
    fill('Username', 'root');
    fill('Password', 'Goodpass123');
    fill('Confirm password', 'Goodpass123');
    fireEvent.submit(screen.getByRole('button', { name: 'Create administrator' }).closest('form')!);
    await waitFor(() =>
      expect(api.createSetupAdmin).toHaveBeenCalledWith({
        setup_code: 'ABCD-EFGH-JKLM',
        login_name: 'root',
        password: 'Goodpass123',
        display_name: undefined,
      }),
    );
    expect(assign).toHaveBeenCalledWith('/admin/accounts');
  });
});

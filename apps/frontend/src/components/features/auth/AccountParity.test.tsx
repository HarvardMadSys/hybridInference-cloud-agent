// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '@/lib/api/accounts';
import * as self from '@/lib/api/accountSelf';
import * as adminSettings from '@/lib/api/adminSettings';
import type { AgentAdminSettings } from '@/lib/api/adminSettings';
import { APIError } from '@/lib/utils/errors';
import type { AccountIdentity, AuthOptions } from '@/lib/api/accounts';
import { SignupPage } from './SignupPage';
import { LocalLogin } from './LocalLogin';
import { BrowserAccounts } from './BrowserAccounts';
import { AccountSecurity } from './AccountSecurity';
import { FirstRunSetup } from './FirstRunSetup';

vi.mock('next/navigation', () => ({ useSearchParams: () => new URLSearchParams() }));
const sync = vi.hoisted(() => ({
  handlers: [] as ((message: { userId: string | null }) => void)[],
}));
vi.mock('@/lib/auth/sessionSync', () => ({
  announceSession: vi.fn(),
  listenForSession: (handler: (message: { userId: string | null }) => void) => {
    sync.handlers.push(handler);
    return () => undefined;
  },
}));
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
vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: vi.fn(),
  setAgentAdminSettings: vi.fn(),
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
const steps = () => screen.getAllByRole('listitem').map((item) => item.textContent);
const currentStep = () =>
  screen.getAllByRole('listitem').find((item) => item.getAttribute('aria-current') === 'step')
    ?.textContent;
function fill(label: string | RegExp, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}
/** Agree in the Terms dialog sign-up opens by itself. */
async function agreeToTerms() {
  fireEvent.click(await screen.findByRole('button', { name: 'Agree' }));
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.getAuthOptions).mockResolvedValue(OPTIONS);
  Object.defineProperty(window, 'location', {
    value: { assign, origin: 'https://agent.example.edu' },
    writable: true,
  });
});
afterEach(cleanup);

describe('sign-up', () => {
  it('asks for the Terms first and sends the name, use case and agreement with the account', async () => {
    vi.mocked(api.signupAccount).mockResolvedValue({ ok: true });
    render(<SignupPage />);
    const button = await screen.findByRole('button', { name: 'Create account' });
    expect(button).toBeDisabled();
    // The deployment published only a link: it opens in a new tab, never here.
    const dialog = await screen.findByRole('dialog', { name: 'Terms of Service' });
    const link = within(dialog).getByRole('link', { name: 'Open the Terms of Service' });
    expect(link).toHaveAttribute('href', 'https://lab.example.edu/terms');
    expect(link).toHaveAttribute('target', '_blank');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Close' }));
    fill('Name', 'Ada Lovelace');
    fill('How will you use Cloud Agent?', 'Thesis experiments');
    expect(button).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Read and agree' }));
    await agreeToTerms();
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
    fireEvent.click(await screen.findByRole('tab', { name: 'Phone number' }));
    // Phone fields stay visible before the Terms are agreed to.
    expect(screen.getByLabelText('Phone number')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Create account' })).toBeDisabled();
    await agreeToTerms();
    fill('Phone number', '13800138000');
    fireEvent.click(screen.getByRole('button', { name: 'Send code' }));
    await waitFor(() =>
      expect(api.sendPhoneCode).toHaveBeenCalledWith('13800138000', 'signup', undefined),
    );
    expect(await screen.findByRole('button', { name: 'Send again in 60s' })).toBeDisabled();
    fill('Verification code', '123456');
    fireEvent.submit(screen.getByRole('button', { name: 'Create account' }).closest('form')!);
    expect(await screen.findByText(/waits for an administrator/)).toBeInTheDocument();
    // The code proved the number: no email to check, and the account is made.
    expect(steps()).toEqual(['Account', 'Get access']);
    expect(currentStep()).toBe('Get access');
  });
  it('offers no phone sign-up while only phone sign-in is enabled', async () => {
    vi.mocked(api.getAuthOptions).mockResolvedValue({ ...OPTIONS, phone_signup_enabled: false });
    render(<SignupPage />);
    expect(await screen.findByLabelText('Email')).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Phone number' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Phone number')).not.toBeInTheDocument();
  });
  it('shows the email check among the steps only for an email sign-up that needs one', async () => {
    vi.mocked(api.signupAccount).mockResolvedValue({ ok: true });
    render(<SignupPage />);
    await agreeToTerms();
    expect(steps()).toEqual(['Account', 'Verify email', 'Get access']);
    expect(currentStep()).toBe('Account');
    fireEvent.click(screen.getByRole('tab', { name: 'Phone number' }));
    expect(steps()).toEqual(['Account', 'Get access']);
    fireEvent.click(screen.getByRole('tab', { name: 'Email' }));
    fill('Email', 'ada@example.edu');
    fill('Password', 'Goodpass123');
    fill('Confirm password', 'Goodpass123');
    fireEvent.submit(screen.getByRole('button', { name: 'Create account' }).closest('form')!);
    await waitFor(() => expect(currentStep()).toBe('Verify email'));
  });
  it('leaves the email check out when only phone sign-up is open', async () => {
    vi.mocked(api.getAuthOptions).mockResolvedValue({ ...OPTIONS, signup_enabled: false });
    render(<SignupPage />);
    await screen.findByRole('button', { name: 'Read and agree' });
    expect(steps()).toEqual(['Account', 'Get access']);
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
  it('reloads the list when another tab changes the accounts (review F10)', async () => {
    sync.handlers.length = 0;
    vi.mocked(api.listBrowserAccounts).mockResolvedValue({
      active_account_id: 'a',
      max_accounts: 5,
      accounts: [],
    });
    render(<BrowserAccounts />);
    await waitFor(() => expect(api.listBrowserAccounts).toHaveBeenCalledTimes(1));
    expect(sync.handlers).toHaveLength(1);
    sync.handlers[0]({ userId: 'b' });
    await waitFor(() => expect(api.listBrowserAccounts).toHaveBeenCalledTimes(2));
  });
});

describe('account security', () => {
  const open = async (name: string) => fireEvent.click(await screen.findByRole('button', { name }));
  it('changes the password with the current one', async () => {
    vi.mocked(self.getOwnAccount).mockResolvedValue(IDENTITY);
    vi.mocked(self.changePassword).mockResolvedValue({ ok: true });
    render(<AccountSecurity />);
    // Every form stays closed until its row asks for it.
    expect(await screen.findByText('me@example.edu', { selector: 'span' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Current password')).toBeNull();
    await open('Change password');
    fill('Current password', 'Oldpass123');
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
    // The form closes on success; its notice stays on the row.
    expect(await screen.findByText(/Password saved/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Current password')).toBeNull();
    expect(screen.getByRole('button', { name: 'Change password' })).toHaveFocus();
  });
  it('keeps a refused change open with its error', async () => {
    vi.mocked(self.getOwnAccount).mockResolvedValue(IDENTITY);
    vi.mocked(self.requestEmailBind).mockRejectedValue(new Error('Wrong password.'));
    render(<AccountSecurity />);
    await open('Change email');
    fill('New email address', 'new@example.edu');
    fill('Current password', 'Badpass123');
    fireEvent.submit(
      screen.getByRole('button', { name: 'Send confirmation link' }).closest('form')!,
    );
    expect(await screen.findByRole('alert')).toHaveTextContent('Wrong password.');
    expect(screen.getByLabelText('New email address')).toHaveValue('new@example.edu');
  });
  it('discards what was typed on Cancel or Esc, but not while a change is pending', async () => {
    vi.mocked(self.getOwnAccount).mockResolvedValue(IDENTITY);
    let answer: (value: { ok: boolean }) => void = () => undefined;
    vi.mocked(self.changePassword).mockReturnValue(new Promise((resolve) => (answer = resolve)));
    render(<AccountSecurity />);
    await open('Change password');
    expect(screen.getByLabelText('Current password')).toHaveFocus();
    fill('Current password', 'Oldpass123');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByLabelText('Current password')).toBeNull();
    expect(screen.getByRole('button', { name: 'Change password' })).toHaveFocus();
    await open('Change password');
    expect(screen.getByLabelText('Current password')).toHaveValue('');
    fill('Current password', 'Oldpass123');
    fireEvent.keyDown(screen.getByLabelText('Current password'), { key: 'Escape' });
    expect(screen.queryByLabelText('Current password')).toBeNull();
    await open('Change password');
    expect(screen.getByLabelText('Current password')).toHaveValue('');
    fill('Current password', 'Oldpass123');
    fill('Password', 'Newpass123');
    fill('Confirm password', 'Newpass123');
    fireEvent.click(screen.getByRole('button', { name: 'Save password' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled());
    expect(screen.getByRole('button', { name: 'Save password' })).toBeDisabled();
    fireEvent.keyDown(screen.getByLabelText('Current password'), { key: 'Escape' });
    expect(screen.getByLabelText('Current password')).toBeInTheDocument();
    answer({ ok: true });
    expect(await screen.findByText(/Password saved/)).toBeInTheDocument();
  });
  it('shows the sign-in details without a link to the browser’s accounts', async () => {
    vi.mocked(self.getOwnAccount).mockResolvedValue({
      ...IDENTITY,
      login_name: 'ada',
      email_verified: false,
      user_name: 'Ada',
    });
    render(<AccountSecurity />);
    expect(await screen.findByText('ada')).toBeInTheDocument();
    expect(screen.getByText('Not verified')).toBeInTheDocument();
    expect(screen.getByText('Not added')).toBeInTheDocument();
    expect(screen.queryByRole('link')).toBeNull();
    expect(screen.queryByText('Accounts on this browser')).toBeNull();
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
    await open('Set a password');
    expect(screen.getByText(/A code will be sent to your verified number/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Current password')).toBeNull();
  });
  it('deletes only once the identifier is typed, and never for an administrator', async () => {
    vi.mocked(self.getOwnAccount).mockResolvedValue(IDENTITY);
    vi.mocked(self.deleteOwnAccount).mockResolvedValue({ ok: true });
    const { unmount } = render(<AccountSecurity />);
    await open('Delete account');
    const button = screen.getByRole('button', { name: 'Delete my account' });
    expect(button).toBeDisabled();
    fill('Type me@example.edu to confirm', 'ME@example.edu');
    fill('Current password', 'Oldpass123');
    fireEvent.click(screen.getByRole('button', { name: 'Delete my account' }));
    await waitFor(() =>
      expect(self.deleteOwnAccount).toHaveBeenCalledWith('ME@example.edu', {
        password: 'Oldpass123',
      }),
    );
    unmount();
    render(<AccountSecurity isAdmin />);
    expect(await screen.findByText(/cannot delete itself/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete account' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Delete my account' })).toBeNull();
  });
});

function settingsSnapshot(webBaseUrl: string | null, revision = 3): AgentAdminSettings {
  return {
    revision,
    definitions: [],
    values: { web_base_url: webBaseUrl },
    secret_configured: {},
    bootstrap: [],
  };
}
/** Complete step 1 and wait for the site-address step. */
async function createAdministrator() {
  vi.mocked(api.getAuthOptions).mockResolvedValue({ ...OPTIONS, setup_required: true });
  vi.mocked(api.createSetupAdmin).mockResolvedValue({ id: 'root' } as never);
  render(<FirstRunSetup />);
  expect(currentStep()).toBe('Administrator');
  fill('Setup code', 'ABCD-EFGH-JKLM');
  fill('Username', 'root');
  fill('Password', 'Goodpass123');
  fill('Confirm password', 'Goodpass123');
  fireEvent.submit(screen.getByRole('button', { name: 'Create administrator' }).closest('form')!);
  await screen.findByRole('heading', { name: 'Confirm the site address' });
}
const siteField = () => screen.getByLabelText('Web app URL') as HTMLInputElement;
const saveButton = () => screen.getByRole('button', { name: 'Save and continue' });
/** The save button once the saved settings have been read and it is offered. */
const readySaveButton = () => screen.findByRole('button', { name: 'Save and continue' });

describe('first-run setup', () => {
  it('creates the administrator with the setup code, then asks for the site address', async () => {
    vi.mocked(adminSettings.getAgentAdminSettings).mockResolvedValue(settingsSnapshot(null));
    await createAdministrator();
    expect(api.createSetupAdmin).toHaveBeenCalledWith({
      setup_code: 'ABCD-EFGH-JKLM',
      login_name: 'root',
      password: 'Goodpass123',
      display_name: undefined,
    });
    expect(steps()).toEqual(['Administrator', 'Site address', 'First server']);
    expect(currentStep()).toBe('Site address');
    expect(assign).not.toHaveBeenCalled();
  });
  it('prefills an unset address with this page’s origin and saves it at the read revision', async () => {
    vi.mocked(adminSettings.getAgentAdminSettings).mockResolvedValue(settingsSnapshot('', 7));
    vi.mocked(adminSettings.setAgentAdminSettings).mockResolvedValue(settingsSnapshot('x', 8));
    await createAdministrator();
    await waitFor(() => expect(adminSettings.getAgentAdminSettings).toHaveBeenCalled());
    expect(siteField().value).toBe('https://agent.example.edu');
    expect(screen.queryByText(/already saved/)).toBeNull();
    fill('Web app URL', ' https://lab.example.edu ');
    fireEvent.click(await readySaveButton());
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/admin/hosts'));
    expect(adminSettings.setAgentAdminSettings).toHaveBeenCalledWith(
      { web_base_url: 'https://lab.example.edu' },
      7,
    );
  });
  it('shows an address already saved, and continues without rewriting it', async () => {
    vi.mocked(adminSettings.getAgentAdminSettings).mockResolvedValue(
      settingsSnapshot('https://saved.example.edu'),
    );
    await createAdministrator();
    await waitFor(() => expect(siteField().value).toBe('https://saved.example.edu'));
    expect(screen.getByText(/This address is already saved/)).toBeInTheDocument();
    fireEvent.click(await readySaveButton());
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/admin/hosts'));
    expect(adminSettings.setAgentAdminSettings).not.toHaveBeenCalled();
  });
  it('shows why the API refused the address and stays on the step', async () => {
    vi.mocked(adminSettings.getAgentAdminSettings).mockResolvedValue(settingsSnapshot(null));
    vi.mocked(adminSettings.setAgentAdminSettings).mockRejectedValue(
      // How `jsonOrThrow` reports the API's 422 with a plain `detail`.
      new APIError('UNKNOWN_ERROR', 'web_base_url must be an http(s) URL', 422),
    );
    await createAdministrator();
    await waitFor(() => expect(adminSettings.getAgentAdminSettings).toHaveBeenCalled());
    fireEvent.click(await readySaveButton());
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'web_base_url must be an http(s) URL',
    );
    expect(assign).not.toHaveBeenCalled();
    expect(saveButton()).toBeEnabled();
  });
  it('can skip the address and still go on to add a server', async () => {
    vi.mocked(adminSettings.getAgentAdminSettings).mockResolvedValue(settingsSnapshot(null));
    await createAdministrator();
    fireEvent.click(screen.getByRole('button', { name: 'Skip for now' }));
    expect(assign).toHaveBeenCalledWith('/admin/hosts');
    expect(adminSettings.setAgentAdminSettings).not.toHaveBeenCalled();
  });
  it('cannot save before the saved settings are read, so a slow read keeps a saved address', async () => {
    let answer: (snapshot: ReturnType<typeof settingsSnapshot>) => void = () => {};
    vi.mocked(adminSettings.getAgentAdminSettings).mockReturnValue(
      new Promise((resolve) => {
        answer = resolve;
      }),
    );
    await createAdministrator();
    const waiting = screen.getByRole('button', { name: 'Reading the saved settings…' });
    expect(waiting).toBeDisabled();
    fireEvent.click(waiting);
    expect(adminSettings.setAgentAdminSettings).not.toHaveBeenCalled();
    answer(settingsSnapshot('https://saved.example.edu', 3));
    await waitFor(() => expect(siteField().value).toBe('https://saved.example.edu'));
    fireEvent.click(await readySaveButton());
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/admin/hosts'));
    expect(adminSettings.setAgentAdminSettings).not.toHaveBeenCalled();
  });
  it('offers to read the settings again when the first read failed, and saves at that revision', async () => {
    vi.mocked(adminSettings.getAgentAdminSettings)
      .mockRejectedValueOnce(new Error('Settings cannot be loaded.'))
      .mockResolvedValueOnce(settingsSnapshot(null, 4));
    vi.mocked(adminSettings.setAgentAdminSettings).mockResolvedValue(settingsSnapshot('x', 5));
    await createAdministrator();
    expect(await screen.findByRole('alert')).toHaveTextContent('Settings cannot be loaded.');
    expect(screen.getByRole('button', { name: 'Reading the saved settings…' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Read the saved settings again' }));
    fireEvent.click(await readySaveButton());
    await waitFor(() => expect(assign).toHaveBeenCalledWith('/admin/hosts'));
    expect(adminSettings.setAgentAdminSettings).toHaveBeenCalledWith(
      { web_base_url: 'https://agent.example.edu' },
      4,
    );
    expect(adminSettings.getAgentAdminSettings).toHaveBeenCalledTimes(2);
  });
});

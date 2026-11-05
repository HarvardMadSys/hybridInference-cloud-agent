// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentAdminSettings, setAgentAdminSettings } from '@/lib/api/adminSettings';
import { chooseOption } from '@/components/ui/__tests__/select';

import ApplicationSettings from './ApplicationSettings';
import { accountProviderSnapshot } from './settingsTestFixtures';

vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: vi.fn(),
  setAgentAdminSettings: vi.fn(),
}));

type Values = Record<string, string | number | boolean>;

const read = vi.mocked(getAgentAdminSettings);
const write = vi.mocked(setAgentAdminSettings);

const SMTP = ['accounts.smtp_host', 'accounts.smtp_password', 'accounts.smtp_from'];
const SES = [
  'accounts.tencent_ses_region',
  'accounts.tencent_ses_from_email',
  'accounts.tencent_ses_template_verification',
];
const SMS = ['accounts.sms_provider', 'accounts.sms_code_ttl_s'];
const TENCENT_SMS = ['accounts.tencent_sms_app_id', 'accounts.tencent_sms_template_id'];
const CREDENTIALS = ['accounts.tencent_secret_id', 'accounts.tencent_secret_key'];
const ALWAYS = [
  'accounts.signup_enabled',
  'accounts.email_provider',
  'accounts.sms_enabled',
  'accounts.turnstile_site_key',
];
const ALL = [...ALWAYS, ...SMTP, ...SES, ...SMS, ...TENCENT_SMS, ...CREDENTIALS];

/** The field, its read-only value, or its editor, rendered for a setting key. */
function field(key: string): HTMLElement | null {
  return document.getElementById(`application-setting-${key}`);
}

function shownKeys(): string[] {
  return ALL.filter((key) => field(key) !== null);
}

function group(title: string): HTMLElement {
  return screen.getByRole('form', { name: title });
}

function edit(title: string) {
  fireEvent.click(screen.getByRole('button', { name: `Edit ${title} settings` }));
}

function categoryCount(category: string): string | null {
  const navigation = screen.getByRole('navigation', { name: 'Application setting categories' });
  return within(navigation)
    .getByRole('button', { name: category })
    .querySelector('[aria-hidden="true"]')!.textContent;
}

async function renderWith(values: Values) {
  read.mockResolvedValue(accountProviderSnapshot(values));
  render(<ApplicationSettings />);
  await screen.findByRole('heading', { name: 'Account email' });
}

beforeEach(() => {
  window.history.replaceState(null, '', window.location.pathname);
  read.mockReset();
  write.mockReset();
});

afterEach(() => {
  cleanup();
  window.history.replaceState(null, '', window.location.pathname);
});

describe('provider-aware application settings', () => {
  it.each<{ case: string; values: Values; shown: string[] }>([
    { case: 'SMTP mail with phone sign-in off', values: {}, shown: SMTP },
    {
      case: 'Tencent SES mail',
      values: { 'accounts.email_provider': 'tencent_ses' },
      shown: [...SES, ...CREDENTIALS],
    },
    {
      case: 'a registered mail provider',
      values: { 'accounts.email_provider': 'acme' },
      shown: [],
    },
    {
      case: 'phone sign-in on without Tencent',
      values: { 'accounts.sms_enabled': true, 'accounts.sms_provider': 'none' },
      shown: [...SMTP, ...SMS],
    },
    {
      case: 'phone sign-in on with Tencent',
      values: { 'accounts.sms_enabled': true, 'accounts.sms_provider': 'tencent' },
      shown: [...SMTP, ...SMS, ...TENCENT_SMS, ...CREDENTIALS],
    },
    {
      case: 'phone sign-in off with saved Tencent values',
      values: {
        'accounts.sms_provider': 'tencent',
        'accounts.tencent_sms_app_id': '1400000000',
      },
      shown: SMTP,
    },
  ])('renders and counts only the fields that apply with $case', async ({ values, shown }) => {
    await renderWith(values);
    const expected = [...ALWAYS, ...shown];
    expect(shownKeys().sort()).toEqual(expected.sort());
    expect(categoryCount('Identity & access')).toBe(String(expected.length));
    const credentials = screen.queryByRole('heading', { name: 'Tencent Cloud credentials' });
    if (shown.includes(CREDENTIALS[0])) expect(credentials).toBeVisible();
    else expect(credentials).not.toBeInTheDocument();
    // Captcha and sign-up are not tied to a provider and always stay.
    expect(screen.getByRole('heading', { name: 'Captcha' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Sign-up and approval' })).toBeVisible();
  });

  it('switches the mail provider in the editor and submits only the selected provider', async () => {
    await renderWith({ 'accounts.smtp_host': 'smtp.example.org' });
    edit('Account email');
    fireEvent.change(field('accounts.smtp_host')!, { target: { value: 'draft.example.org' } });
    fireEvent.change(field('accounts.email_provider')!, { target: { value: 'tencent_ses' } });

    expect(field('accounts.smtp_host')).toBeNull();
    expect(field('accounts.tencent_ses_from_email')).toBeInTheDocument();
    // The shared credentials appear for the provider being chosen, before it is saved.
    expect(screen.getByRole('heading', { name: 'Tencent Cloud credentials' })).toBeVisible();
    expect(categoryCount('Identity & access')).toBe(String(ALWAYS.length + 5));
    // The hidden SMTP draft is neither counted nor submitted.
    expect(within(group('Account email')).getByText('1 unsaved change')).toBeInTheDocument();

    fireEvent.change(field('accounts.tencent_ses_from_email')!, {
      target: { value: 'noreply@example.org' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Review Account email changes' }));
    const dialog = screen.getByRole('dialog', { name: 'Review changes' });
    expect(within(dialog).queryByText('SMTP host')).not.toBeInTheDocument();
    expect(within(dialog).getByText('2 changes')).toBeInTheDocument();

    write.mockResolvedValue(
      accountProviderSnapshot(
        {
          'accounts.smtp_host': 'smtp.example.org',
          'accounts.email_provider': 'tencent_ses',
          'accounts.tencent_ses_from_email': 'noreply@example.org',
        },
        { revision: 2 },
      ),
    );
    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply changes' }));
    await screen.findByText('Account email settings saved.');
    expect(write).toHaveBeenCalledTimes(1);
    expect(write.mock.calls[0].slice(0, 2)).toEqual([
      {
        'accounts.email_provider': 'tencent_ses',
        'accounts.tencent_ses_from_email': 'noreply@example.org',
      },
      1,
    ]);
    expect(field('accounts.smtp_host')).toBeNull();
    expect(screen.getByRole('heading', { name: 'Tencent Cloud credentials' })).toBeVisible();
  });

  it('brings back a hidden draft when switching back and drops the provider draft on cancel', async () => {
    await renderWith({ 'accounts.smtp_host': 'smtp.example.org' });
    edit('Account email');
    fireEvent.change(field('accounts.smtp_host')!, { target: { value: 'draft.example.org' } });
    const provider = field('accounts.email_provider')!;
    fireEvent.change(provider, { target: { value: 'tencent_ses' } });
    fireEvent.change(provider, { target: { value: 'smtp' } });

    expect(field('accounts.smtp_host')).toHaveValue('draft.example.org');
    expect(field('accounts.tencent_ses_region')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Tencent Cloud credentials' })).toBeNull();
    expect(within(group('Account email')).getByText('1 unsaved change')).toBeInTheDocument();

    fireEvent.change(provider, { target: { value: 'tencent_ses' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel editing Account email' }));
    expect(field('accounts.smtp_host')).toHaveTextContent('smtp.example.org');
    expect(field('accounts.email_provider')).toHaveTextContent('smtp');
    expect(screen.queryByRole('heading', { name: 'Tencent Cloud credentials' })).toBeNull();
    expect(write).not.toHaveBeenCalled();
  });

  it('keeps an unsent credential draft hidden while no consumer needs it', async () => {
    await renderWith({});
    edit('Account email');
    const provider = field('accounts.email_provider')!;
    fireEvent.change(provider, { target: { value: 'tencent_ses' } });
    edit('Tencent Cloud credentials');
    fireEvent.click(screen.getByRole('button', { name: 'Set Tencent Cloud SecretId' }));
    fireEvent.change(field('accounts.tencent_secret_id')!, { target: { value: 'AKID-draft' } });

    fireEvent.change(provider, { target: { value: 'smtp' } });
    expect(screen.queryByRole('heading', { name: 'Tencent Cloud credentials' })).toBeNull();
    expect(field('accounts.tencent_secret_id')).toBeNull();
    // Nothing on the page can submit it, and leaving does not warn about it.
    expect(
      screen.queryByRole('button', { name: 'Review Tencent Cloud credentials changes' }),
    ).toBeNull();
    const leave = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(leave);
    expect(leave.defaultPrevented).toBe(false);

    fireEvent.change(provider, { target: { value: 'tencent_ses' } });
    expect(field('accounts.tencent_secret_id')).toHaveValue('AKID-draft');
    expect(
      within(group('Tencent Cloud credentials')).getByText('1 unsaved change'),
    ).toBeInTheDocument();
    expect(write).not.toHaveBeenCalled();
  });

  it('enables phone sign-in and configures Tencent text messages from one editor', async () => {
    await renderWith({ 'accounts.tencent_sms_app_id': 'stale' });
    expect(shownKeys().filter((key) => key.includes('sms'))).toEqual(['accounts.sms_enabled']);

    edit('Phone sign-in');
    chooseOption(field('accounts.sms_enabled')!, 'Enabled');
    expect(field('accounts.sms_provider')).toBeInTheDocument();
    expect(field('accounts.sms_code_ttl_s')).toBeInTheDocument();
    expect(field('accounts.tencent_sms_app_id')).toBeNull();
    expect(screen.queryByRole('heading', { name: 'Tencent Cloud credentials' })).toBeNull();

    chooseOption(field('accounts.sms_provider')!, 'tencent');
    // The saved application ID survives being hidden and is shown again unchanged.
    expect(field('accounts.tencent_sms_app_id')).toHaveValue('stale');
    expect(screen.getByRole('heading', { name: 'Tencent Cloud credentials' })).toBeVisible();
    fireEvent.change(field('accounts.tencent_sms_template_id')!, { target: { value: '2100' } });

    write.mockResolvedValue(
      accountProviderSnapshot(
        {
          'accounts.sms_enabled': true,
          'accounts.sms_provider': 'tencent',
          'accounts.tencent_sms_app_id': 'stale',
          'accounts.tencent_sms_template_id': '2100',
        },
        { revision: 2 },
      ),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Review Phone sign-in changes' }));
    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Apply changes' }),
    );
    await screen.findByText('Phone sign-in settings saved.');
    expect(write.mock.calls[0][0]).toEqual({
      'accounts.sms_enabled': true,
      'accounts.sms_provider': 'tencent',
      'accounts.tencent_sms_template_id': '2100',
    });
    expect(field('accounts.tencent_sms_template_id')).toHaveTextContent('2100');
    expect(screen.getByRole('heading', { name: 'Tencent Cloud credentials' })).toBeVisible();
  });

  it('hides phone sign-in details again when the switch is turned off in the editor', async () => {
    await renderWith({ 'accounts.sms_enabled': true, 'accounts.sms_provider': 'tencent' });
    edit('Phone sign-in');
    fireEvent.change(field('accounts.sms_code_ttl_s')!, { target: { value: '120' } });
    chooseOption(field('accounts.sms_enabled')!, 'Disabled');
    expect(shownKeys().filter((key) => key.includes('sms'))).toEqual(['accounts.sms_enabled']);
    expect(screen.queryByRole('heading', { name: 'Tencent Cloud credentials' })).toBeNull();

    write.mockResolvedValue(accountProviderSnapshot({ 'accounts.sms_provider': 'tencent' }));
    fireEvent.click(screen.getByRole('button', { name: 'Review Phone sign-in changes' }));
    fireEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Apply changes' }),
    );
    await screen.findByText('Phone sign-in settings saved.');
    // Turning the feature off writes the switch alone; its provider stays saved.
    expect(write.mock.calls[0][0]).toEqual({ 'accounts.sms_enabled': false });
  });

  it.each<{ case: string; values: Values; link: string; opens: string }>([
    {
      case: 'an SMTP field while Tencent SES is selected',
      values: { 'accounts.email_provider': 'tencent_ses' },
      link: 'accounts.smtp_host',
      opens: 'accounts.email_provider',
    },
    {
      case: 'a shared credential no enabled feature uses',
      values: { 'accounts.sms_provider': 'tencent' },
      link: 'accounts.tencent_secret_key',
      opens: 'accounts.email_provider',
    },
    {
      case: 'a Tencent text message field while phone sign-in is off',
      values: {},
      link: 'accounts.tencent_sms_template_id',
      opens: 'accounts.sms_enabled',
    },
    {
      case: 'a Tencent text message field while another provider is selected',
      values: { 'accounts.sms_enabled': true },
      link: 'accounts.tencent_sms_app_id',
      opens: 'accounts.sms_provider',
    },
  ])('opens the governing setting for a link to $case', async ({ values, link, opens }) => {
    window.history.replaceState(null, '', `#application-setting-${encodeURIComponent(link)}`);
    await renderWith(values);
    await waitFor(() => expect(field(opens)).toHaveFocus());
    expect(field(link)).toBeNull();
  });
});

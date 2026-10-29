// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as accounts from '@/lib/api/accounts';
import * as admin from '@/lib/api/accountAdmin';
import type { AccountEntry } from '@/lib/api/accounts';
import { ActionEditor, actionsFor } from './AccountActions';
import { AccountNotes } from './AccountDetails';
import AccountPolicyPage from './policy/page';
import { MailComposer } from './mail/MailComposer';

const auth = vi.hoisted(() => ({ state: { user: { id: 'admin', auth_mode: 'local' } } }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => auth,
  useSignedInAuthMode: () => 'local',
}));
vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: async () => ({ setup: { required: false, groups: [] } }),
}));
vi.mock('@/lib/api/accounts', () => ({ updateAccount: vi.fn(), listAccounts: vi.fn() }));
vi.mock('@/lib/api/accountAdmin', () => ({
  eraseAccount: vi.fn(),
  saveAccountNotes: vi.fn(),
  listLoginEvents: vi.fn(),
  purgeLoginEvents: vi.fn(),
  listSignupDomains: vi.fn(),
  addSignupDomain: vi.fn(),
  removeSignupDomain: vi.fn(),
  previewMail: vi.fn(),
  testMail: vi.fn(),
  createMail: vi.fn(),
}));

const base: AccountEntry = {
  id: '01ARZ3NDEKTSV4RRFFQ69G5FAV',
  email: 'user@example.edu',
  email_verified: true,
  status: 'active',
  is_admin: false,
  waitlist: {
    status: 'approved',
    purpose: '',
    review_reason: null,
    version: 7,
    submitted_at: null,
    reviewed_at: null,
  },
  created_at: '2026-10-29T00:00:00Z',
};
beforeEach(() => vi.resetAllMocks());
afterEach(cleanup);

describe('account decisions', () => {
  it('offers deletion of live accounts and resumption of deleted ones, nothing once erased', () => {
    expect(actionsFor(base)).toEqual(['revoke', 'grant_admin', 'suspend', 'delete']);
    expect(actionsFor({ ...base, status: 'suspended' })).toEqual(['restore', 'delete']);
    expect(actionsFor({ ...base, status: 'deleted' })).toEqual(['resume']);
    expect(actionsFor({ ...base, status: 'deleted', erased_at: '2026-10-29T01:00:00Z' })).toEqual(
      [],
    );
    const login = {
      ...base,
      email: null,
      email_verified: false,
      waitlist: { ...base.waitlist, status: 'pending' as const },
      identity: {
        locale: null,
        email: null,
        email_verified: false,
        login_name: 'ops',
        phone: null,
        has_password: true,
        identity_verified: true,
        user_name: null,
        label: 'ops',
      },
    };
    // A login-name account is verified by its identity, not by an email.
    expect(actionsFor(login)[0]).toBe('approve');
  });
  it('erases a deleted account only with its id typed and reports retained archives', async () => {
    vi.mocked(admin.eraseAccount).mockResolvedValue({
      account: { ...base, status: 'deleted', erased_at: 'now' },
      deleted: { agent_jobs: 3, agent_threads: 1 },
      retained_archives: ['sandbox-archives/x'],
    });
    const done = vi.fn();
    render(
      <ActionEditor account={{ ...base, status: 'deleted' }} onDone={done} onCancel={vi.fn()} />,
    );
    const erase = screen.getByRole('button', { name: 'Erase account' });
    expect(erase).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Type the account id to confirm'), {
      target: { value: base.id },
    });
    fireEvent.click(erase);
    expect(await screen.findByText(/Erased 4 rows/)).toHaveTextContent('1 sandbox archive');
    expect(admin.eraseAccount).toHaveBeenCalledWith(base.id, base.id);
    expect(done).toHaveBeenCalled();
  });
  it('deletes at the version the administrator read', async () => {
    vi.mocked(accounts.updateAccount).mockResolvedValue(base);
    render(
      <ActionEditor
        account={{ ...base, status: 'suspended' }}
        onDone={vi.fn()}
        onCancel={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('combobox', { name: 'Action' }));
    fireEvent.click(screen.getByRole('option', { name: 'Delete account' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete account' }));
    await waitFor(() =>
      expect(accounts.updateAccount).toHaveBeenCalledWith(
        { ...base, status: 'suspended' },
        'delete',
        '',
      ),
    );
  });
  it('saves the private note and the suspension message', async () => {
    vi.mocked(admin.saveAccountNotes).mockResolvedValue(base);
    const saved = vi.fn();
    render(<AccountNotes account={base} onSaved={saved} />);
    fireEvent.change(screen.getByLabelText(/Administrator note/), { target: { value: 'VIP' } });
    fireEvent.change(screen.getByLabelText(/suspended account signs in/), {
      target: { value: 'Contact the lab' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save notes' }));
    await waitFor(() =>
      expect(admin.saveAccountNotes).toHaveBeenCalledWith(base, {
        admin_note: 'VIP',
        suspension_message: 'Contact the lab',
      }),
    );
    expect(saved).toHaveBeenCalled();
  });
});

describe('sign-up policy', () => {
  it('lists the allowlist, explains the policy and adds a wildcard', async () => {
    vi.mocked(admin.listSignupDomains).mockResolvedValue({
      domains: [],
      approval_policy: 'domains',
    });
    vi.mocked(admin.addSignupDomain).mockResolvedValue({
      domains: [{ domain: 'lab.edu', is_wildcard: true, created_at: 'now', created_by: 'admin' }],
      approval_policy: 'domains',
    });
    render(<AccountPolicyPage />);
    expect(await screen.findByText(/list is empty: every verified account/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Domain to add'), { target: { value: '*.lab.edu' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add domain' }));
    expect(await screen.findByText('*.lab.edu')).toBeInTheDocument();
    expect(admin.addSignupDomain).toHaveBeenCalledWith('*.lab.edu');
  });
  it('purges sign-in records older than a number of days', async () => {
    vi.mocked(admin.listSignupDomains).mockResolvedValue({
      domains: [],
      approval_policy: 'manual',
    });
    vi.mocked(admin.purgeLoginEvents).mockResolvedValue({ deleted: 12 });
    render(<AccountPolicyPage />);
    fireEvent.change(screen.getByLabelText('Older than (days)'), { target: { value: '30' } });
    fireEvent.click(screen.getByRole('button', { name: 'Purge' }));
    expect(await screen.findByText('Deleted 12 sign-in records.')).toBeInTheDocument();
    expect(admin.purgeLoginEvents).toHaveBeenCalledWith({ olderThanDays: 30 });
  });
});

describe('mail to accounts', () => {
  it('sends only after a preview, to the chosen states', async () => {
    vi.mocked(admin.previewMail).mockResolvedValue({
      recipient_count: 4,
      subject: 'Upgrade',
      html: '<p>Hi</p>',
      text: 'Hi',
    });
    vi.mocked(admin.createMail).mockResolvedValue({ recipient_count: 4 } as never);
    const sent = vi.fn();
    render(<MailComposer onSent={sent} />);
    fireEvent.change(screen.getByLabelText('Subject'), { target: { value: 'Upgrade' } });
    fireEvent.change(screen.getByLabelText('Body (Markdown)'), { target: { value: '**Hi**' } });
    fireEvent.click(screen.getByRole('checkbox', { name: 'pending' }));
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    expect(await screen.findByText(/4 recipients/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await waitFor(() =>
      expect(admin.createMail).toHaveBeenCalledWith(
        {
          template_key: null,
          template_vars: {},
          subject: 'Upgrade',
          body_markdown: '**Hi**',
          target_statuses: ['active'],
          target_admissions: ['approved', 'pending'],
        },
        null,
      ),
    );
    expect(await screen.findByText('Queued for 4 accounts.')).toBeInTheDocument();
    expect(sent).toHaveBeenCalled();
  });
});

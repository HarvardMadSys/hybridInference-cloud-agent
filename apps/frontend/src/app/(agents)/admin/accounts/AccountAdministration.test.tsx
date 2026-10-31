// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as accounts from '@/lib/api/accounts';
import * as admin from '@/lib/api/accountAdmin';
import type { AccountEntry } from '@/lib/api/accounts';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { ActionEditor, actionsFor } from './AccountActions';
import { AccountNotes } from './AccountDetails';
import AccountPolicyPage from './policy/page';
import { MailComposer } from './mail/MailComposer';

const auth = vi.hoisted(() => ({ state: { user: { id: 'admin' } } }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => auth,
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
  listOwnedTeams: vi.fn(),
  transferOwnedTeam: vi.fn(),
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
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(admin.listOwnedTeams).mockResolvedValue({ teams: [] });
});
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
    // The id the administrator must type is shown, to copy.
    expect(screen.getByText(base.id)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Type the account id to confirm'), {
      target: { value: base.id },
    });
    fireEvent.click(erase);
    expect(await screen.findByText(/Erased 4 rows/)).toHaveTextContent('1 sandbox archive');
    expect(admin.eraseAccount).toHaveBeenCalledWith(base.id, base.id);
    expect(done).toHaveBeenCalled();
  });
  it('hands a shared team to a member, and lists the obstruction erasure reports', async () => {
    const team = {
      id: 'team-lab',
      name: 'Lab',
      revision: 2,
      members: [{ user_id: 'member-1', role: 'admin', label: 'member@example.edu' }],
    };
    vi.mocked(admin.listOwnedTeams).mockResolvedValue({ teams: [team] });
    vi.mocked(admin.transferOwnedTeam).mockResolvedValue({ teams: [] });
    vi.mocked(admin.eraseAccount).mockRejectedValue(
      new Error('The account owns teams other members still use: Lab (team-lab).'),
    );
    render(
      <ActionEditor account={{ ...base, status: 'deleted' }} onDone={vi.fn()} onCancel={vi.fn()} />,
    );
    expect(await screen.findByText('Teams it owns')).toBeInTheDocument();
    expect(screen.getByRole('combobox', { name: 'New owner' })).toHaveTextContent(
      'member@example.edu (team admin)',
    );
    fireEvent.change(screen.getByLabelText('Type the account id to confirm'), {
      target: { value: base.id },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Erase account' }));
    expect(await screen.findByText(/other members still use: Lab/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Hand over' }));
    await waitFor(() =>
      expect(admin.transferOwnedTeam).toHaveBeenCalledWith(base.id, 'team-lab', 'member-1'),
    );
    await waitFor(() => expect(screen.queryByText('Teams it owns')).not.toBeInTheDocument());
  });
  it('reports teams deleted and environments kept by an erasure', async () => {
    vi.mocked(admin.eraseAccount).mockResolvedValue({
      account: { ...base, status: 'deleted', erased_at: 'now' },
      deleted: { agent_jobs: 1 },
      retained_archives: [],
      deleted_teams: ['team-alone'],
      handed_environments: ['env-1', 'env-2'],
    });
    render(
      <ActionEditor account={{ ...base, status: 'deleted' }} onDone={vi.fn()} onCancel={vi.fn()} />,
    );
    fireEvent.change(screen.getByLabelText('Type the account id to confirm'), {
      target: { value: base.id },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Erase account' }));
    const result = await screen.findByText(/Erased 1 rows/);
    expect(result).toHaveTextContent('1 team(s) it owned alone were deleted.');
    expect(result).toHaveTextContent('2 team environment(s) it made stay with their teams.');
  });
  it('explains that the administrator list undoes withdrawing a listed account', () => {
    render(
      <ActionEditor account={{ ...base, is_admin: true }} onDone={vi.fn()} onCancel={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole('combobox', { name: 'Action' }));
    fireEvent.click(screen.getByRole('option', { name: 'Remove administrator' }));
    expect(screen.getByText(/Administrator email addresses .* this is refused/)).toBeVisible();
  });
  it('follows the administrator’s language (review F11)', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    try {
      render(
        <LocaleProvider>
          <ActionEditor account={base} onDone={vi.fn()} onCancel={vi.fn()} />
        </LocaleProvider>,
      );
      expect(await screen.findByRole('button', { name: '撤销权限' })).toBeInTheDocument();
      expect(screen.getByText(/停止该账号的 Agent 工作/)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: '关闭' })).toBeInTheDocument();
    } finally {
      window.localStorage.clear();
    }
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
    // Irreversible, so asked once more with its scope (review F6).
    expect(screen.getByText(/older than 30 days\? This cannot be undone/)).toBeInTheDocument();
    expect(admin.purgeLoginEvents).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Delete them' }));
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
    // Sending is confirmed with the count the preview showed.
    expect(screen.getByText('Send this message to 4 accounts now?')).toBeInTheDocument();
    expect(admin.createMail).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm send' }));
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
  it('shows why the mail transport would refuse a message and does not send it', async () => {
    vi.mocked(admin.previewMail).mockResolvedValue({
      recipient_count: 4,
      subject: 'Long',
      html: '<p>Hi</p>',
      text: 'Hi',
      delivery_problem: 'Tencent SES … limited to 800 bytes; this one needs 1210.',
    });
    render(<MailComposer onSent={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('Subject'), { target: { value: 'Long' } });
    fireEvent.change(screen.getByLabelText('Body (Markdown)'), { target: { value: 'x' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    expect(await screen.findByText(/limited to 800 bytes/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    expect(admin.createMail).not.toHaveBeenCalled();
  });
  it('asks for a new preview once the message or its audience changes (review R2)', async () => {
    vi.mocked(admin.previewMail).mockResolvedValue({
      recipient_count: 4,
      subject: 'S',
      html: '<p>x</p>',
      text: 'x',
    });
    render(<MailComposer onSent={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('Subject'), { target: { value: 'S' } });
    fireEvent.change(screen.getByLabelText('Body (Markdown)'), { target: { value: 'x' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    expect(await screen.findByText(/4 recipients/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Send' })).toBeEnabled();
    // Widen the audience and rewrite the body after the preview.
    fireEvent.click(screen.getByRole('checkbox', { name: 'pending' }));
    fireEvent.change(screen.getByLabelText('Body (Markdown)'), {
      target: { value: 'unreviewed text' },
    });
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled();
    expect(screen.getByText(/changed since the preview/)).toBeInTheDocument();
    expect(screen.queryByText(/4 recipients/)).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    await waitFor(() => expect(admin.previewMail).toHaveBeenCalledTimes(2));
    expect(vi.mocked(admin.previewMail).mock.calls[1][0]).toMatchObject({
      body_markdown: 'unreviewed text',
      target_admissions: ['approved', 'pending'],
    });
    expect(await screen.findByRole('button', { name: 'Send' })).toBeEnabled();
    expect(admin.createMail).not.toHaveBeenCalled();
  });
  it('offers no deleted accounts as an audience', () => {
    render(<MailComposer onSent={vi.fn()} />);
    expect(screen.getByRole('checkbox', { name: 'active' })).toBeInTheDocument();
    expect(screen.queryByRole('checkbox', { name: 'deleted' })).not.toBeInTheDocument();
  });
});

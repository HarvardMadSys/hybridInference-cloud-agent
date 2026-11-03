// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '@/lib/api/accounts';
import type { AccountEntry } from '@/lib/api/accounts';
import { APIError } from '@/lib/utils/errors';
import AccountsPage from './page';
const auth = vi.hoisted(() => ({
  state: { user: { id: 'admin' } },
  refreshUser: vi.fn(),
}));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => auth,
}));
vi.mock('@/lib/api/accounts', () => ({ listAccounts: vi.fn(), updateAccount: vi.fn() }));
// The filters live in the address bar; the page writes them with replaceState.
vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(window.location.search),
}));
vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: async () => ({ setup: { required: false, groups: [] } }),
}));
const account: AccountEntry = {
  id: 'new-user',
  email: 'user@example.test',
  email_verified: true,
  status: 'active',
  is_admin: false,
  waitlist: {
    status: 'pending',
    purpose: 'Research project',
    review_reason: null,
    version: 4,
    submitted_at: '2026-10-28T00:00:00Z',
    reviewed_at: null,
  },
  created_at: '2026-10-28T00:00:00Z',
};
beforeEach(() => {
  vi.resetAllMocks();
  window.history.replaceState(null, '', '/admin/users');
  vi.mocked(api.listAccounts).mockResolvedValue({ accounts: [account], total: 1 });
  auth.refreshUser.mockResolvedValue(undefined);
});
afterEach(cleanup);
describe('independent account review', () => {
  it('shows application context and sends the exact account version on approval', async () => {
    vi.mocked(api.updateAccount).mockResolvedValue({
      ...account,
      waitlist: { ...account.waitlist, status: 'approved', version: 5 },
    });
    render(<AccountsPage />);
    await screen.findByText('Research project');
    fireEvent.click(screen.getByRole('button', { name: 'Review' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve access' }));
    await waitFor(() => expect(api.updateAccount).toHaveBeenCalledWith(account, 'approve', ''));
    expect(api.listAccounts).toHaveBeenCalledTimes(2);
    expect(auth.refreshUser).not.toHaveBeenCalled();
  });
  it('a conflict locks the stale action until the operator refreshes', async () => {
    vi.mocked(api.updateAccount).mockRejectedValue(
      new APIError('version_conflict', 'Changed', 409),
    );
    render(<AccountsPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Review' }));
    fireEvent.click(screen.getByRole('button', { name: 'Approve access' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('changed while you were reviewing');
    expect(screen.getByRole('button', { name: 'Approve access' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Approve access' }));
    expect(api.updateAccount).toHaveBeenCalledTimes(1);
  });
  it('decline reason is explicitly identified as visible to the applicant', async () => {
    vi.mocked(api.updateAccount).mockResolvedValue(account);
    render(<AccountsPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Review' }));
    fireEvent.click(screen.getByRole('combobox', { name: 'Action' }));
    fireEvent.click(screen.getByRole('option', { name: 'Decline application' }));
    fireEvent.change(screen.getByLabelText('Reason shown to the applicant (optional)'), {
      target: { value: 'Please add context' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Decline application' }));
    await waitFor(() =>
      expect(api.updateAccount).toHaveBeenCalledWith(account, 'reject', 'Please add context'),
    );
  });
  it('never offers approving an unverified account', async () => {
    vi.mocked(api.listAccounts).mockResolvedValue({
      accounts: [{ ...account, email_verified: false }],
      total: 1,
    });
    render(<AccountsPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Review' }));
    fireEvent.click(screen.getByRole('combobox', { name: 'Action' }));
    expect(within(screen.getByRole('listbox')).queryByText('Approve access')).toBeNull();
  });
});
describe('the people table', () => {
  const lead: AccountEntry = {
    ...account,
    id: 'lead',
    email: 'lead@example.test',
    waitlist: { ...account.waitlist, status: 'approved', purpose: '' },
    teams: [
      { id: 'team-lab', name: 'Lab', role: 'owner' },
      { id: 'team-side', name: 'Side project', role: 'member' },
    ],
  };
  it('lists everyone by default with the teams each person is in', async () => {
    vi.mocked(api.listAccounts).mockResolvedValue({ accounts: [lead], total: 1 });
    render(<AccountsPage />);
    const lab = await screen.findByRole('link', { name: 'Lab (owner)' });
    expect(lab).toHaveAttribute('href', '/admin/users/teams/team-lab');
    expect(screen.getByRole('link', { name: 'Side project' })).toHaveAttribute(
      'href',
      '/admin/users/teams/team-side',
    );
    expect(api.listAccounts).toHaveBeenCalledWith('', 0, expect.any(AbortSignal), {
      accountStatus: '',
      q: '',
      sort: 'created',
      team: '',
    });
    expect(screen.getByRole('link', { name: 'Users' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: 'People' })).toHaveAttribute('aria-current', 'page');
  });
  it('a count shows the accounts it counts and keeps the view in the address bar', async () => {
    vi.mocked(api.listAccounts).mockResolvedValue({
      accounts: [account],
      total: 1,
      counts: { all: 3, 'admission:pending': 1, 'status:suspended': 2 },
    });
    render(<AccountsPage />);
    const counts = await screen.findByRole('group', { name: 'Account counts' });
    fireEvent.click(within(counts).getByRole('button', { name: /Waiting/ }));
    await waitFor(() =>
      expect(api.listAccounts).toHaveBeenLastCalledWith(
        'pending',
        0,
        expect.any(AbortSignal),
        expect.objectContaining({ accountStatus: '' }),
      ),
    );
    expect(window.location.search).toBe('?status=pending');
    expect(within(counts).getByRole('button', { name: /Waiting/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    fireEvent.click(within(counts).getByRole('button', { name: /Suspended/ }));
    await waitFor(() => expect(window.location.search).toBe('?state=suspended'));
    expect(api.listAccounts).toHaveBeenLastCalledWith(
      '',
      0,
      expect.any(AbortSignal),
      expect.objectContaining({ accountStatus: 'suspended' }),
    );
  });
  it('opens a team link on its members and shows everyone again on request', async () => {
    window.history.replaceState(null, '', '/admin/users?team=team-lab&q=lead');
    vi.mocked(api.listAccounts).mockResolvedValue({ accounts: [lead], total: 1 });
    render(<AccountsPage />);
    expect(await screen.findByText('Members of Lab')).toBeInTheDocument();
    expect(screen.getByRole('searchbox', { name: 'Search accounts' })).toHaveValue('lead');
    expect(api.listAccounts).toHaveBeenCalledWith('', 0, expect.any(AbortSignal), {
      accountStatus: '',
      q: 'lead',
      sort: 'created',
      team: 'team-lab',
    });
    fireEvent.click(screen.getByRole('button', { name: 'Show everyone' }));
    await waitFor(() => expect(window.location.search).toBe('?q=lead'));
    expect(screen.queryByText('Members of Lab')).not.toBeInTheDocument();
  });
  it('writes a search to the address bar once typing pauses', async () => {
    vi.mocked(api.listAccounts).mockResolvedValue({ accounts: [account], total: 1 });
    render(<AccountsPage />);
    fireEvent.change(await screen.findByRole('searchbox', { name: 'Search accounts' }), {
      target: { value: ' user@ ' },
    });
    expect(window.location.search).toBe('');
    await waitFor(() => expect(window.location.search).toBe('?q=user%40'));
    await waitFor(() =>
      expect(api.listAccounts).toHaveBeenLastCalledWith(
        '',
        0,
        expect.any(AbortSignal),
        expect.objectContaining({ q: 'user@' }),
      ),
    );
  });
});

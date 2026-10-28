// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '@/lib/api/accounts';
import type { AccountEntry } from '@/lib/api/accounts';
import { APIError } from '@/lib/utils/errors';
import AccountsPage from './page';
const auth = vi.hoisted(() => ({
  state: { user: { id: 'admin', auth_mode: 'local' } },
  refreshUser: vi.fn(),
}));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => auth,
  useSignedInAuthMode: () => auth.state.user.auth_mode,
}));
vi.mock('@/lib/api/accounts', () => ({ listAccounts: vi.fn(), updateAccount: vi.fn() }));
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
  auth.state.user.auth_mode = 'local';
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
  it('Gateway deployments do not query local account records', () => {
    auth.state.user.auth_mode = 'gateway';
    render(<AccountsPage />);
    expect(screen.getByText(/Independent accounts are not enabled/)).toBeInTheDocument();
    expect(api.listAccounts).not.toHaveBeenCalled();
    expect(screen.queryByRole('link', { name: 'Accounts' })).toBeNull();
  });
});

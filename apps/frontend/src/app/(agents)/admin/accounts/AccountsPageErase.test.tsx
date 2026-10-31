// @vitest-environment jsdom
// Review F3 (the reviewer's repro R1): on the real accounts page an erasure
// refreshes the list and closes the editor, and its result (rows erased, the
// sandbox archive keys left in object storage) must still be on the page.
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as accounts from '@/lib/api/accounts';
import * as admin from '@/lib/api/accountAdmin';
import type { AccountEntry } from '@/lib/api/accounts';
import AccountsPage from './page';

const auth = vi.hoisted(() => ({
  state: { user: { id: 'admin' } },
  refreshUser: async () => undefined,
}));
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
  listOwnedTeams: vi.fn(),
  transferOwnedTeam: vi.fn(),
}));

const deleted: AccountEntry = {
  id: '01ARZ3NDEKTSV4RRFFQ69G5FAV',
  email: 'gone@example.edu',
  email_verified: true,
  status: 'deleted',
  is_admin: false,
  waitlist: {
    status: 'revoked',
    purpose: '',
    review_reason: null,
    version: 9,
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

describe('erasing from the accounts page', () => {
  it('keeps what was erased and the retained archive keys after the list refreshes', async () => {
    vi.mocked(accounts.listAccounts).mockResolvedValue({
      accounts: [deleted],
      total: 1,
      counts: {},
    });
    vi.mocked(admin.eraseAccount).mockResolvedValue({
      account: { ...deleted, erased_at: 'now' },
      deleted: { agent_jobs: 3 },
      retained_archives: ['sandbox-archives/x/archive.tar.zst'],
      deleted_teams: [],
      handed_environments: [],
    });
    render(<AccountsPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Review' }));
    fireEvent.change(screen.getByLabelText('Type the account id to confirm'), {
      target: { value: deleted.id },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Erase account' }));
    await waitFor(() => expect(accounts.listAccounts).toHaveBeenCalledTimes(2));
    // The editor closed with the refresh; the result is the page's.
    expect(screen.queryByLabelText('Type the account id to confirm')).toBeNull();
    const result = await screen.findByText(/Erased 3 rows/);
    expect(result).toHaveTextContent('1 sandbox archive(s) remain');
    const archives = screen.getByRole('list', { name: 'Retained archives' });
    expect(within(archives).getByText('sandbox-archives/x/archive.tar.zst')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByText(/Erased 3 rows/)).toBeNull();
  });
});

// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  createInvitation,
  createTeam,
  decideJoinRequest,
  leaveTeam,
  listInvitations,
  listJoinRequests,
  listMembers,
  listOwnJoinRequests,
  listTeams,
  renameTeam,
  setTeamMemberRole,
  type Team,
  type TeamMember,
} from '@/lib/api/teams';
import { APIError } from '@/lib/utils/errors';
import { TeamsSettingsView } from './TeamsSettingsView';

vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { loading: false, isAuthenticated: true, user: { id: 'alice' } } }),
}));
vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useSiteConfig: () => ({ branding: { docsUrl: '' } }),
}));
vi.mock('@/lib/api/teams', async (original) => ({
  ...(await original<typeof import('@/lib/api/teams')>()),
  listTeams: vi.fn(),
  listOwnJoinRequests: vi.fn(),
  createTeam: vi.fn(),
  listMembers: vi.fn(),
  listInvitations: vi.fn(),
  listJoinRequests: vi.fn(),
  createInvitation: vi.fn(),
  decideJoinRequest: vi.fn(),
  leaveTeam: vi.fn(),
  renameTeam: vi.fn(),
  setTeamMemberRole: vi.fn(),
}));

const owned: Team = {
  id: 'ateam_1',
  name: 'Platform',
  owner_user_id: 'alice',
  role: 'owner',
  revision: 1,
  member_count: 1,
  created_at: '2026-10-15T00:00:00Z',
};
const alice: TeamMember = {
  user_id: 'alice',
  display_name: 'Alice',
  email: 'alice@example.com',
  avatar_data_url: 'data:image/svg+xml,<svg/>',
  role: 'owner',
  joined_at: '2026-10-15T00:00:00Z',
  you: true,
};

function view() {
  return render(
    <LocaleProvider>
      <TeamsSettingsView />
    </LocaleProvider>,
  );
}

describe('TeamsSettingsView', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(listTeams).mockResolvedValue([owned]);
    vi.mocked(listOwnJoinRequests).mockResolvedValue([]);
    vi.mocked(listMembers).mockResolvedValue([alice]);
    vi.mocked(listInvitations).mockResolvedValue([]);
    vi.mocked(listJoinRequests).mockResolvedValue([]);
  });
  afterEach(cleanup);

  it('shows a new invitation link once, with the token only in its fragment', async () => {
    vi.mocked(createInvitation).mockResolvedValue({
      invitation: {
        id: 'atinv_1',
        status: 'open',
        created_by_user_id: 'alice',
        created_at: '2026-10-15T00:00:00Z',
        expires_at: '2026-10-16T00:00:00Z',
        claimed_by_user_id: null,
        request_state: null,
      },
      token: 'K'.repeat(43),
    });
    view();
    fireEvent.click(await screen.findByRole('button', { name: 'Create invitation link' }));
    const link = await screen.findByLabelText('New invitation link');
    expect(link).toHaveValue(`http://localhost:3000/teams/join#token=${'K'.repeat(43)}`);
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(screen.queryByLabelText('New invitation link')).not.toBeInTheDocument();
  });

  it('asks the owner to revoke and recreate when a replayed link cannot be shown', async () => {
    vi.mocked(createInvitation).mockResolvedValue({
      invitation: {
        id: 'atinv_1',
        status: 'open',
        created_by_user_id: 'alice',
        created_at: '2026-10-15T00:00:00Z',
        expires_at: '2026-10-16T00:00:00Z',
        claimed_by_user_id: null,
        request_state: null,
      },
      token: null,
    });
    view();
    fireEvent.click(await screen.findByRole('button', { name: 'Create invitation link' }));
    expect(await screen.findByText(/cannot be shown again/)).toBeVisible();
  });

  it('lets the owner approve a request and refreshes the roster', async () => {
    vi.mocked(listJoinRequests).mockResolvedValueOnce([
      {
        id: 'atjr_1',
        user_id: 'bob',
        display_name: '',
        email: 'bob@example.com',
        avatar_data_url: 'data:image/svg+xml,<svg/>',
        state: 'pending',
        created_at: '2026-10-15T00:00:00Z',
        expires_at: '2026-10-16T00:00:00Z',
      },
    ]);
    vi.mocked(decideJoinRequest).mockResolvedValue({} as never);
    view();
    const requests = await screen.findByRole('region', { name: 'Join requests' });
    expect(within(requests).getByText('bob@example.com')).toBeVisible();
    fireEvent.click(within(requests).getByRole('button', { name: 'Approve' }));
    await waitFor(() =>
      expect(decideJoinRequest).toHaveBeenCalledExactlyOnceWith('ateam_1', 'atjr_1', true),
    );
    await waitFor(() => expect(listMembers).toHaveBeenCalledTimes(2));
  });

  it('shows a member Leave, not Delete, and explains a refusal', async () => {
    vi.mocked(listTeams).mockResolvedValue([{ ...owned, role: 'member', owner_user_id: 'carol' }]);
    vi.mocked(leaveTeam).mockRejectedValue(new APIError('not_found', 'No such team.', 404));
    view();
    expect(await screen.findByRole('button', { name: 'Leave team' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Delete team' })).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Create invitation link' }),
    ).not.toBeInTheDocument();
    expect(listInvitations).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Leave team' }));
    const confirm = await screen.findByRole('group', { name: 'Leave Platform?' });
    fireEvent.click(within(confirm).getByRole('button', { name: 'Leave team' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/no longer exists/);
  });

  it('creates a team once even when the response is retried', async () => {
    vi.mocked(listTeams).mockResolvedValue([]);
    vi.mocked(createTeam).mockRejectedValueOnce(new Error('network')).mockResolvedValue(owned);
    view();
    const input = await screen.findByLabelText('New team');
    fireEvent.change(input, { target: { value: '  Platform  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create team' }));
    expect(await screen.findByRole('alert')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Create team' }));
    await waitFor(() => expect(createTeam).toHaveBeenCalledTimes(2));
    const [first, second] = vi.mocked(createTeam).mock.calls;
    expect(first[0]).toBe('Platform');
    expect(second[1]).toBe(first[1]);
  });

  it('gives admins daily management but no ownership or role controls', async () => {
    vi.mocked(listTeams).mockResolvedValue([{ ...owned, role: 'admin', owner_user_id: 'bob' }]);
    vi.mocked(listMembers).mockResolvedValue([
      { ...alice, user_id: 'bob', display_name: 'Bob', role: 'owner', you: false },
      { ...alice, role: 'admin' },
      { ...alice, user_id: 'carol', display_name: 'Carol', role: 'admin', you: false },
      { ...alice, user_id: 'dave', display_name: 'Dave', role: 'member', you: false },
    ]);
    view();
    expect(await screen.findByRole('button', { name: 'Create invitation link' })).toBeVisible();
    expect(screen.getByLabelText('Team name')).toBeVisible();
    expect(screen.getByRole('region', { name: 'Join requests' })).toBeVisible();
    expect(screen.getAllByRole('button', { name: 'Remove' })).toHaveLength(1);
    expect(screen.queryByRole('button', { name: 'Make owner' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Make admin' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove admin role' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Delete team' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Leave team' })).toBeVisible();
  });

  it('lets the owner explicitly confirm an admin promotion and refreshes roles', async () => {
    const bob = {
      ...alice,
      user_id: 'bob',
      display_name: 'Bob',
      role: 'member' as const,
      you: false,
    };
    vi.mocked(listMembers).mockResolvedValue([alice, bob]);
    vi.mocked(setTeamMemberRole).mockImplementation(async () => {
      vi.mocked(listMembers).mockResolvedValue([alice, { ...bob, role: 'admin' }]);
      return { ...owned, revision: 2 };
    });
    view();
    fireEvent.click(await screen.findByRole('button', { name: 'Make admin' }));
    expect(setTeamMemberRole).not.toHaveBeenCalled();
    const confirmation = await screen.findByRole('group', { name: 'Make Bob an admin?' });
    fireEvent.click(within(confirmation).getByRole('button', { name: 'Make admin' }));
    await waitFor(() =>
      expect(setTeamMemberRole).toHaveBeenCalledExactlyOnceWith(owned, 'bob', 'admin'),
    );
    expect(await screen.findByRole('button', { name: 'Remove admin role' })).toBeVisible();
  });

  it('re-reads the team list when a demoted admin opens the team', async () => {
    const forbidden = new APIError('forbidden', 'Owner or admin only.', 403);
    vi.mocked(listTeams)
      .mockResolvedValueOnce([{ ...owned, role: 'admin', owner_user_id: 'bob' }])
      .mockResolvedValue([{ ...owned, role: 'member', owner_user_id: 'bob' }]);
    vi.mocked(listJoinRequests).mockRejectedValue(forbidden);
    vi.mocked(listInvitations).mockRejectedValue(forbidden);
    view();
    expect(await screen.findByRole('button', { name: 'Leave team' })).toBeVisible();
    expect(screen.queryByText('Teams could not be loaded.')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Create invitation link' }),
    ).not.toBeInTheDocument();
  });

  it('drops a team the viewer was removed from instead of a dead-end error', async () => {
    vi.mocked(listTeams)
      .mockResolvedValueOnce([{ ...owned, role: 'member', owner_user_id: 'carol' }])
      .mockResolvedValue([]);
    vi.mocked(listMembers).mockRejectedValue(new APIError('not_found', 'No such team.', 404));
    view();
    expect(await screen.findByText(/not in any team yet/)).toBeVisible();
    expect(screen.queryByText('Teams could not be loaded.')).not.toBeInTheDocument();
  });

  it('refreshes the team revision after a conflict so a retry can succeed', async () => {
    vi.mocked(renameTeam)
      .mockImplementationOnce(async () => {
        vi.mocked(listTeams).mockResolvedValue([{ ...owned, revision: 2 }]);
        throw new APIError('revision_conflict', 'Stale', 409);
      })
      .mockResolvedValue({ ...owned, name: 'Infra', revision: 3 });
    view();
    fireEvent.change(await screen.findByLabelText('Team name'), { target: { value: 'Infra' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save name' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/changed in the meantime/);
    await waitFor(() => expect(listTeams).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: 'Save name' }));
    await waitFor(() =>
      expect(renameTeam).toHaveBeenLastCalledWith(
        expect.objectContaining({ revision: 2 }),
        'Infra',
      ),
    );
  });
});

// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: async () => ({
    revision: 1,
    definitions: [],
    values: {},
    secret_configured: {},
    bootstrap: [],
    setup: { required: false, groups: [], bootstrap_missing: [] },
  }),
}));

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAdminTeam,
  listAdminTeamAudit,
  type AdminTeamAuditEvent,
  type AdminTeamDetail,
  type AdminTeamPerson,
} from '@/lib/api/adminTeams';
import { APIError } from '@/lib/utils/errors';

vi.mock('next/navigation', () => ({ useParams: () => ({ teamId: 'ateam_1' }) }));
vi.mock('@/lib/api/adminTeams', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/adminTeams')>()),
  getAdminTeam: vi.fn(),
  listAdminTeamAudit: vi.fn(),
}));

import AdminTeamPage from './page';

const AT = '2026-10-15T12:00:00Z';

function person(user_id: string, display_name = '', email: string | null = null): AdminTeamPerson {
  return { user_id, display_name, email, avatar_data_url: 'data:image/svg+xml,<svg/>' };
}

function event(id: number, overrides: Partial<AdminTeamAuditEvent> = {}): AdminTeamAuditEvent {
  return {
    id,
    action: 'member_removed',
    actor: person('alice', 'Alice'),
    target_user: person('carol', '', 'carol@example.com'),
    target_id: null,
    created_at: AT,
    ...overrides,
  };
}

function detail(): AdminTeamDetail {
  return {
    team: {
      id: 'ateam_1',
      name: 'Platform',
      state: 'active',
      owner: person('alice', 'Alice', 'alice@example.com'),
      member_count: 2,
      admin_count: 1,
      pending_request_count: 1,
      created_at: AT,
      deleted_at: null,
    },
    members: [
      {
        ...person('alice', 'Alice', 'alice@example.com'),
        role: 'owner',
        state: 'active',
        joined_at: AT,
        departed_at: null,
      },
      {
        ...person('bob', '', 'bob@example.com'),
        role: 'admin',
        state: 'active',
        joined_at: AT,
        departed_at: null,
      },
      {
        ...person('carol', '', null),
        role: 'member',
        state: 'removed',
        joined_at: AT,
        departed_at: AT,
      },
    ],
    join_requests: [
      { ...person('dave', 'Dave'), id: 'atjr_1', state: 'pending', created_at: AT, expires_at: AT },
    ],
    invitations: { open: 1, expired: 2, claimed: 3, revoked: 0 },
    departed_member_count: 1,
  };
}

beforeEach(() => {
  vi.mocked(getAdminTeam).mockReset();
  vi.mocked(listAdminTeamAudit).mockReset();
});

afterEach(() => cleanup());

describe('Admin → Teams → one team', () => {
  it('shows members past and present, waiting requests, links and history', async () => {
    vi.mocked(getAdminTeam).mockResolvedValue(detail());
    vi.mocked(listAdminTeamAudit)
      .mockResolvedValueOnce({
        events: [
          event(9),
          event(8, { action: 'join_approved', target_user: person('bob', '', 'bob@example.com') }),
        ],
        next_before: 8,
      })
      .mockResolvedValueOnce({
        events: [
          event(7, { action: 'environment_created', target_user: null }),
          event(6, { action: 'team_created', target_user: null }),
        ],
        next_before: null,
      });

    render(<AdminTeamPage />);

    expect(await screen.findByText('Platform')).toBeInTheDocument();
    const members = screen.getByRole('region', { name: 'Members (2)' });
    const rows = within(members).getAllByRole('row').slice(1);
    expect(rows.map((row) => within(row).getAllByRole('cell')[1].textContent)).toEqual([
      'Owner',
      'Admin',
      'Member',
    ]);
    expect(within(rows[2]).getByText(/^Removed/)).toBeInTheDocument();
    // bob has no profile name, so his email names him; carol has neither.
    expect(within(rows[1]).getAllByText('bob@example.com')).toHaveLength(1);
    expect(within(rows[2]).getByText('carol')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Waiting to join (1)' })).toHaveTextContent('Dave');
    expect(screen.getByText('1 open · 3 claimed · 2 expired · 0 revoked')).toBeInTheDocument();

    const history = screen.getByRole('region', { name: 'History' });
    expect(history).toHaveTextContent('removed carol@example.com');
    expect(history).toHaveTextContent('approved bob@example.com’s request to join');
    fireEvent.click(within(history).getByRole('button', { name: 'Show older' }));
    await waitFor(() => expect(history).toHaveTextContent('created the team'));
    expect(history).toHaveTextContent('created a team environment');
    expect(listAdminTeamAudit).toHaveBeenLastCalledWith('ateam_1', 8);
    expect(within(history).queryByRole('button', { name: 'Show older' })).not.toBeInTheDocument();
  });

  it('says how many departures it lists of all the team has had', async () => {
    vi.mocked(getAdminTeam).mockResolvedValue({ ...detail(), departed_member_count: 120 });
    vi.mocked(listAdminTeamAudit).mockResolvedValue({ events: [], next_before: null });

    render(<AdminTeamPage />);

    const members = await screen.findByRole('region', { name: 'Members (2)' });
    expect(members).toHaveTextContent(
      'Showing the latest 1 of 120 people who left or were removed.',
    );
  });

  it('does not count departures when it lists them all', async () => {
    vi.mocked(getAdminTeam).mockResolvedValue(detail());
    vi.mocked(listAdminTeamAudit).mockResolvedValue({ events: [], next_before: null });

    render(<AdminTeamPage />);

    const members = await screen.findByRole('region', { name: 'Members (2)' });
    expect(members).not.toHaveTextContent(/Showing the latest/);
  });

  it('counts every request waiting, not only the oldest ones it lists', async () => {
    const team = detail();
    // The list holds the oldest 50 requests; the count is all of them.
    vi.mocked(getAdminTeam).mockResolvedValue({
      ...team,
      team: { ...team.team, pending_request_count: 60 },
    });
    vi.mocked(listAdminTeamAudit).mockResolvedValue({ events: [], next_before: null });

    render(<AdminTeamPage />);

    const requests = await screen.findByRole('region', { name: 'Waiting to join (60)' });
    expect(requests).toHaveTextContent('Dave');
    expect(requests).toHaveTextContent('Showing the oldest 1 of 60.');
  });

  it('says when there is no such team', async () => {
    vi.mocked(getAdminTeam).mockRejectedValue(new APIError('not_found', 'No such team.', 404));
    vi.mocked(listAdminTeamAudit).mockRejectedValue(
      new APIError('not_found', 'No such team.', 404),
    );

    render(<AdminTeamPage />);

    expect(await screen.findByText('There is no team with this id.')).toBeInTheDocument();
  });
});

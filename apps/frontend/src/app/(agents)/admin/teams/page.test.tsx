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

import { listAdminTeams, type AdminTeamSummary } from '@/lib/api/adminTeams';
import { APIError } from '@/lib/utils/errors';

vi.mock('@/lib/api/adminTeams', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/adminTeams')>()),
  listAdminTeams: vi.fn(),
}));

import AdminTeamsPage from './page';

function team(overrides: Partial<AdminTeamSummary> = {}): AdminTeamSummary {
  return {
    id: 'ateam_1',
    name: 'Platform',
    state: 'active',
    owner: {
      user_id: 'alice',
      display_name: 'Alice Liddell',
      email: 'alice@example.com',
      avatar_data_url: 'data:image/svg+xml,<svg/>',
    },
    member_count: 3,
    admin_count: 1,
    pending_request_count: 2,
    created_at: '2026-10-15T12:00:00Z',
    deleted_at: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(listAdminTeams).mockReset();
});

afterEach(() => cleanup());

describe('Admin → Teams', () => {
  it('lists each team with its owner, counts and a link to it', async () => {
    vi.mocked(listAdminTeams).mockResolvedValue({ teams: [team()], next_offset: null });

    render(<AdminTeamsPage />);

    const link = await screen.findByRole('link', { name: 'Platform' });
    expect(link).toHaveAttribute('href', '/admin/teams/ateam_1');
    const row = link.closest('tr')!;
    expect(within(row).getByText('Alice Liddell')).toBeInTheDocument();
    expect(within(row).getByText('alice@example.com')).toBeInTheDocument();
    expect(
      within(row)
        .getAllByRole('cell')
        .map((cell) => cell.textContent),
    ).toEqual(expect.arrayContaining(['3', '1', '2']));
    expect(listAdminTeams).toHaveBeenCalledWith(
      { q: '', state: 'active', offset: 0 },
      expect.any(AbortSignal),
    );
    expect(screen.getByRole('link', { name: 'Teams' })).toHaveAttribute('aria-current', 'page');
  });

  it('searches, filters by state and shows more', async () => {
    vi.mocked(listAdminTeams).mockResolvedValue({
      teams: [team({ state: 'deleted', deleted_at: '2026-10-16T00:00:00Z' })],
      next_offset: 1,
    });
    render(<AdminTeamsPage />);
    await screen.findByText('Deleted');

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search teams' }), {
      target: { value: '  bob  ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() =>
      expect(listAdminTeams).toHaveBeenLastCalledWith(
        { q: 'bob', state: 'active', offset: 0 },
        expect.any(AbortSignal),
      ),
    );
    fireEvent.click(screen.getByRole('button', { name: 'All' }));
    await waitFor(() =>
      expect(listAdminTeams).toHaveBeenLastCalledWith(
        { q: 'bob', state: 'all', offset: 0 },
        expect.any(AbortSignal),
      ),
    );
    fireEvent.click(await screen.findByRole('button', { name: 'Show more' }));
    await waitFor(() =>
      expect(listAdminTeams).toHaveBeenLastCalledWith(
        { q: 'bob', state: 'all', offset: 1 },
        expect.any(AbortSignal),
      ),
    );
    await waitFor(() => expect(screen.getAllByRole('link', { name: 'Platform' })).toHaveLength(2));
  });

  it('says when nothing matches', async () => {
    vi.mocked(listAdminTeams).mockResolvedValue({ teams: [], next_offset: null });
    render(<AdminTeamsPage />);
    expect(await screen.findByText('No teams yet.')).toBeInTheDocument();
  });

  it('shows the admin notice when access is refused', async () => {
    vi.mocked(listAdminTeams).mockRejectedValue(new APIError('forbidden', 'Forbidden', 403));
    render(<AdminTeamsPage />);
    expect(await screen.findByText('Admin access required')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });
});

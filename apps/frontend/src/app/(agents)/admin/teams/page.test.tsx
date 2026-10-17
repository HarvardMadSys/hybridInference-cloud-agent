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
    vi.mocked(listAdminTeams).mockResolvedValue({ teams: [team()], next_before: null });

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
      { q: '', state: 'active', before: null },
      expect.any(AbortSignal),
    );
    expect(screen.getByRole('link', { name: 'Teams' })).toHaveAttribute('aria-current', 'page');
  });

  it('searches, filters by state and shows more', async () => {
    vi.mocked(listAdminTeams).mockResolvedValue({
      teams: [team({ state: 'deleted', deleted_at: '2026-10-16T00:00:00Z' })],
      next_before: 'ateam_1',
    });
    render(<AdminTeamsPage />);
    await screen.findByText('Deleted');

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search teams' }), {
      target: { value: '  bob  ' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    await waitFor(() =>
      expect(listAdminTeams).toHaveBeenLastCalledWith(
        { q: 'bob', state: 'active', before: null },
        expect.any(AbortSignal),
      ),
    );
    fireEvent.click(screen.getByRole('button', { name: 'All' }));
    await waitFor(() =>
      expect(listAdminTeams).toHaveBeenLastCalledWith(
        { q: 'bob', state: 'all', before: null },
        expect.any(AbortSignal),
      ),
    );
    // The next page starts after the last team shown, not after a row count.
    fireEvent.click(await screen.findByRole('button', { name: 'Show more' }));
    await waitFor(() =>
      expect(listAdminTeams).toHaveBeenLastCalledWith(
        { q: 'bob', state: 'all', before: 'ateam_1' },
        expect.any(AbortSignal),
      ),
    );
    await waitFor(() => expect(screen.getAllByRole('link', { name: 'Platform' })).toHaveLength(2));
  });

  it('says when nothing matches', async () => {
    vi.mocked(listAdminTeams).mockResolvedValue({ teams: [], next_before: null });
    render(<AdminTeamsPage />);
    expect(await screen.findByText('No active teams.')).toBeInTheDocument();
  });

  it('names the filter that found nothing', async () => {
    vi.mocked(listAdminTeams).mockResolvedValue({ teams: [], next_before: null });
    render(<AdminTeamsPage />);
    await screen.findByText(/^No /);

    // Active teams may well exist; none of them is deleted.
    fireEvent.click(screen.getByRole('button', { name: 'Deleted' }));
    expect(await screen.findByText('No deleted teams.')).toBeInTheDocument();

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search teams' }), {
      target: { value: 'bob' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    expect(await screen.findByText('No deleted team matches “bob”.')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'All' }));
    expect(await screen.findByText('No team matches “bob”.')).toBeInTheDocument();

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search teams' }), {
      target: { value: '' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Search' }));
    expect(await screen.findByText('No teams yet.')).toBeInTheDocument();
  });

  it('shows the admin notice when access is refused', async () => {
    vi.mocked(listAdminTeams).mockRejectedValue(new APIError('forbidden', 'Forbidden', 403));
    render(<AdminTeamsPage />);
    expect(await screen.findByText('Admin access required')).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();
  });
});

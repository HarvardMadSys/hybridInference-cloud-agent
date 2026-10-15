// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { claimInvitation, type OwnJoinRequest } from '@/lib/api/teams';
import { readInvitationToken, stashInvitationToken } from '@/lib/teamInvitation';
import { APIError } from '@/lib/utils/errors';
import { JoinTeamView } from './JoinTeamView';

const auth = vi.hoisted(() => ({ signedIn: true }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({
    state: {
      loading: false,
      isAuthenticated: auth.signedIn,
      user: auth.signedIn ? { id: 'bob' } : null,
    },
  }),
}));
vi.mock('@/lib/api/teams', () => ({
  claimInvitation: vi.fn(),
  listOwnJoinRequests: vi.fn(),
}));

const TOKEN = 'T'.repeat(43);
const pending: OwnJoinRequest = {
  id: 'atjr_1',
  team_id: 'ateam_1',
  team_name: 'Platform',
  inviter_display_name: 'Alice',
  state: 'pending',
  created_at: '2026-10-15T00:00:00Z',
  expires_at: '2026-10-16T00:00:00Z',
};

function view() {
  return render(
    <LocaleProvider>
      <JoinTeamView />
    </LocaleProvider>,
  );
}

describe('JoinTeamView', () => {
  beforeEach(() => {
    auth.signedIn = true;
    vi.mocked(claimInvitation).mockReset();
    window.history.replaceState(null, '', `/teams/join#token=${TOKEN}`);
  });
  afterEach(() => {
    cleanup();
    window.sessionStorage.clear();
  });

  it('moves the token out of the address bar before anything else', async () => {
    auth.signedIn = false;
    view();
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument();
    expect(window.location.hash).toBe('');
    expect(window.location.href).not.toContain(TOKEN);
    expect(readInvitationToken()).toBe(TOKEN);
    expect(claimInvitation).not.toHaveBeenCalled();
  });

  it('claims only when asked, then forgets the token and shows where the request stands', async () => {
    vi.mocked(claimInvitation).mockResolvedValue(pending);
    view();
    fireEvent.click(await screen.findByRole('button', { name: 'Ask to join' }));
    expect(await screen.findByText(/You asked to join Platform, invited by Alice/)).toBeVisible();
    expect(claimInvitation).toHaveBeenCalledExactlyOnceWith(TOKEN);
    expect(readInvitationToken()).toBeNull();
    expect(screen.getByRole('button', { name: 'Check again' })).toBeInTheDocument();
  });

  it('resumes from tab storage after the sign-in round trip', async () => {
    window.history.replaceState(null, '', '/teams/join');
    stashInvitationToken(TOKEN);
    vi.mocked(claimInvitation).mockResolvedValue(pending);
    view();
    fireEvent.click(await screen.findByRole('button', { name: 'Ask to join' }));
    expect(claimInvitation).toHaveBeenCalledExactlyOnceWith(TOKEN);
  });

  it('drops a dead link and says so', async () => {
    vi.mocked(claimInvitation).mockRejectedValue(
      new APIError('invitation_unavailable', 'Gone.', 410),
    );
    view();
    fireEvent.click(await screen.findByRole('button', { name: 'Ask to join' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(/no longer valid/);
    expect(readInvitationToken()).toBeNull();
    expect(screen.queryByRole('button', { name: 'Ask to join' })).not.toBeInTheDocument();
  });

  it('keeps the link for a retry when the failure is temporary', async () => {
    vi.mocked(claimInvitation).mockRejectedValue(new APIError('unavailable', 'Later.', 503));
    view();
    fireEvent.click(await screen.findByRole('button', { name: 'Ask to join' }));
    expect(await screen.findByRole('alert')).toBeVisible();
    expect(readInvitationToken()).toBe(TOKEN);
    expect(screen.getByRole('button', { name: 'Ask to join' })).toBeEnabled();
  });

  it('explains what to do without a link', async () => {
    window.history.replaceState(null, '', '/teams/join');
    view();
    expect(await screen.findByText(/needs an invitation link/)).toBeVisible();
  });
});

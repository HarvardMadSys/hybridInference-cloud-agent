// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { claimInvitation, listOwnJoinRequests, type OwnJoinRequest } from '@/lib/api/teams';
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
vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useSiteConfig: () => ({ branding: { appName: 'Cloud Agent' } }),
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
    vi.mocked(listOwnJoinRequests).mockReset();
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
    expect(await screen.findByRole('heading', { name: 'Request sent' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Platform' })).toBeVisible();
    expect(screen.getByText('Invited by Alice')).toBeVisible();
    expect(claimInvitation).toHaveBeenCalledExactlyOnceWith(TOKEN);
    expect(readInvitationToken()).toBeNull();
    expect(screen.getByRole('button', { name: 'Refresh status' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Go to Teams' })).toHaveAttribute(
      'href',
      '/settings/teams',
    );
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

  it.each([
    [
      'no_longer_member',
      'You are no longer a member of this team. Ask an owner or admin for a new invitation link.',
    ],
    ['already_member', 'You are already a member of this team.'],
  ])('answers %s once, in place of the link hint', async (code, sentence) => {
    vi.mocked(claimInvitation).mockRejectedValue(new APIError(code, 'Refused.', 409));
    view();
    fireEvent.click(await screen.findByRole('button', { name: 'Ask to join' }));
    expect(await screen.findByRole('status')).toHaveTextContent(sentence);
    expect(screen.getAllByText(sentence)).toHaveLength(1);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.queryByText(/needs an invitation link/)).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Welcome to the team' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Join a team' })).toBeVisible();
    expect(readInvitationToken()).toBeNull();
    expect(screen.queryByRole('button', { name: 'Ask to join' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Go to Teams' })).toHaveAttribute(
      'href',
      '/settings/teams',
    );
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

  it.each([
    ['approved', 'Welcome to the team'],
    ['declined', 'Request not approved'],
    ['cancelled', 'Invitation withdrawn'],
    ['expired', 'Invitation expired'],
  ] as const)('refreshes a pending request into the %s state', async (state, heading) => {
    vi.mocked(claimInvitation).mockResolvedValue(pending);
    vi.mocked(listOwnJoinRequests).mockResolvedValue([{ ...pending, state }]);
    view();
    fireEvent.click(await screen.findByRole('button', { name: 'Ask to join' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Refresh status' }));
    expect(await screen.findByRole('heading', { name: heading })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Platform' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Refresh status' })).not.toBeInTheDocument();
    expect(screen.queryByText('Awaiting approval')).not.toBeInTheDocument();
  });
});

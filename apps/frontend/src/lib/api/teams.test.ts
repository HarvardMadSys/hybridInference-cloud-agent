import { beforeEach, describe, expect, it, vi } from 'vitest';
import { expectRequestsMatchContract } from './__tests__/contract';
import { fetchWithAuth } from './client';
import {
  claimInvitation,
  createInvitation,
  createTeam,
  decideJoinRequest,
  deleteTeam,
  invitationLink,
  leaveTeam,
  listInvitations,
  listJoinRequests,
  listMembers,
  listOwnJoinRequests,
  listTeams,
  removeMember,
  renameTeam,
  revokeInvitation,
  setTeamMemberRole,
  transferOwnership,
  type Team,
} from './teams';

vi.mock('./client', async (original) => ({
  ...(await original<typeof import('./client')>()),
  fetchWithAuth: vi.fn(),
}));

const team: Team = {
  id: 'ateam_1',
  name: 'Platform',
  owner_user_id: 'alice',
  role: 'owner',
  revision: 4,
  member_count: 2,
  created_at: '2026-10-15T00:00:00Z',
};

function lastCall() {
  const [, path, init] = vi.mocked(fetchWithAuth).mock.calls.at(-1)!;
  return { path, init: init ?? {} };
}

describe('team API client', () => {
  beforeEach(() => {
    vi.mocked(fetchWithAuth).mockReset();
  });

  it('sends only requests the server accepts', async () => {
    vi.mocked(fetchWithAuth).mockImplementation(async () => new Response(JSON.stringify(team)));
    await createTeam('Platform', 'key-1');
    await renameTeam(team, 'Infra');
    await transferOwnership(team, 'bob');
    await setTeamMemberRole(team, 'bob', 'member');
    await createInvitation('ateam_1', 'key-2');
    await decideJoinRequest('ateam_1', 'atjr_1', true);
    await claimInvitation('A'.repeat(43));
    vi.mocked(fetchWithAuth).mockImplementation(
      async () =>
        new Response(JSON.stringify({ teams: [], members: [], invitations: [], requests: [] })),
    );
    await listTeams();
    await listMembers('ateam_1');
    await listInvitations('ateam_1');
    await listJoinRequests('ateam_1');
    await listOwnJoinRequests();
    vi.mocked(fetchWithAuth).mockImplementation(async () => new Response(null, { status: 204 }));
    await deleteTeam(team);
    await leaveTeam('ateam_1');
    await removeMember('ateam_1', 'bob');
    await revokeInvitation('ateam_1', 'atinv_1');
    expectRequestsMatchContract();
  });

  it('unwraps each list', async () => {
    vi.mocked(fetchWithAuth).mockImplementation(
      async () =>
        new Response(
          JSON.stringify({ teams: [team], members: ['m'], invitations: ['i'], requests: ['r'] }),
        ),
    );
    await expect(listTeams()).resolves.toEqual([team]);
    await expect(listMembers('ateam_1')).resolves.toEqual(['m']);
    await expect(listInvitations('ateam_1')).resolves.toEqual(['i']);
    await expect(listJoinRequests('ateam_1')).resolves.toEqual(['r']);
    await expect(listOwnJoinRequests()).resolves.toEqual(['r']);
  });

  it('sends the revision the page read with every change that can conflict', async () => {
    vi.mocked(fetchWithAuth).mockImplementation(async () => new Response(JSON.stringify(team)));
    await renameTeam(team, 'Infra');
    expect(lastCall().path).toBe('/v1/agent/teams/ateam_1');
    expect(JSON.parse(String(lastCall().init.body))).toEqual({
      name: 'Infra',
      expected_revision: 4,
    });
    await transferOwnership(team, 'bob');
    expect(JSON.parse(String(lastCall().init.body))).toEqual({
      user_id: 'bob',
      expected_revision: 4,
    });
    await setTeamMemberRole(team, 'bob/admin', 'admin');
    expect(lastCall().path).toBe('/v1/agent/teams/ateam_1/members/bob%2Fadmin/role');
    expect(lastCall().init.method).toBe('PATCH');
    expect(JSON.parse(String(lastCall().init.body))).toEqual({
      role: 'admin',
      expected_revision: 4,
    });
    vi.mocked(fetchWithAuth).mockResolvedValue(new Response(null, { status: 204 }));
    await deleteTeam(team);
    expect(lastCall()).toMatchObject({
      path: '/v1/agent/teams/ateam_1?expected_revision=4',
      init: { method: 'DELETE' },
    });
  });

  it('escapes ids in paths and never caches a team answer', async () => {
    vi.mocked(fetchWithAuth).mockResolvedValue(new Response(null, { status: 204 }));
    await removeMember('ateam_1', 'user/../x');
    expect(lastCall().path).toBe('/v1/agent/teams/ateam_1/members/user%2F..%2Fx');
    expect(lastCall().init.cache).toBe('no-store');
  });

  it('surfaces a refusal from a no-content route as an error', async () => {
    vi.mocked(fetchWithAuth).mockResolvedValue(
      new Response(
        JSON.stringify({ error: { type: 'owner_cannot_leave', message: 'Transfer first.' } }),
        { status: 409, headers: { 'Content-Type': 'application/json' } },
      ),
    );
    await expect(removeMember('ateam_1', 'alice')).rejects.toMatchObject({
      code: 'owner_cannot_leave',
      statusCode: 409,
    });
  });

  it('claims with the token in the body, not the URL', async () => {
    vi.mocked(fetchWithAuth).mockResolvedValue(new Response(JSON.stringify({ id: 'atjr_1' })));
    const token = 'A'.repeat(43);
    await claimInvitation(token);
    expect(lastCall().path).toBe('/v1/agent/team-invitations/claim');
    expect(lastCall().path).not.toContain(token);
    expect(JSON.parse(String(lastCall().init.body))).toEqual({ token });
  });

  it('puts the link token in the fragment', () => {
    expect(invitationLink('A'.repeat(43), 'https://agent.example')).toBe(
      `https://agent.example/teams/join#token=${'A'.repeat(43)}`,
    );
  });
});

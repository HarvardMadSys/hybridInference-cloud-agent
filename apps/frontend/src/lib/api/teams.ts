import { basePath, config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

const TEAMS = '/v1/agent/teams';

export type TeamRole = 'owner' | 'admin' | 'member';
export type InvitationStatus = 'open' | 'claimed' | 'revoked' | 'expired';
export type JoinRequestState = 'pending' | 'approved' | 'declined' | 'cancelled' | 'expired';

export interface Team {
  id: string;
  name: string;
  owner_user_id: string;
  role: TeamRole;
  /** Sent with a rename, transfer or delete, so a stale page cannot overwrite a newer change. */
  revision: number;
  member_count: number;
  created_at: string;
}

export interface TeamPerson {
  user_id: string;
  /** The person's own profile name, else their account email; may be empty. */
  display_name: string;
  email: string | null;
  avatar_data_url: string;
}

export interface TeamMember extends TeamPerson {
  role: TeamRole;
  joined_at: string;
  you: boolean;
}

export interface TeamInvitation {
  id: string;
  status: InvitationStatus;
  created_by_user_id: string;
  created_at: string;
  expires_at: string;
  claimed_by_user_id: string | null;
  request_state: JoinRequestState | null;
}

export interface CreatedInvitation {
  invitation: TeamInvitation;
  /** Present only in the response that created the link; never retrievable again. */
  token: string | null;
}

export interface TeamJoinRequest extends TeamPerson {
  id: string;
  state: JoinRequestState;
  created_at: string;
  expires_at: string;
}

export interface OwnJoinRequest {
  id: string;
  team_id: string;
  team_name: string;
  inviter_display_name: string;
  state: JoinRequestState;
  created_at: string;
  expires_at: string;
}

function team(teamId: string, suffix = ''): string {
  return `${TEAMS}/${encodeURIComponent(teamId)}${suffix}`;
}

async function send(path: string, init: RequestInit, signal?: AbortSignal): Promise<Response> {
  return fetchWithAuth(config.apiBase, path, { cache: 'no-store', ...init, signal });
}

function json(method: string, body: unknown): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}

async function noContent(resp: Response): Promise<void> {
  if (resp.ok) return;
  await jsonOrThrow<never>(resp);
}

/** A fresh idempotency key: resending the same request never creates a second team or link. */
export function newRequestKey(): string {
  return crypto.randomUUID();
}

export async function listTeams(signal?: AbortSignal): Promise<Team[]> {
  const body = await jsonOrThrow<{ teams: Team[] }>(await send(TEAMS, {}, signal));
  return body.teams;
}

export async function createTeam(
  name: string,
  requestKey: string,
  signal?: AbortSignal,
): Promise<Team> {
  return jsonOrThrow<Team>(
    await send(TEAMS, json('POST', { name, request_key: requestKey }), signal),
  );
}

export async function renameTeam(current: Team, name: string, signal?: AbortSignal): Promise<Team> {
  return jsonOrThrow<Team>(
    await send(
      team(current.id),
      json('PATCH', { name, expected_revision: current.revision }),
      signal,
    ),
  );
}

export async function deleteTeam(current: Team, signal?: AbortSignal): Promise<void> {
  const query = new URLSearchParams({ expected_revision: String(current.revision) });
  await noContent(await send(`${team(current.id)}?${query}`, { method: 'DELETE' }, signal));
}

export async function listMembers(teamId: string, signal?: AbortSignal): Promise<TeamMember[]> {
  const body = await jsonOrThrow<{ members: TeamMember[] }>(
    await send(team(teamId, '/members'), {}, signal),
  );
  return body.members;
}

export async function leaveTeam(teamId: string, signal?: AbortSignal): Promise<void> {
  await noContent(await send(team(teamId, '/leave'), { method: 'POST' }, signal));
}

export async function removeMember(
  teamId: string,
  userId: string,
  signal?: AbortSignal,
): Promise<void> {
  await noContent(
    await send(
      team(teamId, `/members/${encodeURIComponent(userId)}`),
      { method: 'DELETE' },
      signal,
    ),
  );
}

export async function transferOwnership(
  current: Team,
  userId: string,
  signal?: AbortSignal,
): Promise<Team> {
  return jsonOrThrow<Team>(
    await send(
      team(current.id, '/transfer-ownership'),
      json('POST', { user_id: userId, expected_revision: current.revision }),
      signal,
    ),
  );
}

export async function setTeamMemberRole(
  current: Team,
  userId: string,
  role: Exclude<TeamRole, 'owner'>,
  signal?: AbortSignal,
): Promise<Team> {
  return jsonOrThrow<Team>(
    await send(
      team(current.id, `/members/${encodeURIComponent(userId)}/role`),
      json('PATCH', { role, expected_revision: current.revision }),
      signal,
    ),
  );
}

export async function createInvitation(
  teamId: string,
  requestKey: string,
  signal?: AbortSignal,
): Promise<CreatedInvitation> {
  return jsonOrThrow<CreatedInvitation>(
    await send(team(teamId, '/invitations'), json('POST', { request_key: requestKey }), signal),
  );
}

export async function listInvitations(
  teamId: string,
  signal?: AbortSignal,
): Promise<TeamInvitation[]> {
  const body = await jsonOrThrow<{ invitations: TeamInvitation[] }>(
    await send(team(teamId, '/invitations'), {}, signal),
  );
  return body.invitations;
}

export async function revokeInvitation(
  teamId: string,
  invitationId: string,
  signal?: AbortSignal,
): Promise<void> {
  await noContent(
    await send(
      team(teamId, `/invitations/${encodeURIComponent(invitationId)}`),
      { method: 'DELETE' },
      signal,
    ),
  );
}

export async function listJoinRequests(
  teamId: string,
  signal?: AbortSignal,
): Promise<TeamJoinRequest[]> {
  const body = await jsonOrThrow<{ requests: TeamJoinRequest[] }>(
    await send(team(teamId, '/join-requests'), {}, signal),
  );
  return body.requests;
}

export async function decideJoinRequest(
  teamId: string,
  requestId: string,
  approve: boolean,
  signal?: AbortSignal,
): Promise<TeamJoinRequest> {
  return jsonOrThrow<TeamJoinRequest>(
    await send(
      team(teamId, `/join-requests/${encodeURIComponent(requestId)}/decision`),
      json('POST', { approve }),
      signal,
    ),
  );
}

export async function claimInvitation(
  token: string,
  signal?: AbortSignal,
): Promise<OwnJoinRequest> {
  return jsonOrThrow<OwnJoinRequest>(
    await send('/v1/agent/team-invitations/claim', json('POST', { token }), signal),
  );
}

export async function listOwnJoinRequests(signal?: AbortSignal): Promise<OwnJoinRequest[]> {
  const body = await jsonOrThrow<{ requests: OwnJoinRequest[] }>(
    await send('/v1/agent/team-join-requests', {}, signal),
  );
  return body.requests;
}

/**
 * The link an owner shares. The token rides in the fragment, which browsers do
 * not send to any server, so it never reaches request logs or a referrer.
 */
export function invitationLink(token: string, origin = window.location.origin): string {
  return `${origin}${basePath}/teams/join#token=${encodeURIComponent(token)}`;
}

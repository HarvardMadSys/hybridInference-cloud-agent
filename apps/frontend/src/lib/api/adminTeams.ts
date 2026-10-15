import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from './client';

/** Someone a team page names: their profile name, else their account email. */
export interface AdminTeamPerson {
  user_id: string;
  display_name: string;
  /** Read from the Gateway per request; null when it could not be read. */
  email: string | null;
  avatar_data_url: string;
}

export interface AdminTeamSummary {
  id: string;
  name: string;
  state: 'active' | 'deleted';
  owner: AdminTeamPerson;
  member_count: number;
  /** Active admins, not counting the owner. */
  admin_count: number;
  /** Join requests waiting for a decision. */
  pending_request_count: number;
  created_at: string;
  deleted_at: string | null;
}

export interface AdminTeamsPage {
  teams: AdminTeamSummary[];
  next_offset: number | null;
}

export interface AdminTeamMember extends AdminTeamPerson {
  role: 'owner' | 'admin' | 'member';
  state: 'active' | 'left' | 'removed';
  joined_at: string;
  departed_at: string | null;
}

export interface AdminTeamJoinRequest extends AdminTeamPerson {
  id: string;
  state: string;
  created_at: string;
  expires_at: string;
}

export interface AdminTeamDetail {
  team: AdminTeamSummary;
  members: AdminTeamMember[];
  join_requests: AdminTeamJoinRequest[];
  invitations: { open: number; expired: number; claimed: number; revoked: number };
}

export interface AdminTeamAuditEvent {
  id: number;
  action: string;
  actor: AdminTeamPerson;
  target_user: AdminTeamPerson | null;
  target_id: string | null;
  created_at: string;
}

export interface AdminTeamAuditPage {
  events: AdminTeamAuditEvent[];
  next_before: number | null;
}

export type AdminTeamStateFilter = 'active' | 'deleted' | 'all';

async function read<T>(path: string, signal?: AbortSignal): Promise<T> {
  return jsonOrThrow<T>(await fetchWithAuth(config.apiBase, path, { signal, cache: 'no-store' }));
}

/** Teams newest first; `q` matches part of a name, a team id, or a member's user id. */
export function listAdminTeams(
  filters: { q?: string; state?: AdminTeamStateFilter; offset?: number },
  signal?: AbortSignal,
): Promise<AdminTeamsPage> {
  const params = new URLSearchParams();
  if (filters.q?.trim()) params.set('q', filters.q.trim());
  if (filters.state) params.set('state', filters.state);
  if (filters.offset) params.set('offset', String(filters.offset));
  return read(`/admin/agent/teams${params.size ? `?${params}` : ''}`, signal);
}

export function getAdminTeam(teamId: string, signal?: AbortSignal): Promise<AdminTeamDetail> {
  return read(`/admin/agent/teams/${encodeURIComponent(teamId)}`, signal);
}

export function listAdminTeamAudit(
  teamId: string,
  before: number | null,
  signal?: AbortSignal,
): Promise<AdminTeamAuditPage> {
  const suffix = before === null ? '' : `?before=${before}`;
  return read(`/admin/agent/teams/${encodeURIComponent(teamId)}/audit-events${suffix}`, signal);
}

/** The name a page shows for someone: profile name, email, then the bare id. */
export function personLabel(person: AdminTeamPerson): string {
  return person.display_name || person.email || person.user_id;
}

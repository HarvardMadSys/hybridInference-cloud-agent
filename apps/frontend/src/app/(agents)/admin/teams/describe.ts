// How the admin team page words one audit event: the actor is shown beside it,
// so the sentence says what they did, naming whoever it was done to.

import { personLabel, type AdminTeamAuditEvent } from '@/lib/api/adminTeams';

const EVENT_SENTENCES: Record<string, (target: string) => string> = {
  team_created: () => 'created the team',
  team_renamed: () => 'renamed the team',
  team_deleted: () => 'deleted the team',
  invitation_created: () => 'created an invitation link',
  invitation_revoked: () => 'revoked an invitation link',
  join_requested: () => 'asked to join',
  join_approved: (target) => `approved ${target}’s request to join`,
  join_declined: (target) => `declined ${target}’s request to join`,
  member_left: () => 'left the team',
  member_removed: (target) => `removed ${target}`,
  member_role_changed: (target) => `changed ${target}’s role`,
  ownership_transferred: (target) => `made ${target} the owner`,
  environment_created: () => 'created a team environment',
  environment_updated: () => 'changed a team environment',
  environment_archived: () => 'removed a team environment',
};

/** One sentence for an audit event; the actor is shown beside it. */
export function describeTeamEvent(event: AdminTeamAuditEvent): string {
  const sentence = EVENT_SENTENCES[event.action];
  if (!sentence) return event.action;
  return sentence(event.target_user ? personLabel(event.target_user) : 'someone');
}

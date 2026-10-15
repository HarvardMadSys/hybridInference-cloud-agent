import type { Locale } from '@/lib/i18n/locales';
import { LOCALE_HTML_LANG } from '@/lib/i18n/locales';
import type { Translator } from '@/lib/i18n/translate';
import { APIError } from '@/lib/utils/errors';

export const CARD = 'rounded-2xl border border-border bg-surface';
export const INPUT =
  'ui-focus-ring w-full min-w-0 rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground disabled:opacity-50';
export const SECONDARY = 'ui-button ui-button-secondary ui-focus-ring min-h-9 rounded-lg';
export const GHOST = 'ui-button ui-button-ghost ui-focus-ring min-h-9 rounded-lg';
export const DANGER =
  'ui-button ui-button-secondary ui-focus-ring min-h-9 rounded-lg text-status-error';

const KNOWN_ERRORS = {
  already_member: 'teams.error.alreadyMember',
  already_owner: 'teams.error.alreadyOwner',
  applicant_not_admitted: 'teams.error.applicantNotAdmitted',
  forbidden: 'teams.error.forbidden',
  invitation_closed: 'teams.error.invitationClosed',
  invitation_limit_reached: 'teams.error.invitationLimit',
  invitation_unavailable: 'teams.error.invitationUnavailable',
  not_found: 'teams.error.notFound',
  owner_cannot_leave: 'teams.error.ownerCannotLeave',
  owner_not_admitted: 'teams.error.ownerNotAdmitted',
  request_closed: 'teams.error.requestClosed',
  request_expired: 'teams.error.requestExpired',
  request_pending: 'teams.error.requestPending',
  revision_conflict: 'teams.error.revisionConflict',
  team_full: 'teams.error.teamFull',
  team_limit_reached: 'teams.error.teamLimit',
  unavailable: 'teams.error.unavailable',
} as const;

/** A readable sentence for a team refusal, in the reader's language. */
export function teamErrorMessage(t: Translator, cause: unknown): string {
  if (cause instanceof APIError) {
    const key = KNOWN_ERRORS[cause.code as keyof typeof KNOWN_ERRORS];
    if (key) return t(key);
  }
  return t('teams.error.generic');
}

export function formatTeamTime(locale: Locale, iso: string): string {
  return new Intl.DateTimeFormat(LOCALE_HTML_LANG[locale], {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(iso));
}

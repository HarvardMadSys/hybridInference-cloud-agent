'use client';

// The page an invitation link opens. It sits outside the signed-in app shell,
// like `/login`, because the shell would send a signed-out visitor to sign in
// and drop the link's fragment on the way. Instead this page moves the token
// out of the address bar into short-lived tab storage first, then asks the
// visitor to sign in if needed, and only claims when they choose to.

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { useAuth, useLocale, useT } from '@/components/providers';
import { basePath } from '@/config/env';
import { claimInvitation, listOwnJoinRequests, type OwnJoinRequest } from '@/lib/api/teams';
import {
  clearInvitationToken,
  readInvitationToken,
  stashInvitationToken,
  tokenFromHash,
} from '@/lib/teamInvitation';
import { APIError } from '@/lib/utils/errors';
import { SETTINGS_PATHS } from '@/components/features/settings/navigation';
import { formatTeamTime, teamErrorMessage } from './teamUi';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { TeamJoinIcon } from './TeamJoinIcon';

const JOIN_PATH = '/teams/join';

// Refusals that answer for the visitor's membership rather than the link. The
// page says them once, where it would otherwise ask for a link.
const MEMBERSHIP_ANSWERS = new Set(['already_member', 'no_longer_member']);

export function JoinTeamView() {
  const t = useT();
  const { locale } = useLocale();
  const { state } = useAuth();
  const { branding } = useSiteConfig();
  // Undefined until the effect below has moved the fragment into storage.
  const [token, setToken] = useState<string | null | undefined>(undefined);
  const [request, setRequest] = useState<OwnJoinRequest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [membership, setMembership] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const fromHash = tokenFromHash(window.location.hash);
    if (fromHash) stashInvitationToken(fromHash);
    if (window.location.hash) {
      // Out of the address bar, history and anything that copies the URL.
      window.history.replaceState(null, '', `${window.location.pathname}${window.location.search}`);
    }
    setToken(fromHash ?? readInvitationToken());
  }, []);

  const claim = async () => {
    if (!token || busy) return;
    setBusy(true);
    setError(null);
    try {
      setRequest(await claimInvitation(token));
      clearInvitationToken();
    } catch (cause) {
      const answer = cause instanceof APIError && MEMBERSHIP_ANSWERS.has(cause.code);
      if (cause instanceof APIError && cause.statusCode === 403) {
        setError(t('teams.join.noAccess'));
      } else if (!answer) {
        setError(teamErrorMessage(t, cause));
      }
      // A refusal about the link itself is final; a transient one keeps it for a retry.
      if (cause instanceof APIError && (cause.statusCode === 410 || cause.statusCode === 409)) {
        clearInvitationToken();
        setToken(null);
        setMembership(answer ? teamErrorMessage(t, cause) : null);
      }
    } finally {
      setBusy(false);
    }
  };

  const checkAgain = async () => {
    if (!request || busy) return;
    setBusy(true);
    setError(null);
    try {
      const mine = await listOwnJoinRequests();
      setRequest(mine.find((item) => item.id === request.id) ?? request);
    } catch (cause) {
      setError(teamErrorMessage(t, cause));
    } finally {
      setBusy(false);
    }
  };

  const primary = 'ui-button ui-button-primary ui-focus-ring min-h-11 flex-1 rounded-xl px-5';
  const secondary = 'ui-button ui-button-secondary ui-focus-ring min-h-11 flex-1 rounded-xl px-5';
  const kind = request
    ? request.state === 'pending' || request.state === 'approved'
      ? request.state
      : 'closed'
    : 'invite';
  const heading = request ? t(`teams.join.heading.${request.state}`) : t('teams.join.title');
  let body: React.ReactNode;
  if (request) {
    body = (
      <>
        <div className="flex items-center gap-3 rounded-xl border border-border bg-surface-muted p-4">
          <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-border bg-surface text-muted">
            <TeamJoinIcon />
          </span>
          <div className="min-w-0">
            <h2 className="break-words text-base font-semibold text-foreground">
              {request.team_name}
            </h2>
            <p className="mt-1 break-words text-xs text-muted">
              {t('teams.join.invitedBy', {
                inviter: request.inviter_display_name || t('teams.join.theOwner'),
              })}
            </p>
          </div>
        </div>
        {request.state === 'pending' ? (
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
            <span className="inline-flex items-center gap-1.5 rounded-full bg-status-warning/10 px-2.5 py-1 font-medium text-status-warning">
              <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
              {t('teams.join.pendingBadge')}
            </span>
            <span className="text-muted">
              {t('teams.join.expires', { expires: formatTeamTime(locale, request.expires_at) })}
            </span>
          </div>
        ) : null}
        <p role="status" className="text-sm leading-6 text-muted">
          {t(`teams.join.state.${request.state}`)}
        </p>
        <div className="flex flex-col gap-2 pt-1 sm:flex-row">
          <Link href={SETTINGS_PATHS.teams} className={primary}>
            {t('teams.join.openTeams')}
          </Link>
          {request.state === 'pending' ? (
            <button
              type="button"
              className={secondary}
              disabled={busy}
              onClick={() => void checkAgain()}
            >
              {busy ? t('teams.join.checking') : t('teams.join.checkAgain')}
            </button>
          ) : null}
        </div>
      </>
    );
  } else if (token === undefined || state.loading) {
    body = (
      <p role="status" className="text-sm text-muted">
        {t('teams.loading')}
      </p>
    );
  } else if (!token) {
    body = (
      <>
        {membership ? (
          <p role="status" className="text-sm leading-6 text-muted">
            {membership}
          </p>
        ) : (
          <p className="text-sm leading-6 text-muted">{t('teams.join.noLink')}</p>
        )}
        <Link href={SETTINGS_PATHS.teams} className={primary}>
          {t('teams.join.openTeams')}
        </Link>
      </>
    );
  } else if (!state.isAuthenticated) {
    body = (
      <>
        <p className="text-sm leading-6 text-muted">{t('teams.join.signInFirst')}</p>
        <button
          type="button"
          className={primary}
          onClick={() => {
            const query = new URLSearchParams({ return_to: JOIN_PATH });
            window.location.assign(`${basePath}/login?${query.toString()}`);
          }}
        >
          {t('teams.join.signIn')}
        </button>
      </>
    );
  } else {
    body = (
      <>
        <p className="text-sm leading-6 text-muted">{t('teams.join.explain')}</p>
        <button type="button" className={primary} disabled={busy} onClick={() => void claim()}>
          {busy ? t('teams.join.requesting') : t('teams.join.request')}
        </button>
      </>
    );
  }

  return (
    <main className="flex min-h-dvh flex-col items-center justify-center bg-surface-muted px-5 py-12">
      <div className="w-full max-w-[480px]">
        <Link
          href="/"
          className="ui-focus-ring mx-auto mb-7 flex w-fit items-center gap-2 rounded-lg text-sm font-medium text-foreground"
        >
          <span
            className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent-subtle font-mono text-xs text-accent"
            aria-hidden="true"
          >
            &gt;_
          </span>
          {branding.appName}
        </Link>
        <section
          aria-labelledby="join-team-heading"
          className="overflow-hidden rounded-2xl border border-border bg-surface shadow-card"
        >
          <div className="px-6 pt-7 sm:px-8 sm:pt-8">
            <span
              className={`mb-4 flex h-12 w-12 items-center justify-center rounded-2xl ${kind === 'approved' ? 'bg-status-success/10 text-status-success' : kind === 'closed' ? 'bg-surface-muted text-muted' : 'bg-accent-subtle text-accent'}`}
            >
              <TeamJoinIcon kind={kind} />
            </span>
            <p className="mb-1.5 text-xs font-medium text-muted">{t('teams.join.eyebrow')}</p>
            <h1
              id="join-team-heading"
              className="text-2xl font-semibold tracking-tight text-foreground"
            >
              {heading}
            </h1>
          </div>
          <div className="flex flex-col gap-4 px-6 pb-7 pt-5 sm:px-8 sm:pb-8" aria-busy={busy}>
            {body}
            {error ? (
              <p
                role="alert"
                className="rounded-xl bg-status-error/5 px-4 py-3 text-sm leading-6 text-status-error"
              >
                {error}
              </p>
            ) : null}
          </div>
        </section>
        {request?.state === 'pending' ? (
          <p className="mt-5 text-center text-xs leading-5 text-muted">
            {t('teams.join.closeHint')}
          </p>
        ) : null}
      </div>
    </main>
  );
}

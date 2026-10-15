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

const JOIN_PATH = '/teams/join';

export function JoinTeamView() {
  const t = useT();
  const { locale } = useLocale();
  const { state } = useAuth();
  // Undefined until the effect below has moved the fragment into storage.
  const [token, setToken] = useState<string | null | undefined>(undefined);
  const [request, setRequest] = useState<OwnJoinRequest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [alreadyMember, setAlreadyMember] = useState(false);
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
      if (cause instanceof APIError && cause.statusCode === 403) {
        setError(t('teams.join.noAccess'));
      } else {
        setError(teamErrorMessage(t, cause));
      }
      // A refusal about the link itself is final; a transient one keeps it for a retry.
      if (cause instanceof APIError && (cause.statusCode === 410 || cause.statusCode === 409)) {
        clearInvitationToken();
        setToken(null);
        setAlreadyMember(cause.code === 'already_member');
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

  let body: React.ReactNode;
  if (request) {
    body = (
      <>
        <p className="text-sm text-foreground">
          {t('teams.join.requested', {
            team: request.team_name,
            inviter: request.inviter_display_name || t('teams.join.theOwner'),
          })}
        </p>
        <p role="status" className="text-sm text-muted">
          {t(`teams.join.state.${request.state}`, {
            expires: formatTeamTime(locale, request.expires_at),
          })}
        </p>
        <div className="flex flex-wrap gap-2">
          {request.state === 'pending' ? (
            <button
              type="button"
              className="ui-button ui-button-secondary ui-focus-ring min-h-9 rounded-lg"
              disabled={busy}
              onClick={() => void checkAgain()}
            >
              {t('teams.join.checkAgain')}
            </button>
          ) : null}
          <Link href={SETTINGS_PATHS.teams} className="ui-button ui-button-ghost min-h-9">
            {t('teams.join.openTeams')}
          </Link>
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
        <p className="text-sm text-muted">
          {alreadyMember ? t('teams.error.alreadyMember') : t('teams.join.noLink')}
        </p>
        <Link href={SETTINGS_PATHS.teams} className="ui-button ui-button-ghost min-h-9 self-start">
          {t('teams.join.openTeams')}
        </Link>
      </>
    );
  } else if (!state.isAuthenticated) {
    body = (
      <>
        <p className="text-sm text-muted">{t('teams.join.signInFirst')}</p>
        <button
          type="button"
          className="ui-button ui-button-primary ui-focus-ring min-h-9 self-start rounded-lg"
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
        <p className="text-sm text-muted">{t('teams.join.explain')}</p>
        <button
          type="button"
          className="ui-button ui-button-primary ui-focus-ring min-h-9 self-start rounded-lg"
          disabled={busy}
          onClick={() => void claim()}
        >
          {busy ? t('teams.join.requesting') : t('teams.join.request')}
        </button>
      </>
    );
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-surface-muted px-4 py-10">
      <section
        aria-labelledby="join-team-heading"
        className="flex w-full max-w-md flex-col gap-4 rounded-2xl border border-border bg-surface p-6"
      >
        <h1 id="join-team-heading" className="text-lg font-semibold text-foreground">
          {t('teams.join.title')}
        </h1>
        {body}
        {error ? (
          <p role="alert" className="text-sm text-status-error">
            {error}
          </p>
        ) : null}
      </section>
    </main>
  );
}

'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth, useLocale, useT } from '@/components/providers';
import { SettingsPage } from '@/components/features/settings/SettingsPage';
import {
  createTeam,
  listOwnJoinRequests,
  listTeams,
  newRequestKey,
  type OwnJoinRequest,
  type Team,
} from '@/lib/api/teams';
import { TeamDetails } from './TeamDetails';
import { CARD, INPUT, SECONDARY, formatTeamTime, teamErrorMessage } from './teamUi';

export function TeamsSettingsView() {
  const t = useT();
  const { state } = useAuth();
  return (
    <SettingsPage title={t('teams.title')} description={t('teams.description')}>
      {state.user?.id ? (
        <Teams key={state.user.id} />
      ) : (
        <p className="mt-6 text-sm text-muted">{t('teams.signIn')}</p>
      )}
    </SettingsPage>
  );
}

function Teams() {
  const t = useT();
  const { locale } = useLocale();
  const [teams, setTeams] = useState<Team[] | null>(null);
  const [requests, setRequests] = useState<OwnJoinRequest[]>([]);
  const [failed, setFailed] = useState(false);
  const [reload, setReload] = useState(0);
  const [openTeamId, setOpenTeamId] = useState<string | null>(null);
  const refresh = useCallback(() => setReload((value) => value + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    setFailed(false);
    void Promise.all([listTeams(controller.signal), listOwnJoinRequests(controller.signal)])
      .then(([nextTeams, nextRequests]) => {
        if (controller.signal.aborted) return;
        setTeams(nextTeams);
        setRequests(nextRequests);
        setOpenTeamId((current) =>
          current && nextTeams.some((team) => team.id === current)
            ? current
            : nextTeams.length === 1
              ? nextTeams[0].id
              : null,
        );
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailed(true);
      });
    return () => controller.abort();
  }, [reload]);

  const pending = requests.filter((request) => request.state === 'pending');

  return (
    <div className="mt-6 space-y-6">
      {pending.length ? (
        <section aria-labelledby="teams-pending-heading" className={`${CARD} px-4 py-4 sm:px-6`}>
          <h2 id="teams-pending-heading" className="text-sm font-medium text-foreground">
            {t('teams.pendingTitle')}
          </h2>
          <ul className="mt-2 space-y-1">
            {pending.map((request) => (
              <li key={request.id} className="text-sm text-muted">
                {t('teams.pendingItem', {
                  team: request.team_name,
                  expires: formatTeamTime(locale, request.expires_at),
                })}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <CreateTeam
        onCreated={(team) => {
          setOpenTeamId(team.id);
          refresh();
        }}
      />

      {failed ? (
        <div role="alert" className="flex flex-wrap items-center gap-3 text-sm text-status-error">
          {t('teams.loadFailed')}
          <button type="button" className={SECONDARY} onClick={refresh}>
            {t('teams.retry')}
          </button>
        </div>
      ) : teams === null ? (
        <p role="status" className="text-sm text-muted">
          {t('teams.loading')}
        </p>
      ) : teams.length === 0 ? (
        <p className="text-sm text-muted">{t('teams.empty')}</p>
      ) : (
        <ul className="space-y-3" aria-label={t('teams.listLabel')}>
          {teams.map((team) => {
            const open = openTeamId === team.id;
            return (
              <li key={team.id} className={CARD}>
                <button
                  type="button"
                  aria-expanded={open}
                  aria-controls={`team-${team.id}`}
                  onClick={() => setOpenTeamId(open ? null : team.id)}
                  className="ui-focus-ring flex w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-2xl px-4 py-4 text-left sm:px-6"
                >
                  <span className="min-w-0 flex-1 truncate text-base font-medium text-foreground">
                    {team.name}
                  </span>
                  <span className="rounded-full bg-surface-muted px-2 py-0.5 text-xs text-muted">
                    {t(`teams.role.${team.role}`)}
                  </span>
                  <span className="text-xs text-muted">
                    {t('teams.memberCount', { count: team.member_count })}
                  </span>
                </button>
                {open ? (
                  <div id={`team-${team.id}`} className="border-t border-border px-4 pb-5 sm:px-6">
                    <TeamDetails team={team} onChanged={refresh} />
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function CreateTeam({ onCreated }: { onCreated: (team: Team) => void }) {
  const t = useT();
  const [name, setName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // One key per intended team: a retry after a lost response finds the first
  // create instead of making a second team.
  const requestKey = useRef(newRequestKey());

  return (
    <form
      className={`${CARD} flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-end sm:px-6`}
      aria-busy={saving}
      onSubmit={async (event) => {
        event.preventDefault();
        const trimmed = name.trim();
        if (!trimmed || saving) return;
        setSaving(true);
        setError(null);
        try {
          const team = await createTeam(trimmed, requestKey.current);
          requestKey.current = newRequestKey();
          setName('');
          onCreated(team);
        } catch (cause) {
          setError(teamErrorMessage(t, cause));
        } finally {
          setSaving(false);
        }
      }}
    >
      <label className="min-w-0 flex-1 text-sm text-foreground">
        {t('teams.create.label')}
        <input
          className={`${INPUT} mt-1`}
          value={name}
          maxLength={80}
          placeholder={t('teams.create.placeholder')}
          onChange={(event) => {
            setName(event.target.value);
            setError(null);
          }}
          disabled={saving}
        />
      </label>
      <button type="submit" className={SECONDARY} disabled={saving || !name.trim()}>
        {saving ? t('teams.create.saving') : t('teams.create.submit')}
      </button>
      {error ? (
        <p role="alert" className="w-full text-sm text-status-error">
          {error}
        </p>
      ) : null}
    </form>
  );
}

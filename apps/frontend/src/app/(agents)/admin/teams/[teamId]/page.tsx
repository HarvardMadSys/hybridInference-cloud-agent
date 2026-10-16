'use client';

// Admin → Teams → one team: its members past and present, the requests
// waiting on its owner, its invitation links and its audit history. Read-only.

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';

import {
  getAdminTeam,
  listAdminTeamAudit,
  personLabel,
  type AdminTeamAuditEvent,
  type AdminTeamDetail,
  type AdminTeamPerson,
} from '@/lib/api/adminTeams';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../../AdminNavigation';
import { describeTeamEvent } from '../describe';
import { ADMIN_PAGE } from '../../AdminPageHeader';
import {
  accessDenied,
  AdminRequired,
  BUTTON,
  CARD,
  ErrorNotice,
  dateTime,
} from '../../runs/shared';

const ROLE_LABELS = { owner: 'Owner', admin: 'Admin', member: 'Member' } as const;
const STATE_LABELS = { active: 'Member', left: 'Left', removed: 'Removed' } as const;

function Person({ person }: { person: AdminTeamPerson }) {
  const label = personLabel(person);
  // Whatever the label did not already say: the email, then the account id.
  const detail = [person.email, person.user_id].filter((part) => part && part !== label);
  return (
    <div className="flex min-w-0 items-center gap-3">
      {/* Generated from the account id, never a user upload. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img src={person.avatar_data_url} alt="" className="h-8 w-8 shrink-0 rounded-full" />
      <div className="min-w-0">
        <div className="truncate text-foreground">{label}</div>
        {detail.length > 0 && (
          <div className="truncate font-mono text-xs text-muted">{detail.join(' · ')}</div>
        )}
      </div>
    </div>
  );
}

export default function AdminTeamPage() {
  const { teamId } = useParams<{ teamId: string }>();
  const [detail, setDetail] = useState<AdminTeamDetail | null>(null);
  const [events, setEvents] = useState<AdminTeamAuditEvent[] | null>(null);
  const [nextBefore, setNextBefore] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  const [denied, setDenied] = useState(false);
  const request = useRef<AbortController | null>(null);

  const fail = useCallback((cause: unknown) => {
    if (accessDenied(cause)) setDenied(true);
    else if ((cause as { statusCode?: number }).statusCode === 404) setMissing(true);
    else setError(getErrorMessage(cause));
  }, []);

  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setError(null);
    try {
      const [team, audit] = await Promise.all([
        getAdminTeam(teamId, controller.signal),
        listAdminTeamAudit(teamId, null, controller.signal),
      ]);
      if (controller.signal.aborted) return;
      setDetail(team);
      setEvents(audit.events);
      setNextBefore(audit.next_before);
    } catch (cause) {
      if (!controller.signal.aborted) fail(cause);
    }
  }, [teamId, fail]);

  useEffect(() => {
    void load();
    return () => request.current?.abort();
  }, [load]);

  async function showOlder(before: number) {
    setLoadingMore(true);
    try {
      const page = await listAdminTeamAudit(teamId, before);
      setEvents((current) => [...(current ?? []), ...page.events]);
      setNextBefore(page.next_before);
    } catch (cause) {
      fail(cause);
    } finally {
      setLoadingMore(false);
    }
  }

  if (denied) return <AdminRequired />;

  const team = detail?.team;
  const active = detail?.members.filter((member) => member.state === 'active') ?? [];
  const departed = detail?.members.filter((member) => member.state !== 'active') ?? [];

  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <header>
          <h1 className="text-[11px] font-medium uppercase tracking-[0.12em] text-muted">
            <Link href="/admin/teams" className="hover:underline">
              Admin · Teams
            </Link>
          </h1>
          <p className="text-lg font-semibold tracking-tight text-foreground">
            {team?.name ?? (missing ? 'Team not found' : 'Team')}
            {team?.state === 'deleted' && (
              <span className="ml-2 rounded-full bg-surface-muted px-2 py-0.5 align-middle text-xs font-normal text-muted">
                Deleted {dateTime(team.deleted_at)}
              </span>
            )}
          </p>
          <p className="mt-0.5 font-mono text-[13px] text-muted">{teamId}</p>
        </header>
        <AdminNavigation current="teams" />

        {error && (
          <div className="mt-6">
            <ErrorNotice>
              {error}{' '}
              <button type="button" className="font-medium underline" onClick={() => void load()}>
                Retry
              </button>
            </ErrorNotice>
          </div>
        )}
        {missing && <p className="mt-8 text-sm text-muted">There is no team with this id.</p>}
        {!detail && !missing && !error && (
          <p role="status" className="mt-8 text-sm text-muted">
            Loading…
          </p>
        )}

        {detail && team && (
          <div className="mt-6 grid gap-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
            <div className="min-w-0 space-y-6">
              <section aria-labelledby="team-members" className={`${CARD} overflow-x-auto`}>
                <h2 id="team-members" className="px-4 pt-4 text-sm font-semibold text-foreground">
                  Members ({active.length})
                </h2>
                <table className="mt-2 w-full min-w-[560px] text-left text-sm">
                  <thead className="border-b border-border text-xs text-muted">
                    <tr>
                      <th scope="col" className="px-4 py-2 font-medium">
                        Person
                      </th>
                      <th scope="col" className="px-4 py-2 font-medium">
                        Role
                      </th>
                      <th scope="col" className="px-4 py-2 font-medium">
                        Joined
                      </th>
                      <th scope="col" className="px-4 py-2 font-medium">
                        Status
                      </th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-border">
                    {[...active, ...departed].map((member) => (
                      <tr key={member.user_id}>
                        <td className="px-4 py-3">
                          <Person person={member} />
                        </td>
                        <td className="px-4 py-3">{ROLE_LABELS[member.role]}</td>
                        <td className="px-4 py-3 text-xs text-muted">
                          {dateTime(member.joined_at)}
                        </td>
                        <td className="px-4 py-3 text-xs">
                          {member.state === 'active' ? (
                            <span className="text-foreground">{STATE_LABELS.active}</span>
                          ) : (
                            <span className="text-muted">
                              {STATE_LABELS[member.state]} {dateTime(member.departed_at)}
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>

              <section aria-labelledby="team-history" className={CARD}>
                <h2 id="team-history" className="px-4 pt-4 text-sm font-semibold text-foreground">
                  History
                </h2>
                {events && events.length > 0 ? (
                  <ul className="mt-2 divide-y divide-border">
                    {events.map((event) => (
                      <li
                        key={event.id}
                        className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4 py-3 text-sm"
                      >
                        <span className="font-medium text-foreground">
                          {personLabel(event.actor)}
                        </span>
                        <span className="min-w-0 flex-1 text-foreground">
                          {describeTeamEvent(event)}
                        </span>
                        <time dateTime={event.created_at} className="text-xs text-muted">
                          {dateTime(event.created_at)}
                        </time>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="px-4 py-3 text-sm text-muted">Nothing recorded yet.</p>
                )}
                {nextBefore !== null && (
                  <div className="px-4 pb-4">
                    <button
                      type="button"
                      disabled={loadingMore}
                      onClick={() => void showOlder(nextBefore)}
                      className={BUTTON}
                    >
                      {loadingMore ? 'Loading…' : 'Show older'}
                    </button>
                  </div>
                )}
              </section>
            </div>

            <div className="min-w-0 space-y-6">
              <section aria-labelledby="team-summary" className={`${CARD} p-4`}>
                <h2 id="team-summary" className="text-sm font-semibold text-foreground">
                  Summary
                </h2>
                <dl className="mt-3 grid grid-cols-[7rem_1fr] gap-x-3 gap-y-2 text-sm">
                  <dt className="text-muted">Owner</dt>
                  <dd className="min-w-0">
                    <Person person={team.owner} />
                  </dd>
                  <dt className="text-muted">Created</dt>
                  <dd>{dateTime(team.created_at)}</dd>
                  <dt className="text-muted">Admins</dt>
                  <dd className="tabular-nums">{team.admin_count}</dd>
                  <dt className="text-muted">Links</dt>
                  <dd className="text-xs text-muted">
                    {detail.invitations.open} open · {detail.invitations.claimed} claimed ·{' '}
                    {detail.invitations.expired} expired · {detail.invitations.revoked} revoked
                  </dd>
                </dl>
              </section>

              <section aria-labelledby="team-requests" className={CARD}>
                <h2 id="team-requests" className="px-4 pt-4 text-sm font-semibold text-foreground">
                  Waiting to join ({detail.join_requests.length})
                </h2>
                {detail.join_requests.length ? (
                  <ul className="mt-2 divide-y divide-border">
                    {detail.join_requests.map((request) => (
                      <li key={request.id} className="px-4 py-3 text-sm">
                        <Person person={request} />
                        <p className="mt-1 text-xs text-muted">
                          Asked {dateTime(request.created_at)} · expires{' '}
                          {dateTime(request.expires_at)}
                        </p>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="px-4 py-3 text-sm text-muted">No open requests.</p>
                )}
              </section>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

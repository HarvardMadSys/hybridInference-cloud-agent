'use client';

// Admin → Teams: every team on the platform, newest first.
//
// Read-only. Owners and admins manage their teams from Settings; this page is
// for answering "which teams are there, and who is in them" across all of
// them, deleted teams included.

import Link from 'next/link';
import { useCallback, useEffect, useRef, useState } from 'react';

import {
  listAdminTeams,
  personLabel,
  type AdminTeamStateFilter,
  type AdminTeamSummary,
} from '@/lib/api/adminTeams';
import { getErrorMessage } from '@/lib/utils/errors';

import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import {
  accessDenied,
  AdminRequired,
  BUTTON,
  CARD,
  ErrorNotice,
  INPUT,
  dateTime,
} from '../runs/shared';

const STATES: { key: AdminTeamStateFilter; label: string }[] = [
  { key: 'active', label: 'Active' },
  { key: 'deleted', label: 'Deleted' },
  { key: 'all', label: 'All' },
];

/** What an empty list says. Under Active or Deleted, the other state may still hold teams. */
function nothingFound(state: AdminTeamStateFilter, query: string): string {
  const kind = state === 'all' ? '' : `${state} `;
  if (query) return `No ${kind}team matches “${query}”.`;
  return state === 'all' ? 'No teams yet.' : `No ${kind}teams.`;
}

export default function AdminTeamsPage() {
  const [draft, setDraft] = useState('');
  const [query, setQuery] = useState('');
  const [state, setState] = useState<AdminTeamStateFilter>('active');
  const [teams, setTeams] = useState<AdminTeamSummary[] | null>(null);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [denied, setDenied] = useState(false);
  const request = useRef<AbortController | null>(null);

  const load = useCallback(
    async (before: string | null) => {
      request.current?.abort();
      const controller = new AbortController();
      request.current = controller;
      setError(null);
      if (before === null) {
        // A cursor belongs to the list it came from; a new first page drops it.
        setTeams(null);
        setNextBefore(null);
      } else {
        setLoadingMore(true);
      }
      try {
        const page = await listAdminTeams({ q: query, state, before }, controller.signal);
        if (controller.signal.aborted) return;
        setTeams((current) => (before === null ? page.teams : [...(current ?? []), ...page.teams]));
        setNextBefore(page.next_before);
      } catch (cause) {
        if (controller.signal.aborted) return;
        if (accessDenied(cause)) setDenied(true);
        else setError(getErrorMessage(cause));
        if (before === null) setTeams([]);
      } finally {
        if (!controller.signal.aborted) setLoadingMore(false);
      }
    },
    [query, state],
  );

  useEffect(() => {
    void load(null);
    return () => request.current?.abort();
  }, [load]);

  if (denied) return <AdminRequired />;

  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section="Teams"
          description="Every team on the platform, who is in it, and what changed. Owners and admins manage their teams from Settings."
        />
        <AdminNavigation current="teams" />

        <div className="mt-6 flex flex-wrap items-center gap-3">
          <form
            role="search"
            className="flex min-w-0 flex-1 gap-2 sm:max-w-md"
            onSubmit={(event) => {
              event.preventDefault();
              setQuery(draft.trim());
            }}
          >
            <label className="sr-only" htmlFor="admin-team-search">
              Search teams
            </label>
            <input
              id="admin-team-search"
              type="search"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="Team name, team id or member user id"
              maxLength={256}
              className={INPUT}
            />
            <button type="submit" className={BUTTON}>
              Search
            </button>
          </form>
          <div
            role="group"
            aria-label="Show"
            className="inline-flex gap-1 rounded-xl border border-border bg-surface-muted p-1"
          >
            {STATES.map((item) => (
              <button
                key={item.key}
                type="button"
                aria-pressed={item.key === state}
                onClick={() => setState(item.key)}
                className={`ui-focus-ring min-h-9 rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
                  item.key === state
                    ? 'bg-surface text-foreground shadow-subtle'
                    : 'text-muted hover:bg-surface-hover hover:text-foreground'
                }`}
              >
                {item.label}
              </button>
            ))}
          </div>
        </div>

        {error && (
          <div className="mt-6">
            <ErrorNotice>
              {error}{' '}
              <button
                type="button"
                className="font-medium underline"
                onClick={() => void load(null)}
              >
                Retry
              </button>
            </ErrorNotice>
          </div>
        )}

        {teams === null ? (
          <p role="status" className="mt-8 text-sm text-muted">
            Loading…
          </p>
        ) : teams.length === 0 ? (
          !error && <p className="mt-8 text-sm text-muted">{nothingFound(state, query)}</p>
        ) : (
          <div className={`${CARD} mt-6 overflow-x-auto`}>
            <table className="w-full min-w-[720px] text-left text-sm">
              <thead className="border-b border-border text-xs text-muted">
                <tr>
                  <th scope="col" className="px-4 py-3 font-medium">
                    Team
                  </th>
                  <th scope="col" className="px-4 py-3 font-medium">
                    Owner
                  </th>
                  <th scope="col" className="px-4 py-3 text-right font-medium">
                    Members
                  </th>
                  <th scope="col" className="px-4 py-3 text-right font-medium">
                    Admins
                  </th>
                  <th scope="col" className="px-4 py-3 text-right font-medium">
                    Waiting
                  </th>
                  <th scope="col" className="px-4 py-3 font-medium">
                    Created
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {teams.map((team) => (
                  <tr key={team.id} className="hover:bg-surface-hover/50">
                    <td className="px-4 py-3">
                      <Link
                        href={`/admin/teams/${encodeURIComponent(team.id)}`}
                        className="ui-focus-ring rounded font-medium text-foreground hover:underline"
                      >
                        {team.name}
                      </Link>
                      {team.state === 'deleted' && (
                        <span className="ml-2 rounded-full bg-surface-muted px-2 py-0.5 text-xs text-muted">
                          Deleted
                        </span>
                      )}
                      <div className="font-mono text-xs text-muted">{team.id}</div>
                    </td>
                    <td className="px-4 py-3">
                      <div className="text-foreground">{personLabel(team.owner)}</div>
                      {team.owner.email && team.owner.display_name !== team.owner.email && (
                        <div className="text-xs text-muted">{team.owner.email}</div>
                      )}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums">{team.member_count}</td>
                    <td className="px-4 py-3 text-right tabular-nums">{team.admin_count}</td>
                    <td className="px-4 py-3 text-right tabular-nums">
                      {team.pending_request_count}
                    </td>
                    <td className="px-4 py-3 text-xs text-muted">{dateTime(team.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {teams !== null && nextBefore !== null && (
          <button
            type="button"
            disabled={loadingMore}
            onClick={() => void load(nextBefore)}
            className={`${BUTTON} mt-4`}
          >
            {loadingMore ? 'Loading…' : 'Show more'}
          </button>
        )}
      </section>
    </div>
  );
}

'use client';
// The teams a deleted account still owns. A team belongs to its members too,
// so erasing the owner never deletes one that others use: an administrator
// hands it to one of them first, as the owner's own ownership transfer would.
// A team the account owns alone is deleted with it.
import { useCallback, useEffect, useState } from 'react';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import { listOwnedTeams, transferOwnedTeam, type OwnedTeam } from '@/lib/api/accountAdmin';
import { getErrorMessage } from '@/lib/utils/errors';

function TeamRow({
  accountId,
  team,
  onChanged,
}: {
  accountId: string;
  team: OwnedTeam;
  onChanged: (teams: OwnedTeam[]) => void;
}) {
  const [member, setMember] = useState(team.members[0]?.user_id ?? '');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function handOver() {
    setBusy(true);
    setError(null);
    try {
      onChanged((await transferOwnedTeam(accountId, team.id, member)).teams);
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <li className="space-y-2 rounded-lg border border-border p-3">
      <p className="text-sm font-medium break-all">
        {team.name} <span className="font-mono text-xs text-muted">{team.id}</span>
      </p>
      {team.members.length === 0 ? (
        <p className="text-xs text-muted">No other member: it is deleted with the account.</p>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <label className="text-sm" htmlFor={`team-owner-${team.id}`}>
            New owner
          </label>
          <select
            id={`team-owner-${team.id}`}
            className="ui-input"
            value={member}
            disabled={busy}
            onChange={(e) => setMember(e.target.value)}
          >
            {team.members.map((m) => (
              <option key={m.user_id} value={m.user_id}>
                {m.label ?? m.user_id}
                {m.role === 'admin' ? ' (team admin)' : ''}
              </option>
            ))}
          </select>
          <button
            className="ui-button ui-button-secondary"
            disabled={busy || !member}
            onClick={() => void handOver()}
          >
            Hand over
          </button>
        </div>
      )}
      {error && <AccountNotice error>{error}</AccountNotice>}
    </li>
  );
}

export function OwnedTeams({ accountId, reload }: { accountId: string; reload: number }) {
  const [teams, setTeams] = useState<OwnedTeam[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(
    async (signal?: AbortSignal) => {
      try {
        setTeams((await listOwnedTeams(accountId, signal)).teams);
      } catch (cause) {
        if (!signal?.aborted) setError(getErrorMessage(cause));
      }
    },
    [accountId],
  );
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, reload]);
  if (error) return <AccountNotice error>{error}</AccountNotice>;
  if (!teams || teams.length === 0) return null;
  return (
    <div className="space-y-2">
      <p className="text-sm font-medium">Teams it owns</p>
      <p className="text-xs text-muted">
        Hand each team other members use to one of them before erasing. The previous owner stays a
        member until the account is erased; the team’s open invitations are closed.
      </p>
      <ul className="space-y-2">
        {teams.map((team) => (
          <TeamRow key={team.id} accountId={accountId} team={team} onChanged={setTeams} />
        ))}
      </ul>
    </div>
  );
}

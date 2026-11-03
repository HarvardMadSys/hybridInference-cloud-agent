'use client';

// Admin → Users → People: one row per account, as HybridInference's users
// table. Review opens the account's decisions and records under its row; a
// team opens that team.

import Link from 'next/link';
import { Fragment, type ReactNode } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import type { AccountEntry, AccountTeam } from '@/lib/api/accounts';
import type { TranslationKey, Translator } from '@/lib/i18n/translate';
import { CARD } from '../runs/shared';
import { FILTERS, STATES, stateLabel } from './options';

const COLUMNS: TranslationKey[] = [
  'adminAccounts.column.user',
  'adminAccounts.column.access',
  'adminAccounts.column.account',
  'adminAccounts.column.teams',
  'adminAccounts.column.lastSignIn',
  'adminAccounts.column.joined',
];
const TEAM_ROLES: Partial<Record<AccountTeam['role'], TranslationKey>> = {
  owner: 'adminAccounts.team.owner',
  admin: 'adminAccounts.team.admin',
};
/** Teams shown in a row before the rest are counted. */
const TEAMS_SHOWN = 3;

function date(value: string | null | undefined, t: Translator): string {
  return value ? new Date(value).toLocaleDateString() : t('adminAccounts.never');
}

function UserCell({ account, open }: { account: AccountEntry; open: boolean }) {
  const t = useT();
  const identity = account.identity;
  const label = identity?.label ?? account.email ?? account.id;
  const details = [
    (identity?.identity_verified ?? account.email_verified)
      ? t('adminAccounts.identityVerified')
      : t('adminAccounts.emailUnverified'),
    identity?.login_name ? t('adminAccounts.username', { name: identity.login_name }) : '',
    identity?.phone ?? '',
  ].filter(Boolean);
  const purpose = account.waitlist.purpose;
  return (
    <div className="min-w-0">
      <p className="break-all font-medium text-foreground">
        {identity?.user_name ? `${identity.user_name} · ` : ''}
        {label}
      </p>
      <p className="mt-0.5 text-xs text-muted">{details.join(' · ')}</p>
      {/* Why they applied, in full once their row is open. */}
      {purpose && (
        <p
          className={`mt-1 max-w-md whitespace-pre-wrap break-words text-xs text-foreground ${open ? '' : 'line-clamp-2'}`}
        >
          {purpose}
        </p>
      )}
    </div>
  );
}

function AccessCell({ account }: { account: AccountEntry }) {
  const t = useT();
  return (
    <div className="space-y-1">
      <p>{stateLabel(FILTERS, account.waitlist.status, t)}</p>
      {account.is_admin && (
        <p className="inline-flex rounded-full bg-surface-muted px-2 py-0.5 text-xs text-foreground">
          {t('adminAccounts.administrator')}
        </p>
      )}
      {account.reviewed_by && (
        <p className="text-xs text-muted">
          {t('adminAccounts.reviewedBy', { actor: account.reviewed_by })}
        </p>
      )}
    </div>
  );
}

function TeamsCell({ teams }: { teams: AccountTeam[] }) {
  const t = useT();
  if (teams.length === 0) return <span className="text-muted">{t('adminAccounts.noTeams')}</span>;
  return (
    <ul className="flex max-w-[18rem] flex-wrap gap-1">
      {teams.slice(0, TEAMS_SHOWN).map((team) => {
        const role = TEAM_ROLES[team.role];
        return (
          <li key={team.id} className="min-w-0">
            <Link
              href={`/admin/users/teams/${encodeURIComponent(team.id)}`}
              aria-label={role ? `${team.name} (${t(role)})` : team.name}
              className="ui-focus-ring inline-flex max-w-full items-center gap-1 rounded-full border border-border px-2 py-0.5 text-xs text-foreground hover:bg-surface-hover"
            >
              <span className="truncate">{team.name}</span>
              {role && <span className="shrink-0 text-muted">{t(role)}</span>}
            </Link>
          </li>
        );
      })}
      {teams.length > TEAMS_SHOWN && (
        <li className="self-center text-xs text-muted">
          {t('adminAccounts.moreTeams', { count: teams.length - TEAMS_SHOWN })}
        </li>
      )}
    </ul>
  );
}

function UserRow({
  account,
  open,
  onToggle,
}: {
  account: AccountEntry;
  open: boolean;
  onToggle: () => void;
}) {
  const t = useT();
  return (
    <tr className={`align-top ${open ? 'bg-surface-hover/50' : 'hover:bg-surface-hover/50'}`}>
      <td className="px-4 py-3">
        <UserCell account={account} open={open} />
      </td>
      <td className="px-4 py-3">
        <AccessCell account={account} />
      </td>
      <td className={`px-4 py-3 ${account.status === 'active' ? '' : 'font-medium'}`}>
        {stateLabel(STATES, account.status, t)}
      </td>
      <td className="px-4 py-3">
        <TeamsCell teams={account.teams ?? []} />
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-xs text-muted">
        {date(account.last_login_at, t)}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-xs text-muted">
        {date(account.created_at, t)}
      </td>
      <td className="px-4 py-3 text-right">
        <button
          type="button"
          className="ui-button ui-button-secondary"
          aria-expanded={open}
          onClick={onToggle}
        >
          {t('adminAccounts.review')}
        </button>
      </td>
    </tr>
  );
}

export function UserTable({
  accounts,
  openId,
  onToggle,
  detail,
}: {
  accounts: AccountEntry[];
  /** The account whose row is open, if any. */
  openId: string | null;
  onToggle: (account: AccountEntry) => void;
  /** What an open row shows under it. */
  detail: (account: AccountEntry) => ReactNode;
}) {
  const t = useT();
  return (
    <div className={`${CARD} overflow-x-auto`}>
      <table className="w-full min-w-[960px] text-left text-sm">
        <thead className="border-b border-border text-xs text-muted">
          <tr>
            {COLUMNS.map((column) => (
              <th key={column} scope="col" className="whitespace-nowrap px-4 py-3 font-medium">
                {t(column)}
              </th>
            ))}
            <th scope="col" className="px-4 py-3">
              <span className="sr-only">{t('adminAccounts.review')}</span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {accounts.map((account) => {
            const open = account.id === openId;
            return (
              <Fragment key={account.id}>
                <UserRow account={account} open={open} onToggle={() => onToggle(account)} />
                {open && (
                  <tr>
                    <td colSpan={COLUMNS.length + 1} className="bg-surface-hover/50 px-4 pb-4">
                      {detail(account)}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

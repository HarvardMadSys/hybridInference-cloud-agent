'use client';

// Admin → Users → People: one row per account, as HybridInference's users
// table. A row carries what an administrator acts on; Review (an application
// waiting) or Details opens the rest of the account, and its decisions and
// records, under the row. A team opens that team.

import Link from 'next/link';
import { Fragment, type ReactNode } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import type { AccountEntry, AccountTeam } from '@/lib/api/accounts';
import type { TranslationKey, Translator } from '@/lib/i18n/translate';
import { FILTERS, STATES, stateLabel } from './options';

const COLUMNS: { label: TranslationKey; width: string }[] = [
  { label: 'adminAccounts.column.user', width: '' },
  { label: 'adminAccounts.column.access', width: 'w-48' },
  { label: 'adminAccounts.column.account', width: 'w-28' },
  { label: 'adminAccounts.column.teams', width: 'w-64' },
  { label: 'adminAccounts.column.lastSignIn', width: 'w-28' },
  { label: 'adminAccounts.column.joined', width: 'w-28' },
];
const TEAM_ROLES: Partial<Record<AccountTeam['role'], TranslationKey>> = {
  owner: 'adminAccounts.team.owner',
  admin: 'adminAccounts.team.admin',
};
/** How an account's state reads in its row: active stays quiet. */
const STATE_TONES: Record<AccountEntry['status'], string> = {
  active: 'text-muted',
  suspended: 'font-medium text-status-error',
  deleted: 'font-medium text-foreground',
};
/** Teams shown in a row before the rest are counted. */
const TEAMS_SHOWN = 3;
const CELL = 'px-3 py-2.5 first:pl-4 last:pr-4';

function date(value: string | null | undefined, t: Translator): string {
  return value ? new Date(value).toLocaleDateString() : t('adminAccounts.never');
}

function labelOf(account: AccountEntry): string {
  return account.identity?.label ?? account.email ?? account.id;
}

function verified(account: AccountEntry): boolean {
  return account.identity?.identity_verified ?? account.email_verified;
}

function waiting(account: AccountEntry): boolean {
  return account.waitlist.status === 'pending';
}

function UserCell({ account, open }: { account: AccountEntry; open: boolean }) {
  const t = useT();
  const label = labelOf(account);
  const name = account.identity?.user_name;
  // An application waiting shows why at a glance; its full text, and every
  // other application's, is in the open row.
  const purpose = waiting(account) && !open ? account.waitlist.purpose : null;
  return (
    <div className="min-w-0">
      <p className="truncate font-medium text-foreground" title={name ?? label}>
        {name ?? label}
      </p>
      {(name || !verified(account)) && (
        <p className="mt-0.5 flex min-w-0 items-center gap-2 text-xs text-muted">
          {name && (
            <span className="truncate" title={label}>
              {label}
            </span>
          )}
          {!verified(account) && (
            <span className="shrink-0 text-status-warning">
              {t('adminAccounts.emailUnverified')}
            </span>
          )}
        </p>
      )}
      {purpose && (
        <p className="mt-0.5 truncate text-xs text-muted" title={purpose}>
          {purpose}
        </p>
      )}
    </div>
  );
}

function AccessCell({ account }: { account: AccountEntry }) {
  const t = useT();
  const label = stateLabel(FILTERS, account.waitlist.status, t);
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {waiting(account) ? (
        <span className="inline-flex items-center gap-1.5 rounded-full border border-status-warning/30 bg-status-warning/10 px-2 py-0.5 text-xs font-medium text-status-warning">
          <span aria-hidden className="h-1.5 w-1.5 rounded-full bg-status-warning" />
          {label}
        </span>
      ) : (
        <span className={account.waitlist.status === 'approved' ? 'text-foreground' : 'text-muted'}>
          {label}
        </span>
      )}
      {account.is_admin && (
        <span className="rounded-full border border-border px-1.5 py-px text-[11px] text-muted">
          {t('adminAccounts.administrator')}
        </span>
      )}
    </div>
  );
}

function TeamsCell({ teams }: { teams: AccountTeam[] }) {
  const t = useT();
  if (teams.length === 0) return <span className="text-muted">{t('adminAccounts.noTeams')}</span>;
  return (
    <ul className="flex flex-wrap gap-1">
      {teams.slice(0, TEAMS_SHOWN).map((team) => {
        const role = TEAM_ROLES[team.role];
        return (
          <li key={team.id} className="min-w-0 max-w-[11rem]">
            <Link
              href={`/admin/users/teams/${encodeURIComponent(team.id)}`}
              aria-label={role ? `${team.name} (${t(role)})` : team.name}
              title={team.name}
              className="ui-focus-ring inline-flex max-w-full items-center gap-1 rounded-md border border-border bg-surface px-1.5 py-px text-xs text-foreground hover:bg-surface-hover"
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

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      className={`h-3.5 w-3.5 shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
    >
      <path d="m4 6 4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function ToggleButton({
  account,
  open,
  controls,
  onToggle,
}: {
  account: AccountEntry;
  open: boolean;
  controls: string;
  onToggle: () => void;
}) {
  const t = useT();
  const review = waiting(account);
  return (
    <button
      type="button"
      className={`ui-button inline-flex items-center gap-1 rounded-md text-[13px] font-medium ${
        review ? 'ui-button-secondary' : 'ui-button-ghost'
      }`}
      aria-expanded={open}
      aria-controls={open ? controls : undefined}
      aria-label={t(review ? 'adminAccounts.reviewOf' : 'adminAccounts.detailsOf', {
        label: labelOf(account),
      })}
      onClick={onToggle}
    >
      {t(review ? 'adminAccounts.review' : 'adminAccounts.details')}
      <Chevron open={open} />
    </button>
  );
}

function UserRow({
  account,
  open,
  controls,
  onToggle,
}: {
  account: AccountEntry;
  open: boolean;
  controls: string;
  onToggle: () => void;
}) {
  const t = useT();
  return (
    <tr className={open ? 'bg-surface-muted' : 'hover:bg-surface-hover/40'}>
      <td className={CELL}>
        <UserCell account={account} open={open} />
      </td>
      <td className={CELL}>
        <AccessCell account={account} />
      </td>
      <td className={`${CELL} ${STATE_TONES[account.status] ?? ''}`}>
        {stateLabel(STATES, account.status, t)}
      </td>
      <td className={CELL}>
        <TeamsCell teams={account.teams ?? []} />
      </td>
      <td className={`${CELL} whitespace-nowrap text-xs tabular-nums text-muted`}>
        {date(account.last_login_at, t)}
      </td>
      <td className={`${CELL} whitespace-nowrap text-xs tabular-nums text-muted`}>
        {date(account.created_at, t)}
      </td>
      <td className={`${CELL} text-right`}>
        <ToggleButton account={account} open={open} controls={controls} onToggle={onToggle} />
      </td>
    </tr>
  );
}

/** Who decided the application, and when, if anyone has. */
function reviewedBy(account: AccountEntry, t: Translator): string {
  const actor = account.reviewed_by;
  const at = account.waitlist.reviewed_at;
  if (!actor) return '';
  return at ? t('adminAccounts.fact.reviewedBy', { actor, date: date(at, t) }) : actor;
}

/** Who the account is and how it came in, in full: what its row leaves out. */
export function AccountFacts({ account }: { account: AccountEntry }) {
  const t = useT();
  const identity = account.identity;
  const reviewed = reviewedBy(account, t);
  const facts = (
    [
      ['adminAccounts.fact.email', account.email ?? identity?.email ?? ''],
      ['adminAccounts.fact.username', identity?.login_name ?? ''],
      ['adminAccounts.fact.phone', identity?.phone ?? ''],
      [
        'adminAccounts.fact.verification',
        t(verified(account) ? 'adminAccounts.identityVerified' : 'adminAccounts.emailUnverified'),
      ],
      ['adminAccounts.fact.reviewed', reviewed],
      ['adminAccounts.fact.accountId', account.id],
    ] as [TranslationKey, string][]
  ).filter(([, value]) => value);
  const notes = (
    [
      ['adminAccounts.fact.purpose', account.waitlist.purpose ?? ''],
      ['adminAccounts.fact.reason', account.waitlist.review_reason ?? ''],
    ] as [TranslationKey, string][]
  ).filter(([, value]) => value);
  return (
    <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
      {facts.map(([label, value]) => (
        <div key={label} className="min-w-0">
          <dt className="text-xs text-muted">{t(label)}</dt>
          <dd className="mt-0.5 break-words text-foreground">{value}</dd>
        </div>
      ))}
      {notes.map(([label, value]) => (
        <div key={label} className="min-w-0 sm:col-span-2 lg:col-span-3">
          <dt className="text-xs text-muted">{t(label)}</dt>
          <dd className="mt-0.5 whitespace-pre-wrap break-words text-foreground">{value}</dd>
        </div>
      ))}
    </dl>
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
    <div className="overflow-x-auto">
      <table className="w-full min-w-[1120px] table-fixed text-left text-sm">
        <thead className="border-b border-border bg-surface-muted/60 text-xs text-muted">
          <tr>
            {COLUMNS.map((column) => (
              <th
                key={column.label}
                scope="col"
                className={`${CELL} ${column.width} whitespace-nowrap py-2 font-medium`}
              >
                {t(column.label)}
              </th>
            ))}
            <th scope="col" className={`${CELL} w-32 py-2`}>
              <span className="sr-only">{t('adminAccounts.column.actions')}</span>
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border">
          {accounts.map((account) => {
            const open = account.id === openId;
            const controls = `account-detail-${account.id}`;
            return (
              <Fragment key={account.id}>
                <UserRow
                  account={account}
                  open={open}
                  controls={controls}
                  onToggle={() => onToggle(account)}
                />
                {open && (
                  <tr id={controls} className="!border-t-0 bg-surface-muted">
                    <td colSpan={COLUMNS.length + 1} className="px-4 pb-4 pt-1">
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

'use client';

// Admin → Users → People: one row per account, as HybridInference's users
// table. The identity column stays in view when the table scrolls sideways,
// and its chevron (or a click anywhere on the row) opens the rest of the
// account, and its decisions and records, under the row. An application
// waiting also offers Review; a team opens that team.

import Link from 'next/link';
import { Fragment, type MouseEvent, type ReactNode } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import type { AccountEntry, AccountTeam } from '@/lib/api/accounts';
import type { TranslationKey, Translator } from '@/lib/i18n/translate';
import type { Density } from './density';
import { FILTERS, STATES, stateLabel } from './options';

const COLUMNS: TranslationKey[] = [
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

type Tone = 'info' | 'warning' | 'error' | 'neutral';
/** A pill per tone, over the theme's status colors so dark mode follows. */
const PILLS: Record<Tone, { pill: string; dot: string }> = {
  info: { pill: 'bg-status-info/10 text-status-info ring-status-info/25', dot: 'bg-status-info' },
  warning: {
    pill: 'bg-status-warning/10 text-status-warning ring-status-warning/25',
    dot: 'bg-status-warning',
  },
  error: {
    pill: 'bg-status-error/10 text-status-error ring-status-error/25',
    dot: 'bg-status-error',
  },
  neutral: { pill: 'bg-surface-muted text-muted ring-border', dot: 'bg-border-strong' },
};
/**
 * The access an application has led to. Approved is the normal state and stays
 * plain; the others are pills, waiting the one that asks for something.
 */
const ACCESS_TONES: Partial<Record<string, Tone>> = {
  pending: 'info',
  rejected: 'error',
  revoked: 'error',
  not_applied: 'neutral',
};
/** The account's own state; active is the normal one and stays plain. */
const STATE_TONES: Partial<Record<string, Tone>> = {
  suspended: 'warning',
  deleted: 'neutral',
};
/** The interactive parts of a row, whose clicks are their own and not the row's. */
const OWN_CLICKS = 'a, button, input, textarea, select, label, [role="combobox"]';
const CELL = 'whitespace-nowrap px-2.5 py-1.5';
const DATE = `${CELL} text-[12px] tabular-nums text-muted`;

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

function StatusPill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ${PILLS[tone].pill}`}
    >
      <span aria-hidden className={`h-1.5 w-1.5 rounded-full ${PILLS[tone].dot}`} />
      {children}
    </span>
  );
}

/** A state as a pill when it has a tone, as quiet text when it is the normal one. */
function Status({ tone, label }: { tone: Tone | undefined; label: string }) {
  if (tone) return <StatusPill tone={tone}>{label}</StatusPill>;
  return <span className="text-[13px] text-muted">{label}</span>;
}

function Chevron({ open, className }: { open: boolean; className: string }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 20 20"
      fill="currentColor"
      className={`h-3.5 w-3.5 shrink-0 transition-transform ${open ? 'rotate-90' : ''} ${className}`}
    >
      <path
        fillRule="evenodd"
        d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function Unverified({ compact }: { compact: boolean }) {
  const t = useT();
  const label = t('adminAccounts.emailUnverified');
  if (!compact) return <span className="shrink-0 text-status-warning">{label}</span>;
  return (
    <span className="inline-flex shrink-0 text-status-warning" title={label}>
      <svg aria-hidden="true" viewBox="0 0 20 20" fill="currentColor" className="h-3.5 w-3.5">
        <path
          fillRule="evenodd"
          d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 6a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 6zm0 9a1 1 0 100-2 1 1 0 000 2z"
          clipRule="evenodd"
        />
      </svg>
      <span className="sr-only">{label}</span>
    </span>
  );
}

function Identity({
  account,
  open,
  density,
  controls,
  onToggle,
}: {
  account: AccountEntry;
  open: boolean;
  density: Density;
  controls: string;
  onToggle: () => void;
}) {
  const t = useT();
  const label = labelOf(account);
  const name = account.identity?.user_name;
  const compact = density === 'compact';
  const flagged = !verified(account);
  return (
    <div className="flex min-w-0 max-w-[10.5rem] items-center gap-1.5 sm:max-w-[16rem] lg:max-w-[22rem]">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={open ? controls : undefined}
        aria-label={t('adminAccounts.detailsOf', { label })}
        onClick={onToggle}
        className="ui-focus-ring -ml-1 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded text-muted hover:bg-surface-hover hover:text-foreground"
      >
        <Chevron open={open} className={open ? 'text-foreground' : ''} />
      </button>
      <div className="min-w-0">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="truncate text-[13px] font-medium text-foreground" title={name ?? label}>
            {name ?? label}
          </span>
          {account.is_admin && (
            <span className="shrink-0 rounded bg-foreground px-1.5 py-px text-[10px] font-semibold uppercase tracking-wide text-surface">
              {t('adminAccounts.adminBadge')}
            </span>
          )}
          {flagged && compact && <Unverified compact />}
        </div>
        {!compact && (name || flagged) && (
          <div className="flex min-w-0 items-center gap-2 text-[12px] text-muted">
            {name && (
              <span className="truncate" title={label}>
                {label}
              </span>
            )}
            {flagged && <Unverified compact={false} />}
          </div>
        )}
      </div>
    </div>
  );
}

function Access({
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
  const status = account.waitlist.status;
  return (
    <div className="inline-flex items-center gap-1.5">
      <Status tone={ACCESS_TONES[status]} label={stateLabel(FILTERS, status, t)} />
      {waiting(account) && (
        <button
          type="button"
          aria-expanded={open}
          aria-controls={open ? controls : undefined}
          aria-label={t('adminAccounts.reviewOf', { label: labelOf(account) })}
          onClick={onToggle}
          className="ui-focus-ring rounded-md border border-border-strong bg-surface px-2 py-0.5 text-[11px] font-semibold text-foreground transition-colors hover:bg-surface-hover"
        >
          {t('adminAccounts.review')}
        </button>
      )}
    </div>
  );
}

function TeamsCell({ teams, shown }: { teams: AccountTeam[]; shown: number }) {
  const t = useT();
  if (teams.length === 0)
    return <span className="text-[13px] text-muted">{t('adminAccounts.noTeams')}</span>;
  return (
    <ul className="flex items-center gap-1">
      {teams.slice(0, shown).map((team) => {
        const role = TEAM_ROLES[team.role];
        return (
          <li key={team.id} className="min-w-0 max-w-[9rem]">
            <Link
              href={`/admin/users/teams/${encodeURIComponent(team.id)}`}
              aria-label={role ? `${team.name} (${t(role)})` : team.name}
              title={team.name}
              className="ui-focus-ring inline-flex max-w-full items-center gap-1 rounded-md border border-border bg-surface px-1.5 py-px text-[12px] text-foreground hover:bg-surface-hover"
            >
              <span className="truncate">{team.name}</span>
              {role && <span className="shrink-0 text-muted">{t(role)}</span>}
            </Link>
          </li>
        );
      })}
      {teams.length > shown && (
        <li className="shrink-0 text-[12px] text-muted">
          {t('adminAccounts.moreTeams', { count: teams.length - shown })}
        </li>
      )}
    </ul>
  );
}

function UserRow({
  account,
  open,
  density,
  controls,
  onToggle,
}: {
  account: AccountEntry;
  open: boolean;
  density: Density;
  controls: string;
  onToggle: () => void;
}) {
  const t = useT();
  const compact = density === 'compact';
  // A click on the row opens it, as the chevron does; a link, a button or a
  // text selection keeps its own meaning.
  const click = (event: MouseEvent<HTMLTableRowElement>) => {
    if (event.target instanceof Element && event.target.closest(OWN_CLICKS)) return;
    if (window.getSelection()?.toString()) return;
    onToggle();
  };
  // The identity cell stays in view when the table scrolls, so it needs an
  // opaque background that follows the row's.
  const sticky = open ? 'bg-surface-muted' : 'bg-surface group-hover:bg-surface-hover';
  return (
    <tr
      onClick={click}
      className={`group cursor-pointer border-b border-border transition-colors ${
        compact ? 'h-10' : 'h-14'
      } ${open ? 'bg-surface-muted' : 'hover:bg-surface-hover'}`}
    >
      <td
        className={`sticky left-0 z-10 py-1.5 pl-3 pr-2.5 shadow-[inset_-1px_0_0_rgb(var(--ui-border))] ${sticky}`}
      >
        <Identity
          account={account}
          open={open}
          density={density}
          controls={controls}
          onToggle={onToggle}
        />
      </td>
      <td className={CELL}>
        <Access account={account} open={open} controls={controls} onToggle={onToggle} />
      </td>
      <td className={CELL}>
        <Status tone={STATE_TONES[account.status]} label={stateLabel(STATES, account.status, t)} />
      </td>
      <td className={CELL}>
        <TeamsCell teams={account.teams ?? []} shown={compact ? 2 : 3} />
      </td>
      <td className={DATE}>{date(account.last_login_at, t)}</td>
      <td className={`${DATE} pr-4`}>{date(account.created_at, t)}</td>
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

function teamList(account: AccountEntry, t: Translator): string {
  return (account.teams ?? [])
    .map((team) => {
      const role = TEAM_ROLES[team.role];
      return role ? `${team.name} (${t(role)})` : team.name;
    })
    .join(', ');
}

/** Who the account is and how it came in, in full: what its row leaves out. */
export function AccountFacts({ account }: { account: AccountEntry }) {
  const t = useT();
  const identity = account.identity;
  const facts = (
    [
      ['adminAccounts.fact.email', account.email ?? identity?.email ?? ''],
      ['adminAccounts.fact.username', identity?.login_name ?? ''],
      ['adminAccounts.fact.phone', identity?.phone ?? ''],
      [
        'adminAccounts.fact.verification',
        t(verified(account) ? 'adminAccounts.identityVerified' : 'adminAccounts.emailUnverified'),
      ],
      ['adminAccounts.fact.reviewed', reviewedBy(account, t)],
      ['adminAccounts.fact.accountId', account.id],
    ] as [TranslationKey, string][]
  ).filter(([, value]) => value);
  const notes = (
    [
      ['adminAccounts.fact.teams', teamList(account, t)],
      ['adminAccounts.fact.purpose', account.waitlist.purpose ?? ''],
      ['adminAccounts.fact.reason', account.waitlist.review_reason ?? ''],
    ] as [TranslationKey, string][]
  ).filter(([, value]) => value);
  return (
    <dl className="grid gap-x-6 gap-y-3 text-[13px] sm:grid-cols-2 lg:grid-cols-3">
      {facts.map(([label, value]) => (
        <div key={label} className="min-w-0">
          <dt className="text-[11px] font-medium uppercase tracking-wide text-muted">{t(label)}</dt>
          <dd className="mt-0.5 break-words text-foreground">{value}</dd>
        </div>
      ))}
      {notes.map(([label, value]) => (
        <div key={label} className="min-w-0 sm:col-span-2 lg:col-span-3">
          <dt className="text-[11px] font-medium uppercase tracking-wide text-muted">{t(label)}</dt>
          <dd className="mt-0.5 whitespace-pre-wrap break-words text-foreground">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function UserTable({
  accounts,
  openId,
  density,
  onToggle,
  detail,
}: {
  accounts: AccountEntry[];
  /** The account whose row is open, if any. */
  openId: string | null;
  density: Density;
  onToggle: (account: AccountEntry) => void;
  /** What an open row shows under it. */
  detail: (account: AccountEntry) => ReactNode;
}) {
  const t = useT();
  const head =
    'whitespace-nowrap px-2.5 py-2 text-left text-[11px] font-medium uppercase tracking-wide';
  return (
    // `container-type` lets an open row's detail size itself to the visible
    // width (100cqw) rather than the scrolled table's, so on a phone it can be
    // read and used without panning across every column.
    <div className="overflow-x-auto [container-type:inline-size]">
      <table className="min-w-full text-sm [&>tbody>tr:last-child]:border-b-0">
        <thead className="border-b border-border bg-surface-muted text-muted">
          <tr>
            <th
              scope="col"
              className={`${head} sticky left-0 z-10 bg-surface-muted pl-10 shadow-[inset_-1px_0_0_rgb(var(--ui-border))]`}
            >
              {t('adminAccounts.column.user')}
            </th>
            {COLUMNS.map((column) => (
              <th key={column} scope="col" className={`${head} last:pr-4`}>
                {t(column)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {accounts.map((account) => {
            const open = account.id === openId;
            const controls = `account-detail-${account.id}`;
            return (
              <Fragment key={account.id}>
                <UserRow
                  account={account}
                  open={open}
                  density={density}
                  controls={controls}
                  onToggle={() => onToggle(account)}
                />
                {open && (
                  <tr id={controls} className="border-b border-border">
                    <td colSpan={COLUMNS.length + 1} className="bg-surface-muted p-0">
                      <div className="sticky left-0 w-[100cqw] p-3 sm:p-4">{detail(account)}</div>
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

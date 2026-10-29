'use client';
import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { useAuth } from '@/components/providers/AuthProvider';
import { Select } from '@/components/ui/Select';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import {
  listAccounts,
  type AccountEntry,
  type AccountFilters,
  type WaitlistStatus,
} from '@/lib/api/accounts';
import { getErrorMessage } from '@/lib/utils/errors';
import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import { ActionEditor } from './AccountActions';
import { AccountNotes, SignInHistory } from './AccountDetails';

const FILTERS = [
  { value: 'pending', label: 'Waiting for review' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Declined' },
  { value: 'revoked', label: 'Access withdrawn' },
  { value: 'not_applied', label: 'Not applied' },
  { value: '', label: 'All accounts' },
] as const;
const STATES = [
  { value: '', label: 'Any state' },
  { value: 'active', label: 'Active' },
  { value: 'suspended', label: 'Suspended' },
  { value: 'deleted', label: 'Deleted' },
] as const;
const SORTS = [
  { value: 'created', label: 'Newest first' },
  { value: 'last_login', label: 'Recently signed in' },
] as const;

/** Wait until typing pauses before searching, as HybridInference's filter bar. */
function useDebounced(value: string, delay = 300): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return settled;
}

function identityLine(account: AccountEntry): string {
  const identity = account.identity;
  const parts = [
    account.status,
    account.waitlist.status.replaceAll('_', ' '),
    (identity?.identity_verified ?? account.email_verified)
      ? 'Identity verified'
      : 'Email unverified',
  ];
  if (identity?.login_name) parts.push(`username ${identity.login_name}`);
  if (identity?.phone) parts.push(identity.phone);
  if (account.is_admin) parts.push('Administrator');
  if (account.last_login_at)
    parts.push(`signed in ${new Date(account.last_login_at).toLocaleDateString()}`);
  if (account.reviewed_by) parts.push(`reviewed by ${account.reviewed_by}`);
  return parts.join(' · ');
}

function AccountRow({
  account,
  onReview,
  selected,
}: {
  account: AccountEntry;
  onReview: () => void;
  selected: boolean;
}) {
  const label = account.identity?.label ?? account.email ?? account.id;
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 flex-1">
        <p className="break-all font-medium">
          {account.identity?.user_name ? `${account.identity.user_name} · ` : ''}
          {label}
        </p>
        <p className="mt-1 text-xs text-muted">{identityLine(account)}</p>
        {account.waitlist.purpose && (
          <p className="mt-3 whitespace-pre-wrap break-words text-sm">{account.waitlist.purpose}</p>
        )}
        {account.waitlist.review_reason && (
          <p className="mt-2 whitespace-pre-wrap break-words text-sm text-muted">
            Review reason: {account.waitlist.review_reason}
          </p>
        )}
        {account.admin_note && (
          <p className="mt-2 whitespace-pre-wrap break-words text-xs text-muted">
            Note: {account.admin_note}
          </p>
        )}
      </div>
      <button
        className="ui-button ui-button-secondary shrink-0"
        disabled={selected}
        onClick={onReview}
      >
        Review
      </button>
    </div>
  );
}

function Counts({ counts }: { counts: Record<string, number> | undefined }) {
  if (!counts) return null;
  const shown: [string, string][] = [
    ['all', 'All'],
    ['admission:pending', 'Waiting'],
    ['admission:approved', 'Approved'],
    ['status:suspended', 'Suspended'],
    ['status:deleted', 'Deleted'],
  ];
  return (
    <dl className="flex flex-wrap gap-3 text-xs text-muted" aria-label="Account counts">
      {shown.map(([key, label]) => (
        <div key={key} className="rounded-lg border border-border px-3 py-1">
          <dt className="inline">{label}: </dt>
          <dd className="inline font-medium text-foreground">{counts[key] ?? 0}</dd>
        </div>
      ))}
    </dl>
  );
}

function useAccounts(
  local: boolean,
  status: WaitlistStatus | '',
  offset: number,
  filters: AccountFilters,
  reload: number,
  userId?: string,
) {
  const [accounts, setAccounts] = useState<AccountEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState<Record<string, number> | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const { accountStatus, q, sort } = filters;
  useEffect(() => {
    if (!local) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setAccounts([]);
    void listAccounts(status, offset, controller.signal, { accountStatus, q, sort })
      .then((result) => {
        if (controller.signal.aborted) return;
        setAccounts(result.accounts);
        setTotal(result.total);
        setCounts(result.counts);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(getErrorMessage(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [local, status, offset, accountStatus, q, sort, reload, userId]);
  return { accounts, total, counts, error, loading };
}

export default function AccountsPage() {
  const { state, refreshUser } = useAuth();
  const [status, setStatus] = useState<WaitlistStatus | ''>('pending');
  const [accountStatus, setAccountStatus] = useState<AccountFilters['accountStatus']>('');
  const [sort, setSort] = useState<'created' | 'last_login'>('created');
  const [search, setSearch] = useState('');
  const q = useDebounced(search);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<AccountEntry | null>(null);
  const [reload, setReload] = useState(0);
  const local = state.user?.auth_mode === 'local';
  const { accounts, total, counts, error, loading } = useAccounts(
    local,
    status,
    offset,
    { accountStatus, q, sort },
    reload,
    state.user?.id,
  );
  const refresh = useCallback(() => {
    setSelected(null);
    setReload((v) => v + 1);
  }, []);
  useEffect(() => setOffset(0), [q, accountStatus, sort]);
  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section="Accounts"
          description="Review Cloud Agent applications and manage independent accounts."
        />
        <AdminNavigation current="accounts" />
        {!local ? (
          <p className="mt-6 text-sm text-muted">
            Independent accounts are not enabled on this deployment.
          </p>
        ) : (
          <div className="mt-5 space-y-4">
            <nav className="flex flex-wrap gap-3 text-sm" aria-label="Account administration">
              <Link className="text-accent underline" href="/admin/accounts/policy">
                Sign-up policy and sign-in records
              </Link>
              <Link className="text-accent underline" href="/admin/accounts/mail">
                Mail to accounts
              </Link>
            </nav>
            <Counts counts={counts} />
            <div className="flex flex-wrap items-center gap-3">
              <input
                className="ui-input min-w-[14rem] flex-1"
                type="search"
                aria-label="Search accounts"
                placeholder="Email, username, phone, name or id"
                maxLength={320}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              <Select
                aria-label="Application status"
                value={status}
                onValueChange={(value) => {
                  setStatus(value);
                  setOffset(0);
                }}
                options={FILTERS}
              />
              <Select
                aria-label="Account state"
                value={accountStatus ?? ''}
                onValueChange={(value) => setAccountStatus(value)}
                options={STATES}
              />
              <Select
                aria-label="Sort"
                value={sort}
                onValueChange={(value) => setSort(value)}
                options={SORTS}
              />
              <button
                className="ui-button ui-button-secondary"
                disabled={loading}
                onClick={refresh}
              >
                Refresh
              </button>
              <span className="text-xs text-muted">{total} accounts</span>
            </div>
            {error && <AccountNotice error>{error}</AccountNotice>}
            {loading ? (
              <p role="status" className="text-sm text-muted">
                Loading accounts…
              </p>
            ) : accounts.length === 0 ? (
              <p className="text-sm text-muted">No accounts in this view.</p>
            ) : (
              <ul className="space-y-3">
                {accounts.map((account) => (
                  <li key={account.id} className="rounded-xl border border-border bg-surface p-4">
                    <AccountRow
                      account={account}
                      selected={selected?.id === account.id}
                      onReview={() => setSelected(account)}
                    />
                    {selected?.id === account.id && (
                      <>
                        <ActionEditor
                          key={`${account.id}:${account.waitlist.version}`}
                          account={selected}
                          onCancel={() => setSelected(null)}
                          onDone={() => {
                            refresh();
                            if (account.id === state.user?.id) void refreshUser();
                          }}
                        />
                        <div className="mt-3 space-y-3 rounded-lg border border-border p-4">
                          <AccountNotes account={selected} onSaved={refresh} />
                          <SignInHistory accountId={selected.id} />
                        </div>
                      </>
                    )}
                  </li>
                ))}
              </ul>
            )}
            <div className="flex gap-2">
              <button
                className="ui-button ui-button-secondary"
                disabled={loading || offset === 0}
                onClick={() => setOffset((v) => Math.max(0, v - 50))}
              >
                Previous
              </button>
              <button
                className="ui-button ui-button-secondary"
                disabled={loading || offset + 50 >= total}
                onClick={() => setOffset((v) => v + 50)}
              >
                Next
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

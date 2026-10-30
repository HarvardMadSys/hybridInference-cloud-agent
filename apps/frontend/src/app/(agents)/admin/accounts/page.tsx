'use client';
import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { useAuth } from '@/components/providers/AuthProvider';
import { useT } from '@/components/providers/LocaleProvider';
import { Select } from '@/components/ui/Select';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import {
  listAccounts,
  type AccountEntry,
  type AccountFilters,
  type WaitlistStatus,
} from '@/lib/api/accounts';
import type { ErasureResult } from '@/lib/api/accountAdmin';
import type { TranslationKey, Translator } from '@/lib/i18n/translate';
import { getErrorMessage } from '@/lib/utils/errors';
import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import { ActionEditor, ErasureSummary, SUMMARY } from './AccountActions';
import { AccountNotes, SignInHistory } from './AccountDetails';

const FILTERS = [
  { value: 'pending', label: 'adminAccounts.filter.pending' },
  { value: 'approved', label: 'adminAccounts.filter.approved' },
  { value: 'rejected', label: 'adminAccounts.filter.rejected' },
  { value: 'revoked', label: 'adminAccounts.filter.revoked' },
  { value: 'not_applied', label: 'adminAccounts.filter.notApplied' },
  { value: '', label: 'adminAccounts.filter.all' },
] as const;
const STATES = [
  { value: '', label: 'adminAccounts.state.any' },
  { value: 'active', label: 'adminAccounts.state.active' },
  { value: 'suspended', label: 'adminAccounts.state.suspended' },
  { value: 'deleted', label: 'adminAccounts.state.deleted' },
] as const;
const SORTS = [
  { value: 'created', label: 'adminAccounts.sort.created' },
  { value: 'last_login', label: 'adminAccounts.sort.lastLogin' },
] as const;

/** The options with their labels in the administrator's language. */
function translated<T extends { value: string; label: TranslationKey }>(
  options: readonly T[],
  t: Translator,
): { value: T['value']; label: string }[] {
  return options.map((option) => ({ value: option.value, label: t(option.label) }));
}

/** Wait until typing pauses before searching, as HybridInference's filter bar. */
function useDebounced(value: string, delay = 300): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return settled;
}

/** A state's own label, the one its filter shows; the code itself for one it does not know. */
function stateLabel(
  options: readonly { value: string; label: TranslationKey }[],
  value: string,
  t: Translator,
) {
  const option = options.find((candidate) => candidate.value === value);
  return option ? t(option.label) : value.replaceAll('_', ' ');
}

function identityLine(account: AccountEntry, t: Translator): string {
  const identity = account.identity;
  const parts = [
    stateLabel(STATES, account.status, t),
    stateLabel(FILTERS, account.waitlist.status, t),
    (identity?.identity_verified ?? account.email_verified)
      ? t('adminAccounts.identityVerified')
      : t('adminAccounts.emailUnverified'),
  ];
  if (identity?.login_name) parts.push(t('adminAccounts.username', { name: identity.login_name }));
  if (identity?.phone) parts.push(identity.phone);
  if (account.is_admin) parts.push(t('adminAccounts.administrator'));
  if (account.last_login_at)
    parts.push(
      t('adminAccounts.signedInOn', {
        date: new Date(account.last_login_at).toLocaleDateString(),
      }),
    );
  if (account.reviewed_by)
    parts.push(t('adminAccounts.reviewedBy', { actor: account.reviewed_by }));
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
  const t = useT();
  const label = account.identity?.label ?? account.email ?? account.id;
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 flex-1">
        <p className="break-all font-medium">
          {account.identity?.user_name ? `${account.identity.user_name} · ` : ''}
          {label}
        </p>
        <p className="mt-1 text-xs text-muted">{identityLine(account, t)}</p>
        {account.waitlist.purpose && (
          <p className="mt-3 whitespace-pre-wrap break-words text-sm">{account.waitlist.purpose}</p>
        )}
        {account.waitlist.review_reason && (
          <p className="mt-2 whitespace-pre-wrap break-words text-sm text-muted">
            {t('adminAccounts.reviewReason', { reason: account.waitlist.review_reason })}
          </p>
        )}
        {account.admin_note && (
          <p className="mt-2 whitespace-pre-wrap break-words text-xs text-muted">
            {t('adminAccounts.note', { note: account.admin_note })}
          </p>
        )}
      </div>
      <button
        className="ui-button ui-button-secondary shrink-0"
        disabled={selected}
        onClick={onReview}
      >
        {t('adminAccounts.review')}
      </button>
    </div>
  );
}

const COUNTS: [string, TranslationKey][] = [
  ['all', 'adminAccounts.count.all'],
  ['admission:pending', 'adminAccounts.count.waiting'],
  ['admission:approved', 'adminAccounts.count.approved'],
  ['status:suspended', 'adminAccounts.count.suspended'],
  ['status:deleted', 'adminAccounts.count.deleted'],
];

function Counts({ counts }: { counts: Record<string, number> | undefined }) {
  const t = useT();
  if (!counts) return null;
  return (
    <dl className="flex flex-wrap gap-3 text-xs text-muted" aria-label={t('adminAccounts.counts')}>
      {COUNTS.map(([key, label]) => (
        <div key={key} className="rounded-lg border border-border px-3 py-1">
          <dt className="inline">{t(label)}: </dt>
          <dd className="inline font-medium text-foreground">{counts[key] ?? 0}</dd>
        </div>
      ))}
    </dl>
  );
}

function useAccounts(
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
  }, [status, offset, accountStatus, q, sort, reload, userId]);
  return { accounts, total, counts, error, loading };
}

export default function AccountsPage() {
  const { state, refreshUser } = useAuth();
  const t = useT();
  const [status, setStatus] = useState<WaitlistStatus | ''>('pending');
  const [accountStatus, setAccountStatus] = useState<AccountFilters['accountStatus']>('');
  const [sort, setSort] = useState<'created' | 'last_login'>('created');
  const [search, setSearch] = useState('');
  const q = useDebounced(search);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<AccountEntry | null>(null);
  const [reload, setReload] = useState(0);
  // Kept at the page: the list refreshes and closes the editor after an
  // erasure, and the operator still needs its result and archive keys.
  const [erasure, setErasure] = useState<{ label: string; result: ErasureResult } | null>(null);
  const { accounts, total, counts, error, loading } = useAccounts(
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
          section={t('adminAccounts.section')}
          description={t('adminAccounts.description')}
        />
        <AdminNavigation current="accounts" />
        <div className="mt-5 space-y-4">
          <nav className="flex flex-wrap gap-3 text-sm" aria-label={t('adminAccounts.navigation')}>
            <Link className="text-accent underline" href="/admin/accounts/policy">
              {t('adminAccounts.policyLink')}
            </Link>
            <Link className="text-accent underline" href="/admin/accounts/mail">
              {t('adminAccounts.mailLink')}
            </Link>
          </nav>
          {erasure && (
            <div role="status" className={SUMMARY}>
              <p className="font-medium break-all">{erasure.label}</p>
              <ErasureSummary result={erasure.result} />
              <button className="ui-button ui-button-ghost mt-2" onClick={() => setErasure(null)}>
                {t('adminAccounts.dismiss')}
              </button>
            </div>
          )}
          <Counts counts={counts} />
          <div className="flex flex-wrap items-center gap-3">
            <input
              className="ui-input min-w-[14rem] flex-1"
              type="search"
              aria-label={t('adminAccounts.search')}
              placeholder={t('adminAccounts.searchPlaceholder')}
              maxLength={320}
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <Select
              aria-label={t('adminAccounts.applicationStatus')}
              value={status}
              onValueChange={(value) => {
                setStatus(value);
                setOffset(0);
              }}
              options={translated(FILTERS, t)}
            />
            <Select
              aria-label={t('adminAccounts.accountState')}
              value={accountStatus ?? ''}
              onValueChange={(value) => setAccountStatus(value)}
              options={translated(STATES, t)}
            />
            <Select
              aria-label={t('adminAccounts.sortLabel')}
              value={sort}
              onValueChange={(value) => setSort(value)}
              options={translated(SORTS, t)}
            />
            <button className="ui-button ui-button-secondary" disabled={loading} onClick={refresh}>
              {t('adminAccounts.refresh')}
            </button>
            <span className="text-xs text-muted">{t('adminAccounts.total', { count: total })}</span>
          </div>
          {error && <AccountNotice error>{error}</AccountNotice>}
          {loading ? (
            <p role="status" className="text-sm text-muted">
              {t('adminAccounts.loading')}
            </p>
          ) : accounts.length === 0 ? (
            <p className="text-sm text-muted">{t('adminAccounts.empty')}</p>
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
                        onErased={(result) =>
                          setErasure({
                            label: account.identity?.label ?? account.email ?? account.id,
                            result,
                          })
                        }
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
              {t('adminAccounts.previous')}
            </button>
            <button
              className="ui-button ui-button-secondary"
              disabled={loading || offset + 50 >= total}
              onClick={() => setOffset((v) => v + 50)}
            >
              {t('adminAccounts.next')}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

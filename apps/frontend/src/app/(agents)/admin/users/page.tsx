'use client';
// Admin → Users → People: every account in one table, with its access, its
// state and the teams it is in. The counts above it are its quick views, and
// the filters stay in the address bar.
import Link from 'next/link';
import { Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from '@/components/providers/AuthProvider';
import { useT } from '@/components/providers/LocaleProvider';
import { Select } from '@/components/ui/Select';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import { listAccounts, type AccountEntry } from '@/lib/api/accounts';
import type { ErasureResult } from '@/lib/api/accountAdmin';
import { getErrorMessage } from '@/lib/utils/errors';
import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import { ActionEditor, ErasureSummary, SUMMARY } from './AccountActions';
import { AccountNotes, SignInHistory } from './AccountDetails';
import { queryFromFilters, useUserFilters, type FilterChange, type UserFilters } from './filters';
import { COUNT_VIEWS, FILTERS, SORTS, STATES, translated } from './options';
import { UserTable } from './UserTable';
import { UsersTabs } from './UsersTabs';

const PAGE_SIZE = 50;
/** How long typing pauses before it searches, as HybridInference's filter bar. */
const SEARCH_DELAY_MS = 300;

function Counts({
  counts,
  filters,
  onPick,
}: {
  counts: Record<string, number> | undefined;
  filters: UserFilters;
  onPick: (view: (typeof COUNT_VIEWS)[number]) => void;
}) {
  const t = useT();
  if (!counts) return null;
  return (
    <div role="group" aria-label={t('adminAccounts.counts')} className="flex flex-wrap gap-2">
      {COUNT_VIEWS.map((view) => {
        const active = filters.status === view.status && filters.state === view.state;
        return (
          <button
            key={view.key}
            type="button"
            aria-pressed={active}
            onClick={() => onPick(view)}
            className={`ui-focus-ring min-w-[7rem] rounded-xl border px-3 py-2 text-left transition-colors ${
              active
                ? 'border-accent bg-surface text-foreground'
                : 'border-border bg-surface text-muted hover:bg-surface-hover hover:text-foreground'
            }`}
          >
            <span className="block text-xs">{t(view.label)}</span>
            <span className="block text-lg font-semibold tabular-nums text-foreground">
              {counts[view.key] ?? 0}
            </span>
          </button>
        );
      })}
    </div>
  );
}

function useAccounts(filters: UserFilters, offset: number, reload: number, userId?: string) {
  const [accounts, setAccounts] = useState<AccountEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [counts, setCounts] = useState<Record<string, number> | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const { status, state, q, sort, team } = filters;
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setAccounts([]);
    void listAccounts(status, offset, controller.signal, { accountStatus: state, q, sort, team })
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
  }, [status, state, q, sort, team, offset, reload, userId]);
  return { accounts, total, counts, error, loading };
}

/** The team a team filter names, by the name its listed members carry. */
function teamName(accounts: AccountEntry[], teamId: string): string {
  for (const account of accounts) {
    const team = account.teams?.find((candidate) => candidate.id === teamId);
    if (team) return team.name;
  }
  return teamId;
}

function FilterBar({
  filters,
  onChange,
  loading,
  total,
  onRefresh,
}: {
  filters: UserFilters;
  onChange: (change: FilterChange) => void;
  loading: boolean;
  total: number;
  onRefresh: () => void;
}) {
  const t = useT();
  const [search, setSearch] = useState(filters.q);
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  // Typing settles into the address bar once it pauses; a search that arrives
  // there another way (a link, back and forward) fills the box.
  const type = (value: string) => {
    setSearch(value);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      const q = value.trim();
      onChange((current) => (current.q === q ? current : { ...current, q }));
    }, SEARCH_DELAY_MS);
  };
  useEffect(() => {
    setSearch((current) => (current.trim() === filters.q ? current : filters.q));
  }, [filters.q]);
  return (
    <div className="flex flex-wrap items-center gap-3">
      <input
        className="ui-input min-w-[14rem] flex-1"
        type="search"
        aria-label={t('adminAccounts.search')}
        placeholder={t('adminAccounts.searchPlaceholder')}
        maxLength={320}
        value={search}
        onChange={(e) => type(e.target.value)}
      />
      <Select
        aria-label={t('adminAccounts.applicationStatus')}
        value={filters.status}
        onValueChange={(status) => onChange((current) => ({ ...current, status }))}
        options={translated(FILTERS, t)}
      />
      <Select
        aria-label={t('adminAccounts.accountState')}
        value={filters.state}
        onValueChange={(state) => onChange((current) => ({ ...current, state }))}
        options={translated(STATES, t)}
      />
      <Select
        aria-label={t('adminAccounts.sortLabel')}
        value={filters.sort}
        onValueChange={(sort) => onChange((current) => ({ ...current, sort }))}
        options={translated(SORTS, t)}
      />
      <button className="ui-button ui-button-secondary" disabled={loading} onClick={onRefresh}>
        {t('adminAccounts.refresh')}
      </button>
      <span className="text-xs text-muted">{t('adminAccounts.total', { count: total })}</span>
    </div>
  );
}

function PeopleView() {
  const { state, refreshUser } = useAuth();
  const t = useT();
  const [filters, setFilters] = useUserFilters();
  const [offset, setOffset] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  // Kept at the page: the list refreshes and closes the editor after an
  // erasure, and the operator still needs its result and archive keys.
  const [erasure, setErasure] = useState<{ label: string; result: ErasureResult } | null>(null);
  const filterKey = queryFromFilters(filters);
  useEffect(() => {
    setOffset(0);
    setOpenId(null);
  }, [filterKey]);
  const { accounts, total, counts, error, loading } = useAccounts(
    filters,
    offset,
    reload,
    state.user?.id,
  );
  const refresh = useCallback(() => {
    setOpenId(null);
    setReload((v) => v + 1);
  }, []);
  const detail = (account: AccountEntry) => (
    <div className="space-y-3">
      {account.waitlist.review_reason && (
        <p className="mt-3 whitespace-pre-wrap break-words text-sm text-muted">
          {t('adminAccounts.reviewReason', { reason: account.waitlist.review_reason })}
        </p>
      )}
      <ActionEditor
        key={`${account.id}:${account.waitlist.version}`}
        account={account}
        onCancel={() => setOpenId(null)}
        onErased={(result) =>
          setErasure({ label: account.identity?.label ?? account.email ?? account.id, result })
        }
        onDone={() => {
          refresh();
          if (account.id === state.user?.id) void refreshUser();
        }}
      />
      <div className="space-y-3 rounded-lg border border-border bg-surface p-4">
        <AccountNotes account={account} onSaved={refresh} />
        <SignInHistory accountId={account.id} />
      </div>
    </div>
  );
  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section={t('adminAccounts.section')}
          description={t('adminAccounts.description')}
        />
        <AdminNavigation current="users" />
        <UsersTabs current="people" />
        <div className="mt-5 space-y-4">
          <nav className="flex flex-wrap gap-3 text-sm" aria-label={t('adminAccounts.navigation')}>
            <Link className="text-accent underline" href="/admin/users/policy">
              {t('adminAccounts.policyLink')}
            </Link>
            <Link className="text-accent underline" href="/admin/users/mail">
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
          <Counts
            counts={counts}
            filters={filters}
            onPick={(view) =>
              setFilters((current) => ({ ...current, status: view.status, state: view.state }))
            }
          />
          <FilterBar
            filters={filters}
            onChange={setFilters}
            loading={loading}
            total={total}
            onRefresh={refresh}
          />
          {filters.team && (
            <p className="flex flex-wrap items-center gap-2 text-sm">
              <span className="rounded-full border border-border bg-surface px-3 py-1">
                {t('adminAccounts.teamFilter', { team: teamName(accounts, filters.team) })}
              </span>
              <button
                type="button"
                className="text-accent underline"
                onClick={() => setFilters((current) => ({ ...current, team: '' }))}
              >
                {t('adminAccounts.clearTeamFilter')}
              </button>
            </p>
          )}
          {error && <AccountNotice error>{error}</AccountNotice>}
          {loading ? (
            <p role="status" className="text-sm text-muted">
              {t('adminAccounts.loading')}
            </p>
          ) : accounts.length === 0 ? (
            <p className="text-sm text-muted">{t('adminAccounts.empty')}</p>
          ) : (
            <UserTable
              accounts={accounts}
              openId={openId}
              onToggle={(account) =>
                setOpenId((current) => (current === account.id ? null : account.id))
              }
              detail={detail}
            />
          )}
          <div className="flex gap-2">
            <button
              className="ui-button ui-button-secondary"
              disabled={loading || offset === 0}
              onClick={() => setOffset((v) => Math.max(0, v - PAGE_SIZE))}
            >
              {t('adminAccounts.previous')}
            </button>
            <button
              className="ui-button ui-button-secondary"
              disabled={loading || offset + PAGE_SIZE >= total}
              onClick={() => setOffset((v) => v + PAGE_SIZE)}
            >
              {t('adminAccounts.next')}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

export default function UsersPage() {
  return (
    <Suspense fallback={null}>
      <PeopleView />
    </Suspense>
  );
}

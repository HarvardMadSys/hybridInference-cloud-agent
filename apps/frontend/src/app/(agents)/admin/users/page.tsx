'use client';
// Admin → Users → People: every account in one table, with its access, its
// state and the teams it is in. The counts heading the list are its quick
// views, and the filters stay in the address bar.
import Link from 'next/link';
import { Suspense, useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { useAuth } from '@/components/providers/AuthProvider';
import { useT } from '@/components/providers/LocaleProvider';
import { Select } from '@/components/ui/Select';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import { listAccounts, type AccountEntry } from '@/lib/api/accounts';
import type { ErasureResult } from '@/lib/api/accountAdmin';
import { getErrorMessage } from '@/lib/utils/errors';
import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import { CARD } from '../runs/shared';
import { ActionEditor, ErasureSummary, SUMMARY } from './AccountActions';
import { AccountNotes, SignInHistory } from './AccountDetails';
import { queryFromFilters, useUserFilters, type FilterChange, type UserFilters } from './filters';
import { COUNT_VIEWS, FILTERS, SORTS, STATES, translated } from './options';
import { AccountFacts, UserTable } from './UserTable';
import { USERS_TOOL, UsersTabs } from './UsersTabs';

const PAGE_SIZE = 50;
/** How long typing pauses before it searches, as HybridInference's filter bar. */
const SEARCH_DELAY_MS = 300;
/** A filter's select: sharing the row on a phone, its own width beside the search. */
const SELECT = 'ui-input min-w-[8.5rem] flex-1 sm:flex-none';

/** The quick views, each with the accounts it counts; one may be the view shown. */
function StatusFilters({
  counts,
  filters,
  onPick,
}: {
  counts: Record<string, number> | undefined;
  filters: UserFilters;
  onPick: (view: (typeof COUNT_VIEWS)[number]) => void;
}) {
  const t = useT();
  return (
    <div role="group" aria-label={t('adminAccounts.counts')} className="flex flex-wrap gap-1">
      {COUNT_VIEWS.map((view) => {
        const active = filters.status === view.status && filters.state === view.state;
        const count = counts ? (counts[view.key] ?? 0) : undefined;
        // Applications waiting are the one count that asks for something.
        const waiting = view.key === 'admission:pending' && !!count;
        return (
          <button
            key={view.key}
            type="button"
            aria-pressed={active}
            onClick={() => onPick(view)}
            className={`ui-focus-ring inline-flex min-h-8 items-center gap-1.5 rounded-md border px-2.5 py-1 text-[13px] font-medium transition-colors ${
              active
                ? 'border-border-strong bg-surface text-foreground shadow-subtle'
                : 'border-transparent text-muted hover:bg-surface-hover hover:text-foreground'
            }`}
          >
            {t(view.label)}
            {count !== undefined && (
              <span
                className={`rounded-full px-1.5 text-xs tabular-nums ${
                  waiting
                    ? 'bg-status-warning/15 text-status-warning'
                    : 'bg-surface-hover text-muted'
                }`}
              >
                {count}
              </span>
            )}
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
    <div className="flex flex-wrap items-center gap-2">
      <input
        className="ui-input w-full sm:w-80"
        type="search"
        aria-label={t('adminAccounts.search')}
        placeholder={t('adminAccounts.searchPlaceholder')}
        maxLength={320}
        value={search}
        onChange={(e) => type(e.target.value)}
      />
      <div className="flex w-full flex-wrap gap-2 sm:w-auto">
        <Select
          className={SELECT}
          aria-label={t('adminAccounts.applicationStatus')}
          value={filters.status}
          onValueChange={(status) => onChange((current) => ({ ...current, status }))}
          options={translated(FILTERS, t)}
        />
        <Select
          className={SELECT}
          aria-label={t('adminAccounts.accountState')}
          value={filters.state}
          onValueChange={(state) => onChange((current) => ({ ...current, state }))}
          options={translated(STATES, t)}
        />
        <Select
          className={SELECT}
          aria-label={t('adminAccounts.sortLabel')}
          value={filters.sort}
          onValueChange={(sort) => onChange((current) => ({ ...current, sort }))}
          options={translated(SORTS, t)}
        />
      </div>
      <div className="ml-auto flex items-center gap-2">
        <span className="text-xs tabular-nums text-muted">
          {t('adminAccounts.total', { count: total })}
        </span>
        <button className="ui-button ui-button-secondary" disabled={loading} onClick={onRefresh}>
          {t('adminAccounts.refresh')}
        </button>
      </div>
    </div>
  );
}

/** Previous and Next, once the view runs past one page. */
function Pages({
  offset,
  total,
  loading,
  onMove,
}: {
  offset: number;
  total: number;
  loading: boolean;
  onMove: (offset: number) => void;
}) {
  const t = useT();
  if (offset === 0 && total <= PAGE_SIZE) return null;
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-4 py-2.5">
      <span className="text-xs tabular-nums text-muted">
        {t('adminAccounts.page', {
          from: Math.min(offset + 1, total),
          to: Math.min(offset + PAGE_SIZE, total),
          total,
        })}
      </span>
      <div className="flex gap-2">
        <button
          className="ui-button ui-button-secondary"
          disabled={loading || offset === 0}
          onClick={() => onMove(Math.max(0, offset - PAGE_SIZE))}
        >
          {t('adminAccounts.previous')}
        </button>
        <button
          className="ui-button ui-button-secondary"
          disabled={loading || offset + PAGE_SIZE >= total}
          onClick={() => onMove(offset + PAGE_SIZE)}
        >
          {t('adminAccounts.next')}
        </button>
      </div>
    </div>
  );
}

/** What the list holds now: the rows, or why there are none. */
function Rows({
  accounts,
  error,
  loading,
  table,
}: {
  accounts: AccountEntry[];
  error: string | null;
  loading: boolean;
  table: ReactNode;
}) {
  const t = useT();
  if (error)
    return (
      <div className="p-4">
        <AccountNotice error>{error}</AccountNotice>
      </div>
    );
  if (loading)
    return (
      <p role="status" className="px-4 py-10 text-center text-sm text-muted">
        {t('adminAccounts.loading')}
      </p>
    );
  if (accounts.length === 0)
    return <p className="px-4 py-10 text-center text-sm text-muted">{t('adminAccounts.empty')}</p>;
  return table;
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
    <div className="grid gap-4 lg:grid-cols-2">
      <div className="min-w-0 space-y-4">
        <div className="rounded-lg border border-border bg-surface p-4">
          <AccountFacts account={account} />
        </div>
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
      </div>
      <div className="min-w-0 space-y-3 self-start rounded-lg border border-border bg-surface p-4">
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
        <UsersTabs current="people">
          <nav className="flex flex-wrap gap-1" aria-label={t('adminAccounts.navigation')}>
            <Link className={USERS_TOOL} href="/admin/users/policy">
              {t('adminAccounts.policyLink')}
            </Link>
            <Link className={USERS_TOOL} href="/admin/users/mail">
              {t('adminAccounts.mailLink')}
            </Link>
          </nav>
        </UsersTabs>
        {erasure && (
          <div role="status" className={`${SUMMARY} mt-4`}>
            <p className="font-medium break-all">{erasure.label}</p>
            <ErasureSummary result={erasure.result} />
            <button className="ui-button ui-button-ghost mt-2" onClick={() => setErasure(null)}>
              {t('adminAccounts.dismiss')}
            </button>
          </div>
        )}
        <div className={`${CARD} mt-4 overflow-hidden`}>
          <div className="space-y-2.5 border-b border-border px-3 py-3 sm:px-4">
            <StatusFilters
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
              <p className="flex flex-wrap items-center gap-2 text-[13px]">
                <span className="rounded-md border border-border bg-surface-muted px-2 py-0.5 text-foreground">
                  {t('adminAccounts.teamFilter', { team: teamName(accounts, filters.team) })}
                </span>
                <button
                  type="button"
                  className="ui-focus-ring rounded font-medium text-accent hover:underline"
                  onClick={() => setFilters((current) => ({ ...current, team: '' }))}
                >
                  {t('adminAccounts.clearTeamFilter')}
                </button>
              </p>
            )}
          </div>
          <Rows
            accounts={accounts}
            error={error}
            loading={loading}
            table={
              <UserTable
                accounts={accounts}
                openId={openId}
                onToggle={(account) =>
                  setOpenId((current) => (current === account.id ? null : account.id))
                }
                detail={detail}
              />
            }
          />
          <Pages offset={offset} total={total} loading={loading} onMove={setOffset} />
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

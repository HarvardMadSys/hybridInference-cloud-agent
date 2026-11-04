'use client';
// Admin → Users → People: every account in one table, with its access, its
// state and the teams it is in. The counts heading the list are its quick
// views, and the filters stay in the address bar.
import { Suspense, useCallback, useEffect, useState, type ReactNode } from 'react';
import { useAuth } from '@/components/providers/AuthProvider';
import { useT } from '@/components/providers/LocaleProvider';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import { listAccounts, type AccountEntry } from '@/lib/api/accounts';
import type { ErasureResult } from '@/lib/api/accountAdmin';
import { getErrorMessage } from '@/lib/utils/errors';
import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import { ActionEditor, ErasureSummary, SUMMARY } from './AccountActions';
import { AccountNotes, SignInHistory } from './AccountDetails';
import { AccountTools } from './AccountTools';
import { useDensity } from './density';
import { queryFromFilters, useUserFilters, type UserFilters } from './filters';
import { SMALL_BUTTON, Toolbar } from './Toolbar';
import { AccountFacts, UserTable } from './UserTable';
import { UsersTabs } from './UsersTabs';

const PAGE_SIZE = 50;

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
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-3 py-2">
      <span className="text-[13px] tabular-nums text-muted">
        {t('adminAccounts.page', {
          from: Math.min(offset + 1, total),
          to: Math.min(offset + PAGE_SIZE, total),
          total,
        })}
      </span>
      <div className="flex gap-2">
        <button
          type="button"
          className={SMALL_BUTTON}
          disabled={loading || offset === 0}
          onClick={() => onMove(Math.max(0, offset - PAGE_SIZE))}
        >
          {t('adminAccounts.previous')}
        </button>
        <button
          type="button"
          className={SMALL_BUTTON}
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
      <p role="status" className="px-4 py-12 text-center text-[13px] text-muted">
        {t('adminAccounts.loading')}
      </p>
    );
  if (accounts.length === 0)
    return (
      <p className="px-4 py-12 text-center text-[13px] text-muted">{t('adminAccounts.empty')}</p>
    );
  return table;
}

function PeopleView() {
  const { state, refreshUser } = useAuth();
  const t = useT();
  const [filters, setFilters] = useUserFilters();
  const [offset, setOffset] = useState(0);
  const [openId, setOpenId] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const [density, setDensity] = useDensity();
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
          <AccountTools />
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
        <div className="mt-4 overflow-hidden rounded-lg border border-border bg-surface">
          <Toolbar
            filters={filters}
            onChange={setFilters}
            counts={counts}
            total={total}
            loading={loading}
            onRefresh={refresh}
            density={density}
            onDensity={setDensity}
          />
          {filters.team && (
            <p className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2 text-[13px]">
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
          <Rows
            accounts={accounts}
            error={error}
            loading={loading}
            table={
              <UserTable
                accounts={accounts}
                openId={openId}
                density={density}
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

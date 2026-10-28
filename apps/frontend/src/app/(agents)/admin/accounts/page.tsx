'use client';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from '@/components/providers/AuthProvider';
import { Select } from '@/components/ui/Select';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import {
  listAccounts,
  updateAccount,
  type AccountEntry,
  type AccountAction,
  type WaitlistStatus,
} from '@/lib/api/accounts';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';

const FILTERS = [
  { value: 'pending', label: 'Waiting for review' },
  { value: 'approved', label: 'Approved' },
  { value: 'rejected', label: 'Declined' },
  { value: 'revoked', label: 'Access withdrawn' },
  { value: 'not_applied', label: 'Not applied' },
  { value: '', label: 'All accounts' },
] as const;
const LABELS: Record<AccountAction, string> = {
  approve: 'Approve access',
  reject: 'Decline application',
  revoke: 'Withdraw access',
  suspend: 'Suspend account',
  restore: 'Restore account',
  grant_admin: 'Grant administrator',
  revoke_admin: 'Remove administrator',
};
function actionsFor(account: AccountEntry): AccountAction[] {
  const actions: AccountAction[] = [];
  if (account.status === 'suspended') return ['restore'];
  if (account.email_verified && account.waitlist.status !== 'approved') actions.push('approve');
  if (account.waitlist.status === 'pending') actions.push('reject');
  if (account.waitlist.status === 'approved')
    actions.push('revoke', account.is_admin ? 'revoke_admin' : 'grant_admin');
  actions.push('suspend');
  return actions;
}
function ActionEditor({
  account,
  onDone,
  onCancel,
}: {
  account: AccountEntry;
  onDone: () => void;
  onCancel: () => void;
}) {
  const actions = actionsFor(account);
  const [action, setAction] = useState<AccountAction>(actions[0]);
  const [reason, setReason] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stale, setStale] = useState(false);
  const submitting = useRef(false);
  async function apply() {
    if (submitting.current || stale) return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      await updateAccount(account, action, reason.trim());
      onDone();
    } catch (cause) {
      if (cause instanceof APIError && cause.code === 'version_conflict') {
        setStale(true);
        setError(
          'This account changed while you were reviewing it. Close this form and refresh the list before making another decision.',
        );
      } else setError(getErrorMessage(cause));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <section
      className="mt-3 space-y-3 rounded-lg border border-border bg-surface-muted p-4"
      aria-label={`Review ${account.email}`}
    >
      <p className="text-sm font-medium break-all">{account.email}</p>
      <label className="block text-sm" htmlFor="account-action">
        Action
      </label>
      <Select
        id="account-action"
        value={action}
        onValueChange={setAction}
        options={actions.map((value) => ({ value, label: LABELS[value] }))}
        disabled={busy || stale}
      />
      {(action === 'reject' || action === 'revoke') && (
        <>
          <label className="block text-sm" htmlFor="review-reason">
            Reason shown to the applicant (optional)
          </label>
          <textarea
            id="review-reason"
            className="ui-input w-full"
            rows={3}
            maxLength={2000}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={busy || stale}
          />
        </>
      )}
      {action === 'suspend' && (
        <p className="text-sm text-muted">
          Signs this account out, withdraws Agent access, and stops its work. Restoring the account
          will not restore access automatically.
        </p>
      )}
      {action === 'revoke' && (
        <p className="text-sm text-muted">
          Stops this account’s Agent work. The user can still sign in to view the decision and
          reapply.
        </p>
      )}
      {action === 'grant_admin' && (
        <p className="text-sm text-muted">
          Grants platform administration, including account approvals and infrastructure management.
        </p>
      )}
      {error && <AccountNotice error>{error}</AccountNotice>}
      <div className="flex flex-wrap gap-2">
        <button
          className="ui-button ui-button-primary"
          disabled={busy || stale}
          onClick={() => void apply()}
        >
          {busy ? 'Applying…' : LABELS[action]}
        </button>
        <button className="ui-button ui-button-secondary" disabled={busy} onClick={onCancel}>
          Close
        </button>
      </div>
    </section>
  );
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
  return (
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0 flex-1">
        <p className="break-all font-medium">{account.email}</p>
        <p className="mt-1 text-xs text-muted">
          {account.status} · {account.waitlist.status.replaceAll('_', ' ')} ·{' '}
          {account.email_verified ? 'Email verified' : 'Email unverified'}
          {account.is_admin ? ' · Administrator' : ''}
        </p>
        {account.waitlist.purpose && (
          <p className="mt-3 whitespace-pre-wrap break-words text-sm">{account.waitlist.purpose}</p>
        )}
        {account.waitlist.review_reason && (
          <p className="mt-2 whitespace-pre-wrap break-words text-sm text-muted">
            Review reason: {account.waitlist.review_reason}
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
export default function AccountsPage() {
  const { state, refreshUser } = useAuth();
  const [status, setStatus] = useState<WaitlistStatus | ''>('pending');
  const [offset, setOffset] = useState(0);
  const [accounts, setAccounts] = useState<AccountEntry[]>([]);
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState<AccountEntry | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const local = state.user?.auth_mode === 'local';
  const refresh = useCallback(() => {
    setSelected(null);
    setReload((v) => v + 1);
  }, []);
  useEffect(() => {
    if (!local) {
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setAccounts([]);
    setSelected(null);
    void listAccounts(status, offset, controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;
        setAccounts(result.accounts);
        setTotal(result.total);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(getErrorMessage(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [local, status, offset, reload, state.user?.id]);
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
            <div className="flex flex-wrap items-center gap-3">
              <Select
                aria-label="Application status"
                value={status}
                onValueChange={(value) => {
                  setStatus(value);
                  setOffset(0);
                }}
                options={FILTERS}
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
                      <ActionEditor
                        key={`${account.id}:${account.waitlist.version}`}
                        account={selected}
                        onCancel={() => setSelected(null)}
                        onDone={() => {
                          refresh();
                          if (account.id === state.user?.id) void refreshUser();
                        }}
                      />
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

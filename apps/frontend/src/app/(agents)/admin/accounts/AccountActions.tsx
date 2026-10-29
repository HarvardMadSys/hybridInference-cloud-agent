'use client';
// One administrator decision on one account, at the version the administrator
// read: the waiting-list review and the account's state (suspend, restore,
// delete, resume), as HybridInference's approve/reject/update/delete/resume.
import { useRef, useState } from 'react';
import { Select } from '@/components/ui/Select';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import { updateAccount, type AccountAction, type AccountEntry } from '@/lib/api/accounts';
import { eraseAccount } from '@/lib/api/accountAdmin';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

export const LABELS: Record<AccountAction, string> = {
  approve: 'Approve access',
  reject: 'Decline application',
  revoke: 'Withdraw access',
  suspend: 'Suspend account',
  restore: 'Restore account',
  grant_admin: 'Grant administrator',
  revoke_admin: 'Remove administrator',
  delete: 'Delete account',
  resume: 'Resume deleted account',
};

const EXPLANATIONS: Partial<Record<AccountAction, string>> = {
  suspend:
    'Signs this account out, withdraws Agent access, and stops its work. Restoring the account will not restore access automatically.',
  revoke:
    'Stops this account’s Agent work. The user can still sign in to view the decision and reapply.',
  grant_admin:
    'Grants platform administration, including account approvals and infrastructure management.',
  delete:
    'Signs the account out everywhere, takes it off every browser, stops its work and keeps its data. It can be resumed, but Agent access needs a new approval.',
  resume:
    'Lets the account sign in again. Agent access stays withdrawn until it is approved again.',
};

function verified(account: AccountEntry): boolean {
  return account.identity?.identity_verified ?? account.email_verified;
}

export function actionsFor(account: AccountEntry): AccountAction[] {
  if (account.status === 'deleted') return account.erased_at ? [] : ['resume'];
  if (account.status === 'suspended') return ['restore', 'delete'];
  const actions: AccountAction[] = [];
  if (verified(account) && account.waitlist.status !== 'approved') actions.push('approve');
  if (account.waitlist.status === 'pending') actions.push('reject');
  if (account.waitlist.status === 'approved')
    actions.push('revoke', account.is_admin ? 'revoke_admin' : 'grant_admin');
  actions.push('suspend', 'delete');
  return actions;
}

export function ActionEditor({
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
  const label = account.identity?.label ?? account.email ?? account.id;
  return (
    <section
      className="mt-3 space-y-3 rounded-lg border border-border bg-surface-muted p-4"
      aria-label={`Review ${label}`}
    >
      <p className="text-sm font-medium break-all">{label}</p>
      {actions.length === 0 ? (
        <p className="text-sm text-muted">This account was erased; nothing can be applied.</p>
      ) : (
        <>
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
          {EXPLANATIONS[action] && <p className="text-sm text-muted">{EXPLANATIONS[action]}</p>}
        </>
      )}
      {error && <AccountNotice error>{error}</AccountNotice>}
      <div className="flex flex-wrap gap-2">
        {actions.length > 0 && (
          <button
            className="ui-button ui-button-primary"
            disabled={busy || stale}
            onClick={() => void apply()}
          >
            {busy ? 'Applying…' : LABELS[action]}
          </button>
        )}
        <button className="ui-button ui-button-secondary" disabled={busy} onClick={onCancel}>
          Close
        </button>
      </div>
      {account.status === 'deleted' && !account.erased_at && (
        <ErasePanel account={account} onDone={onDone} />
      )}
    </section>
  );
}

/** Permanent erasure of a deleted account: its id typed out again confirms it. */
function ErasePanel({ account, onDone }: { account: AccountEntry; onDone: () => void }) {
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function erase() {
    setBusy(true);
    setError(null);
    try {
      const erased = await eraseAccount(account.id, confirm.trim());
      const rows = Object.values(erased.deleted).reduce((sum, count) => sum + count, 0);
      const archives = erased.retained_archives.length;
      const kept = archives
        ? ` ${archives} sandbox archive(s) remain in object storage under its retention policy.`
        : '';
      setResult(`Erased ${rows} rows of Agent data.${kept}`);
      onDone();
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-2 border-t border-border pt-3">
      <p className="text-sm font-medium">Erase permanently</p>
      <p className="text-xs text-muted">
        Removes the account’s identity and all its Agent data. Only once its work has stopped and
        its sandboxes are deleted. This cannot be undone, and it cannot be resumed after.
      </p>
      <label className="block text-sm" htmlFor="erase-confirm">
        Type the account id to confirm
      </label>
      <input
        id="erase-confirm"
        className="ui-input w-full font-mono"
        value={confirm}
        onChange={(e) => setConfirm(e.target.value)}
      />
      {error && <AccountNotice error>{error}</AccountNotice>}
      {result && <AccountNotice>{result}</AccountNotice>}
      <button
        className="ui-button ui-button-danger"
        disabled={busy || confirm.trim() !== account.id}
        onClick={() => void erase()}
      >
        Erase account
      </button>
    </div>
  );
}

'use client';
// One administrator decision on one account, at the version the administrator
// read: the waiting-list review and the account's state (suspend, restore,
// delete, resume), as HybridInference's approve/reject/update/delete/resume.
import { useRef, useState } from 'react';
import { Select } from '@/components/ui/Select';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import { useT } from '@/components/providers/LocaleProvider';
import { updateAccount, type AccountAction, type AccountEntry } from '@/lib/api/accounts';
import { eraseAccount, type ErasureResult } from '@/lib/api/accountAdmin';
import type { TranslationKey } from '@/lib/i18n/translate';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { OwnedTeams } from './OwnedTeams';

export const LABELS: Record<AccountAction, TranslationKey> = {
  approve: 'adminAccounts.action.approve',
  reject: 'adminAccounts.action.reject',
  revoke: 'adminAccounts.action.revoke',
  suspend: 'adminAccounts.action.suspend',
  restore: 'adminAccounts.action.restore',
  grant_admin: 'adminAccounts.action.grantAdmin',
  revoke_admin: 'adminAccounts.action.revokeAdmin',
  delete: 'adminAccounts.action.delete',
  resume: 'adminAccounts.action.resume',
};

const EXPLANATIONS: Partial<Record<AccountAction, TranslationKey>> = {
  suspend: 'adminAccounts.explain.suspend',
  revoke: 'adminAccounts.explain.revoke',
  reject: 'adminAccounts.explain.reject',
  revoke_admin: 'adminAccounts.explain.revokeAdmin',
  grant_admin: 'adminAccounts.explain.grantAdmin',
  delete: 'adminAccounts.explain.delete',
  resume: 'adminAccounts.explain.resume',
};

// Administrator email addresses (Admin → Settings → Accounts) make a listed,
// proven address administrator again at sign-in, so the service refuses to
// withdraw what the list would hand back; the address leaves the list first.
const UNDONE_BY_ADMIN_LIST: ReadonlySet<AccountAction> = new Set([
  'revoke',
  'reject',
  'revoke_admin',
]);

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

export const SUMMARY = 'rounded-lg border border-border bg-surface-muted p-3 text-sm text-muted';

/** What an erasure removed and what it left for the operator, archive keys included. */
export function ErasureSummary({ result }: { result: ErasureResult }) {
  const t = useT();
  const rows = Object.values(result.deleted).reduce((sum, count) => sum + count, 0);
  const teams = result.deleted_teams ?? [];
  const environments = result.handed_environments ?? [];
  const archives = result.retained_archives;
  const parts = [
    t('adminAccounts.erase.rows', { count: rows }),
    archives.length ? t('adminAccounts.erase.archives', { count: archives.length }) : '',
    teams.length ? t('adminAccounts.erase.teams', { count: teams.length }) : '',
    environments.length
      ? t('adminAccounts.erase.environments', { count: environments.length })
      : '',
  ].filter(Boolean);
  return (
    <div className="space-y-1">
      <p>{parts.join(' ')}</p>
      {archives.length > 0 && (
        <ul
          className="list-disc pl-5 font-mono text-xs"
          aria-label={t('adminAccounts.erase.archiveList')}
        >
          {archives.map((key) => (
            <li key={key} className="break-all">
              {key}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function Explanation({ action }: { action: AccountAction }) {
  const t = useT();
  const key = EXPLANATIONS[action];
  if (!key) return null;
  const listed = UNDONE_BY_ADMIN_LIST.has(action) ? ` ${t('adminAccounts.explain.listed')}` : '';
  return <p className="text-sm text-muted">{`${t(key)}${listed}`}</p>;
}

export function ActionEditor({
  account,
  onDone,
  onCancel,
  onErased,
}: {
  account: AccountEntry;
  onDone: () => void;
  onCancel: () => void;
  /** Where an erasure's result goes; shown here when the page keeps none. */
  onErased?: (result: ErasureResult) => void;
}) {
  const t = useT();
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
        setError(t('adminAccounts.stale'));
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
      aria-label={t('adminAccounts.reviewOf', { label })}
    >
      <p className="text-sm font-medium break-all">{label}</p>
      {actions.length === 0 ? (
        <p className="text-sm text-muted">{t('adminAccounts.erasedNothing')}</p>
      ) : (
        <>
          <label className="block text-sm" htmlFor="account-action">
            {t('adminAccounts.actionLabel')}
          </label>
          <Select
            id="account-action"
            value={action}
            onValueChange={setAction}
            options={actions.map((value) => ({ value, label: t(LABELS[value]) }))}
            disabled={busy || stale}
          />
          {(action === 'reject' || action === 'revoke') && (
            <>
              <label className="block text-sm" htmlFor="review-reason">
                {t('adminAccounts.reasonLabel')}
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
          <Explanation action={action} />
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
            {busy ? t('adminAccounts.applying') : t(LABELS[action])}
          </button>
        )}
        <button className="ui-button ui-button-secondary" disabled={busy} onClick={onCancel}>
          {t('adminAccounts.close')}
        </button>
      </div>
      {account.status === 'deleted' && !account.erased_at && (
        <ErasePanel account={account} onDone={onDone} onErased={onErased} />
      )}
    </section>
  );
}

/** Permanent erasure of a deleted account: its id typed out again confirms it. */
function ErasePanel({
  account,
  onDone,
  onErased,
}: {
  account: AccountEntry;
  onDone: () => void;
  onErased?: (result: ErasureResult) => void;
}) {
  const t = useT();
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ErasureResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [reload, setReload] = useState(0);
  async function erase() {
    setBusy(true);
    setError(null);
    try {
      const erased = await eraseAccount(account.id, confirm.trim());
      if (onErased) onErased(erased);
      else setResult(erased);
      onDone();
    } catch (cause) {
      setError(getErrorMessage(cause));
      // A refusal may name teams to hand over: show them as they are now.
      setReload((value) => value + 1);
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-2 border-t border-border pt-3">
      <p className="text-sm font-medium">{t('adminAccounts.erase.title')}</p>
      <p className="text-xs text-muted">{t('adminAccounts.erase.explain')}</p>
      <OwnedTeams accountId={account.id} reload={reload} />
      <label className="block text-sm" htmlFor="erase-confirm">
        {t('adminAccounts.erase.confirm')}
      </label>
      <input
        id="erase-confirm"
        className="ui-input w-full font-mono"
        value={confirm}
        onChange={(e) => setConfirm(e.target.value)}
      />
      {error && <AccountNotice error>{error}</AccountNotice>}
      {result && (
        <div role="status" className={SUMMARY}>
          <ErasureSummary result={result} />
        </div>
      )}
      <button
        className="ui-button ui-button-danger"
        disabled={busy || confirm.trim() !== account.id}
        onClick={() => void erase()}
      >
        {t('adminAccounts.erase.button')}
      </button>
    </div>
  );
}

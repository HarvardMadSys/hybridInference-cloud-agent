'use client';
// Sign-up policy, adapted from HybridInference dashboard/admin/SignupPolicySection
// and admin/login_events: the signup domain allowlist (exact domains, or
// `*.` for subdomains) that the `domains` approval policy approves, and the
// retention of sign-in records. The switches themselves are in
// Admin → Settings → Accounts.
import Link from 'next/link';
import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import {
  addSignupDomain,
  listSignupDomains,
  purgeLoginEvents,
  removeSignupDomain,
  type SignupDomains,
} from '@/lib/api/accountAdmin';
import { getErrorMessage } from '@/lib/utils/errors';
import { AdminNavigation } from '../../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../../AdminPageHeader';
import { ConfirmBar } from '../AccountDetails';

function DomainList() {
  const t = useT();
  const [domains, setDomains] = useState<SignupDomains | null>(null);
  const [entry, setEntry] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      setDomains(await listSignupDomains());
    } catch (cause) {
      setError(getErrorMessage(cause));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  async function change(action: () => Promise<SignupDomains>) {
    setBusy(true);
    setError(null);
    try {
      setDomains(await action());
      setEntry('');
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  function add(event: FormEvent) {
    event.preventDefault();
    void change(() => addSignupDomain(entry.trim()));
  }
  const empty = domains?.domains.length === 0;
  return (
    <section className="space-y-3 rounded-xl border border-border bg-surface p-4">
      <h2 className="text-base font-semibold">{t('adminAccounts.policy.domains')}</h2>
      <p className="text-sm text-muted">
        {domains?.approval_policy === 'domains'
          ? empty
            ? t('adminAccounts.policy.domainsEmpty')
            : t('adminAccounts.policy.domainsListed')
          : t('adminAccounts.policy.manual')}
      </p>
      {error && <AccountNotice error>{error}</AccountNotice>}
      <form className="flex flex-wrap gap-2" onSubmit={add}>
        <input
          className="ui-input min-w-[14rem] flex-1"
          aria-label={t('adminAccounts.policy.domainToAdd')}
          placeholder="example.edu or *.example.edu"
          maxLength={256}
          value={entry}
          onChange={(e) => setEntry(e.target.value)}
        />
        <button className="ui-button ui-button-primary" disabled={busy || !entry.trim()}>
          {t('adminAccounts.policy.addDomain')}
        </button>
      </form>
      <ul className="space-y-1 text-sm">
        {domains?.domains.map((row) => (
          <li
            key={`${row.domain}:${row.is_wildcard}`}
            className="flex items-center justify-between gap-2"
          >
            <code className="break-all">{row.is_wildcard ? `*.${row.domain}` : row.domain}</code>
            <button
              className="ui-button ui-button-ghost"
              disabled={busy}
              aria-label={t('adminAccounts.policy.removeDomain', {
                domain: row.is_wildcard ? `*.${row.domain}` : row.domain,
              })}
              onClick={() => void change(() => removeSignupDomain(row))}
            >
              {t('adminAccounts.policy.remove')}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function SignInRetention() {
  const t = useT();
  const [days, setDays] = useState('90');
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  function ask(event: FormEvent) {
    event.preventDefault();
    setMessage(null);
    setConfirming(true);
  }
  async function purge() {
    setConfirming(false);
    setError(null);
    try {
      const result = await purgeLoginEvents({ olderThanDays: Number(days) });
      setMessage(t('adminAccounts.policy.purged', { count: result.deleted }));
    } catch (cause) {
      setError(getErrorMessage(cause));
    }
  }
  return (
    <section className="space-y-3 rounded-xl border border-border bg-surface p-4">
      <h2 className="text-base font-semibold">{t('adminAccounts.policy.records')}</h2>
      <p className="text-sm text-muted">{t('adminAccounts.policy.recordsExplain')}</p>
      {error && <AccountNotice error>{error}</AccountNotice>}
      {message && <AccountNotice>{message}</AccountNotice>}
      <form className="flex flex-wrap items-center gap-2" onSubmit={ask}>
        <label htmlFor="purge-days" className="text-sm">
          {t('adminAccounts.policy.olderThan')}
        </label>
        <input
          id="purge-days"
          className="ui-input w-24"
          type="number"
          min={1}
          max={36500}
          value={days}
          onChange={(e) => setDays(e.target.value)}
        />
        <button className="ui-button ui-button-secondary" disabled={confirming}>
          {t('adminAccounts.policy.purge')}
        </button>
      </form>
      {confirming && (
        <ConfirmBar
          confirm={t('adminAccounts.confirm.delete')}
          onConfirm={() => void purge()}
          onCancel={() => setConfirming(false)}
        >
          {t('adminAccounts.policy.purgeConfirm', { days })}
        </ConfirmBar>
      )}
    </section>
  );
}

export default function AccountPolicyPage() {
  const t = useT();
  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section={t('adminAccounts.section')}
          description={t('adminAccounts.policy.description')}
        />
        <AdminNavigation current="accounts" />
        <div className="mt-5 space-y-4">
          <Link className="text-sm text-accent underline" href="/admin/accounts">
            {t('adminAccounts.back')}
          </Link>
          <DomainList />
          <SignInRetention />
        </div>
      </section>
    </div>
  );
}

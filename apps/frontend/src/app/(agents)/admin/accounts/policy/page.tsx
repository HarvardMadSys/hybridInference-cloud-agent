'use client';
// Sign-up policy, adapted from HybridInference dashboard/admin/SignupPolicySection
// and admin/login_events: the signup domain allowlist (exact domains, or
// `*.` for subdomains) that the `domains` approval policy approves, and the
// retention of sign-in records. The switches themselves are in
// Admin → Settings → Accounts.
import Link from 'next/link';
import { useCallback, useEffect, useState, type FormEvent } from 'react';
import { useAuth } from '@/components/providers/AuthProvider';
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

function DomainList() {
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
      <h2 className="text-base font-semibold">Signup domain allowlist</h2>
      <p className="text-sm text-muted">
        {domains?.approval_policy === 'domains'
          ? empty
            ? 'Approval policy is “domains” and the list is empty: every verified account is approved automatically.'
            : 'Approval policy is “domains”: verified accounts from these domains are approved automatically; others wait for review.'
          : 'Approval policy is “manual”: every account waits for review. This list applies once the policy is “domains” (Admin → Settings → Accounts).'}
      </p>
      {error && <AccountNotice error>{error}</AccountNotice>}
      <form className="flex flex-wrap gap-2" onSubmit={add}>
        <input
          className="ui-input min-w-[14rem] flex-1"
          aria-label="Domain to add"
          placeholder="example.edu or *.example.edu"
          maxLength={256}
          value={entry}
          onChange={(e) => setEntry(e.target.value)}
        />
        <button className="ui-button ui-button-primary" disabled={busy || !entry.trim()}>
          Add domain
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
              aria-label={`Remove ${row.is_wildcard ? `*.${row.domain}` : row.domain}`}
              onClick={() => void change(() => removeSignupDomain(row))}
            >
              Remove
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

function SignInRetention() {
  const [days, setDays] = useState('90');
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  async function purge(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const result = await purgeLoginEvents({ olderThanDays: Number(days) });
      setMessage(`Deleted ${result.deleted} sign-in records.`);
    } catch (cause) {
      setError(getErrorMessage(cause));
    }
  }
  return (
    <section className="space-y-3 rounded-xl border border-border bg-surface p-4">
      <h2 className="text-base font-semibold">Sign-in records</h2>
      <p className="text-sm text-muted">
        Every sign-in attempt is recorded with its outcome, address and browser. Delete the records
        older than a number of days; an account’s own records can be purged from its review panel.
      </p>
      {error && <AccountNotice error>{error}</AccountNotice>}
      {message && <AccountNotice>{message}</AccountNotice>}
      <form className="flex flex-wrap items-center gap-2" onSubmit={purge}>
        <label htmlFor="purge-days" className="text-sm">
          Older than (days)
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
        <button className="ui-button ui-button-secondary">Purge</button>
      </form>
    </section>
  );
}

export default function AccountPolicyPage() {
  const { state } = useAuth();
  const local = state.user?.auth_mode === 'local';
  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader section="Accounts" description="Sign-up policy and sign-in records." />
        <AdminNavigation current="accounts" />
        <div className="mt-5 space-y-4">
          <Link className="text-sm text-accent underline" href="/admin/accounts">
            Back to accounts
          </Link>
          {local ? (
            <>
              <DomainList />
              <SignInRetention />
            </>
          ) : (
            <p className="text-sm text-muted">
              Independent accounts are not enabled on this deployment.
            </p>
          )}
        </div>
      </section>
    </div>
  );
}

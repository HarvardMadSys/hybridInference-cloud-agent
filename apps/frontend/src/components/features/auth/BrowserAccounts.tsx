'use client';
// The accounts signed in on this browser, adapted from HybridInference
// components/auth/AccountChooser.tsx: switch without a password while an
// account's own session lasts, sign one out, add another (up to the
// deployment's limit), or sign this browser out of all of them.
import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import {
  listBrowserAccounts,
  removeBrowserAccount,
  signOutEverywhereHere,
  switchAccount,
  type BrowserAccounts as Accounts,
} from '@/lib/api/accounts';
import { announceSession } from '@/lib/auth/sessionSync';
import { basePath } from '@/config/env';
import { getErrorMessage } from '@/lib/utils/errors';
import { AccountFrame, AccountNotice } from './AccountFrame';

export function BrowserAccounts() {
  const t = useT();
  const [accounts, setAccounts] = useState<Accounts | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const load = useCallback(async () => {
    try {
      setAccounts(await listBrowserAccounts());
    } catch (cause) {
      setError(getErrorMessage(cause));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  async function run(action: () => Promise<unknown>, after: () => void) {
    setBusy(true);
    setError(null);
    try {
      await action();
      after();
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  const full = accounts ? accounts.accounts.length >= accounts.max_accounts : false;
  return (
    <AccountFrame title={t('accounts.title')} description={t('accounts.description')}>
      {error && <AccountNotice error>{error}</AccountNotice>}
      {!accounts ? (
        <p role="status">{t('common.loading')}</p>
      ) : (
        <ul className="space-y-2">
          {accounts.accounts.map((account) => (
            <li
              key={account.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-border p-3"
            >
              <div className="min-w-0">
                <p className="break-all text-sm font-medium">
                  {account.user_name || account.label}
                </p>
                <p className="text-xs text-muted">
                  {account.user_name ? `${account.label} · ` : ''}
                  {account.active
                    ? t('accounts.active')
                    : account.signed_in
                      ? account.agent_access
                        ? t('accounts.ready')
                        : t('accounts.noAccess')
                      : t('accounts.signedOut')}
                </p>
              </div>
              <div className="flex gap-2">
                {!account.active && account.signed_in && (
                  <button
                    className="ui-button ui-button-primary"
                    disabled={busy}
                    onClick={() =>
                      void run(
                        () => switchAccount(account.id),
                        () => {
                          announceSession(account.id);
                          window.location.assign(`${basePath}/`);
                        },
                      )
                    }
                  >
                    {t('accounts.switch')}
                  </button>
                )}
                {!account.signed_in && (
                  <Link className="ui-button ui-button-secondary" href="/login?add=1">
                    {t('account.login')}
                  </Link>
                )}
                <button
                  className="ui-button ui-button-secondary"
                  disabled={busy}
                  onClick={() =>
                    void run(
                      () => removeBrowserAccount(account.id),
                      () => {
                        if (account.active) announceSession(null);
                        void load();
                      },
                    )
                  }
                >
                  {t('accounts.remove')}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap gap-2">
        {full ? (
          <p className="text-xs text-muted">
            {t('accounts.full', { count: accounts?.max_accounts ?? 5 })}
          </p>
        ) : (
          <Link className="ui-button ui-button-secondary" href="/login?add=1">
            {t('accounts.add')}
          </Link>
        )}
        {accounts && accounts.accounts.length > 0 && (
          <button
            className="ui-button ui-button-secondary"
            disabled={busy}
            onClick={() =>
              void run(signOutEverywhereHere, () => {
                announceSession(null);
                window.location.assign(`${basePath}/login`);
              })
            }
          >
            {t('accounts.signOutAll')}
          </button>
        )}
      </div>
    </AccountFrame>
  );
}

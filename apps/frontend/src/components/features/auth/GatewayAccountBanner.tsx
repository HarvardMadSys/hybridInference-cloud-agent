'use client';

import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';
import { useAuth, useT } from '@/components/providers';
import { switchAccountUrl } from '@/lib/auth/signin';

/** The query parameter the gateway console's Agents link carries. */
export const GATEWAY_ACCOUNT_PARAM = 'gateway_account';
const STORAGE_KEY = 'cloud-agent:gateway-account';
// The gateway's account ids; anything else is not worth offering to switch to.
const ACCOUNT_ID = /^[A-Za-z0-9_-]{1,64}$/;

function readStored(): string | null {
  try {
    return sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function store(value: string | null): void {
  try {
    if (value) sessionStorage.setItem(STORAGE_KEY, value);
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // Without storage the banner lasts as long as this page does.
  }
}

/**
 * Say so when the console sent this tab here as another account.
 *
 * The gateway console's Agents link names the account the console is using.
 * Agents signs in on its own, so it may be another account of the same browser
 * — and it does not switch on its own, because work may be running here as
 * this one. It offers the switch instead, through the gateway's account chooser
 * with that account preselected, or keeps this one.
 *
 * The id is taken off the address so a copied link does not carry it, and kept
 * for this tab's session so the banner survives moving between pages.
 */
export function GatewayAccountBanner() {
  const { state } = useAuth();
  const t = useT();
  const pathname = usePathname() ?? '/';
  const [consoleAccount, setConsoleAccount] = useState<string | null>(null);

  useEffect(() => {
    const url = new URL(window.location.href);
    const fromLink = url.searchParams.get(GATEWAY_ACCOUNT_PARAM);
    if (fromLink !== null) {
      url.searchParams.delete(GATEWAY_ACCOUNT_PARAM);
      window.history.replaceState(window.history.state, '', url.toString());
      store(ACCOUNT_ID.test(fromLink) ? fromLink : null);
    }
    setConsoleAccount(readStored());
  }, []);

  const userId = state.user?.id;
  useEffect(() => {
    // The same account needs no banner, now or after a later visit.
    if (userId && consoleAccount === userId) {
      store(null);
      setConsoleAccount(null);
    }
  }, [userId, consoleAccount]);

  if (!userId || !consoleAccount || consoleAccount === userId) return null;

  const dismiss = () => {
    store(null);
    setConsoleAccount(null);
  };

  return (
    <div
      role="status"
      className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border bg-accent-subtle px-4 py-2 text-sm text-foreground"
    >
      <p className="min-w-0 flex-1">{t('session.consoleMismatch')}</p>
      <div className="flex shrink-0 items-center gap-2">
        <a
          href={switchAccountUrl(pathname, consoleAccount)}
          className="ui-button ui-button-primary ui-focus-ring rounded-lg"
        >
          {t('session.consoleMismatchSwitch')}
        </a>
        <button
          type="button"
          onClick={dismiss}
          className="ui-button ui-button-secondary ui-focus-ring rounded-lg"
        >
          {t('session.consoleMismatchDismiss')}
        </button>
      </div>
    </div>
  );
}

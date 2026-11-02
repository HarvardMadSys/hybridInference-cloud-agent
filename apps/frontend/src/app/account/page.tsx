'use client';
// The same account security outside the Agent shell, for an account that is
// still waiting for approval (or no longer has Agent use) and cannot open
// Settings: it can still change its password or email, or delete itself.
import Link from 'next/link';
import { AccountSecurity } from '@/components/features/auth/AccountSecurity';
import { LocalAccountGate } from '@/components/features/auth/LocalAccountGate';
import { useAuth, useT } from '@/components/providers';

export default function AccountPage() {
  const t = useT();
  const { state } = useAuth();
  return (
    <LocalAccountGate>
      <main className="mx-auto w-full max-w-2xl space-y-4 px-4 py-8">
        <h1 className="text-2xl font-semibold">{t('security.title')}</h1>
        <p className="text-sm text-muted">{t('security.description')}</p>
        {state.loading ? (
          <p role="status">{t('common.loading')}</p>
        ) : state.user ? (
          <AccountSecurity isAdmin={state.user.is_local_admin === true} />
        ) : (
          <Link className="text-accent underline" href="/login">
            {t('account.login')}
          </Link>
        )}
        <Link className="block text-sm text-accent underline" href="/waiting-list">
          {t('security.back')}
        </Link>
      </main>
    </LocalAccountGate>
  );
}

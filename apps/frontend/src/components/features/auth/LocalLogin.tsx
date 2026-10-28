'use client';
// Email/password flow adapted from HybridInference 321e9a6c4 app/login/page.tsx.
// Sessions remain HttpOnly cookies; there is no browser token storage.
import { useRef, useState, type FormEvent } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { useT } from '@/components/providers/LocaleProvider';
import { loginAccount } from '@/lib/api/accounts';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { safeReturnTo } from '@/lib/auth/signin';
import { announceSession } from '@/lib/auth/sessionSync';
import { basePath } from '@/config/env';
import { AccountField, AccountFrame, AccountNotice } from './AccountFrame';
import { PasswordInput } from './PasswordInput';

export function LocalLogin({ signupEnabled }: { signupEnabled: boolean }) {
  const t = useT();
  const params = useSearchParams();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [unverified, setUnverified] = useState(false);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    setUnverified(false);
    try {
      const user = await loginAccount(email.trim(), password);
      setPassword('');
      announceSession(user.id);
      const destination =
        user.agent_access?.allowed === true
          ? safeReturnTo(params?.get('return_to') ?? '/')
          : '/waiting-list';
      window.location.assign(`${basePath}${destination}`);
    } catch (cause) {
      setError(getErrorMessage(cause));
      setUnverified(cause instanceof APIError && cause.code === 'email_not_verified');
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <AccountFrame title={t('account.login')} description={t('account.loginDescription')}>
      <form onSubmit={submit} className="space-y-4">
        {error && <AccountNotice error>{error}</AccountNotice>}
        <AccountField id="email" label={t('account.email')}>
          <input
            className="ui-input w-full"
            id="email"
            type="email"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            required
            maxLength={255}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </AccountField>
        <AccountField id="password" label={t('account.password')}>
          <PasswordInput
            className="ui-input w-full"
            id="password"
            autoComplete="current-password"
            required
            maxLength={1024}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </AccountField>
        <button className="ui-button ui-button-primary w-full" disabled={busy} aria-busy={busy}>
          {busy ? t('account.signingIn') : t('account.login')}
        </button>
      </form>
      {unverified && (
        <Link className="block text-sm text-accent underline" href="/resend-verification">
          {t('account.resend')}
        </Link>
      )}
      <div className="flex flex-wrap justify-between gap-3 text-sm text-accent">
        <Link className="underline" href="/forgot-password">
          {t('account.forgot')}
        </Link>
        {signupEnabled && (
          <Link className="underline" href="/signup">
            {t('account.signup')}
          </Link>
        )}
      </div>
    </AccountFrame>
  );
}

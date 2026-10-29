'use client';
// Email/password flow adapted from HybridInference 321e9a6c4 app/login/page.tsx;
// login names, phone codes and first-run setup from 8b49872d.
// Sessions remain HttpOnly cookies; there is no browser token storage.
import { useRef, useState, type FormEvent } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { useT } from '@/components/providers/LocaleProvider';
import { loginAccount, loginPhone, type SessionUserApiLike } from '@/lib/api/accounts';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { safeReturnTo } from '@/lib/auth/signin';
import { announceSession } from '@/lib/auth/sessionSync';
import { basePath } from '@/config/env';
import { AccountField, AccountFrame, AccountNotice } from './AccountFrame';
import { PasswordInput } from './PasswordInput';
import { PhoneCodeFields } from './PhoneCodeFields';
import { useAuthOptions } from './useAuthOptions';

function useSignedIn() {
  const params = useSearchParams();
  return (user: SessionUserApiLike) => {
    announceSession(user.id);
    const destination =
      user.agent_access?.allowed === true
        ? safeReturnTo(params?.get('return_to') ?? '/')
        : '/waiting-list';
    window.location.assign(`${basePath}${destination}`);
  };
}

function PhoneLogin({ countryCode }: { countryCode: string }) {
  const t = useT();
  const signedIn = useSignedIn();
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      signedIn(await loginPhone(phone.trim(), code));
    } catch (cause) {
      setError(getErrorMessage(cause));
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} className="space-y-4">
      {error && <AccountNotice error>{error}</AccountNotice>}
      <PhoneCodeFields
        purpose="login"
        phone={phone}
        code={code}
        onPhone={setPhone}
        onCode={setCode}
        onError={setError}
        defaultCountryCode={countryCode}
      />
      <button className="ui-button ui-button-primary w-full" disabled={busy || code.length !== 6}>
        {busy ? t('account.signingIn') : t('account.login')}
      </button>
    </form>
  );
}

function PasswordLogin() {
  const t = useT();
  const signedIn = useSignedIn();
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
      signedIn(user);
    } catch (cause) {
      setError(getErrorMessage(cause));
      setUnverified(cause instanceof APIError && cause.code === 'email_not_verified');
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <>
      <form onSubmit={submit} className="space-y-4">
        {error && <AccountNotice error>{error}</AccountNotice>}
        <AccountField id="email" label={t('account.email')}>
          <input
            className="ui-input w-full"
            id="email"
            type="text"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            required
            maxLength={255}
            aria-describedby="email-hint"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
          <p id="email-hint" className="mt-1 text-xs text-muted">
            {t('login.usernameHint')}
          </p>
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
    </>
  );
}

export function LocalLogin({ signupEnabled }: { signupEnabled: boolean }) {
  const t = useT();
  const params = useSearchParams();
  const options = useAuthOptions();
  const [method, setMethod] = useState<'password' | 'phone'>('password');
  const phone = options?.phone.enabled === true;
  const adding = params?.get('add') === '1';
  return (
    <AccountFrame
      title={adding ? t('login.addAccount') : t('account.login')}
      description={t('account.loginDescription')}
    >
      {options?.setup_required && (
        <AccountNotice>
          {t('login.setupRequired')}{' '}
          <Link className="text-accent underline" href="/setup/administrator">
            {t('setup.title')}
          </Link>
        </AccountNotice>
      )}
      {phone && (
        <div role="tablist" aria-label={t('login.method')} className="flex gap-2">
          {(['password', 'phone'] as const).map((value) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={method === value}
              className={`ui-button ${method === value ? 'ui-button-primary' : 'ui-button-secondary'}`}
              onClick={() => setMethod(value)}
            >
              {value === 'password' ? t('login.withPassword') : t('login.withPhone')}
            </button>
          ))}
        </div>
      )}
      {phone && method === 'phone' ? (
        <PhoneLogin countryCode={options?.phone.default_country_code ?? ''} />
      ) : (
        <PasswordLogin />
      )}
      <div className="flex flex-wrap justify-between gap-3 text-sm text-accent">
        {options?.email_delivery !== false && (
          <Link className="underline" href="/forgot-password">
            {t('account.forgot')}
          </Link>
        )}
        {(signupEnabled || options?.phone_signup_enabled) && (
          <Link className="underline" href="/signup">
            {t('account.signup')}
          </Link>
        )}
      </div>
    </AccountFrame>
  );
}

'use client';
// Email/password flow adapted from HybridInference 321e9a6c4 app/login/page.tsx;
// login names, phone codes and first-run setup from 8b49872d.
// Sessions remain HttpOnly cookies; there is no browser token storage.
import { useEffect, useRef, useState, type FormEvent } from 'react';
import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { useT } from '@/components/providers/LocaleProvider';
import {
  listBrowserAccounts,
  loginAccount,
  loginPhone,
  type SessionUserApiLike,
} from '@/lib/api/accounts';
import { APIError, getErrorMessage } from '@/lib/utils/errors';
import { safeReturnTo } from '@/lib/auth/signin';
import { announceSession } from '@/lib/auth/sessionSync';
import { AccountField, AccountNotice } from './AccountFrame';
import { AuthFormFrame } from './AuthFormFrame';
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
    window.location.assign(destination);
  };
}

function PhoneLogin({ countryCode, siteKey }: { countryCode: string; siteKey?: string | null }) {
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
        siteKey={siteKey}
      />
      <button className="ui-button ui-button-primary w-full" disabled={busy || code.length !== 6}>
        {busy ? t('account.signingIn') : t('account.login')}
      </button>
    </form>
  );
}

const LOGIN_LINK = 'ui-focus-ring rounded-sm font-medium text-accent hover:underline';

function ForgotPassword() {
  const t = useT();
  return (
    <Link className={`${LOGIN_LINK} text-[13px]`} href="/forgot-password">
      {t('account.forgot')}
    </Link>
  );
}

function PasswordLogin({ canResetPassword }: { canResetPassword: boolean }) {
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
        <AccountField id="email" label={t('login.identifier')}>
          <input
            className="ui-input w-full"
            id="email"
            type="text"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            required
            maxLength={255}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </AccountField>
        <AccountField
          id="password"
          label={t('account.password')}
          action={canResetPassword && <ForgotPassword />}
        >
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
        <Link className={`${LOGIN_LINK} inline-block text-sm`} href="/resend-verification">
          {t('account.resend')}
        </Link>
      )}
    </>
  );
}

/** How many accounts this browser already holds, for the chooser link. */
function useBrowserAccountCount(): number {
  const [count, setCount] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    void Promise.resolve()
      .then(() => listBrowserAccounts(controller.signal))
      .then((value) => {
        if (!controller.signal.aborted) setCount(value?.accounts?.length ?? 0);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);
  return count;
}

function AccountChooserLink({ count }: { count: number }) {
  const t = useT();
  return (
    <Link
      className="ui-focus-ring flex min-h-12 items-center gap-2.5 rounded-xl border border-border bg-surface-muted px-3 py-2 text-sm font-medium transition-colors hover:bg-surface-hover"
      href="/accounts"
    >
      <svg
        className="h-4 w-4 shrink-0 text-muted"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.6}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <circle cx="9" cy="8" r="3" />
        <path d="M3 19v-1a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v1m2-13a3 3 0 0 1 0 6m4 7v-1a4 4 0 0 0-3-3.9" />
      </svg>
      <span className="min-w-0 flex-1">{t('login.chooseAccount')}</span>
      <span
        className="min-w-6 rounded-full bg-surface-hover px-1.5 py-0.5 text-center text-xs text-muted"
        aria-hidden="true"
      >
        {count}
      </span>
      <svg
        className="h-4 w-4 shrink-0 text-muted"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
      >
        <path d="m6 4 4 4-4 4" />
      </svg>
    </Link>
  );
}

export function LocalLogin({ signupEnabled }: { signupEnabled: boolean }) {
  const t = useT();
  const params = useSearchParams();
  const options = useAuthOptions();
  const [method, setMethod] = useState<'password' | 'phone'>('password');
  const phone = options?.phone.enabled === true;
  const adding = params?.get('add') === '1';
  const others = useBrowserAccountCount();
  const canResetPassword = options?.email_delivery !== false;
  return (
    <AuthFormFrame
      title={adding ? t('login.addAccount') : t('account.login')}
      description={adding ? t('login.addDescription') : t('account.loginDescription')}
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
        <div role="tablist" aria-label={t('login.method')} className="auth-method-tabs">
          {(['password', 'phone'] as const).map((value) => (
            <button
              key={value}
              type="button"
              role="tab"
              aria-selected={method === value}
              className="ui-button auth-method-tab"
              onClick={() => setMethod(value)}
            >
              {value === 'password' ? t('login.withPassword') : t('login.withPhone')}
            </button>
          ))}
        </div>
      )}
      {phone && method === 'phone' ? (
        <>
          <PhoneLogin
            countryCode={options?.phone.default_country_code ?? ''}
            siteKey={options?.turnstile_site_key}
          />
          {canResetPassword && <ForgotPassword />}
        </>
      ) : (
        <PasswordLogin canResetPassword={canResetPassword} />
      )}
      {others > 0 && <AccountChooserLink count={others} />}
      {(signupEnabled || options?.phone_signup_enabled) && (
        <p className="border-t border-border pt-5 text-center text-sm text-muted">
          {t('login.newAccount')}{' '}
          <Link className={LOGIN_LINK} href="/signup">
            {t('account.signup')}
          </Link>
        </p>
      )}
    </AuthFormFrame>
  );
}

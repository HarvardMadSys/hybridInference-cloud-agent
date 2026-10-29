'use client';
import Link from 'next/link';
import { useRef, useState, type FormEvent } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { signupAccount, type SignupDetails } from '@/lib/api/accounts';
import { getErrorMessage } from '@/lib/utils/errors';
import { AccountField, AccountFrame, AccountNotice, BackToLogin } from './AccountFrame';
import { NewPasswordFields } from './NewPasswordFields';
import { validNewPassword } from './passwordPolicy';

/**
 * The email and password half of sign-up, without a frame. On the sign-up
 * page it follows the consent and details step, which supplies `details` (the
 * name, the use case, the agreed Terms and a captcha token); `verified` says
 * the deployment does not ask for email verification, so the account can sign
 * in at once. `onDone` lets the page show what happened.
 */
export function SignupEmailForm({
  details,
  verified = false,
  ready = true,
  onDone,
  onAttempt,
}: {
  details?: SignupDetails;
  verified?: boolean;
  ready?: boolean;
  onDone?: () => void;
  /** Called after each request, answered or not: its captcha token is spent. */
  onAttempt?: () => void;
}) {
  const t = useT();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    if (!validNewPassword(password)) {
      setError(t('account.passwordRules'));
      return;
    }
    if (password !== confirm) {
      setError(t('account.passwordMismatch'));
      return;
    }
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      if (details) await signupAccount(email.trim(), password, details);
      else await signupAccount(email.trim(), password);
      setPassword('');
      setConfirm('');
      setDone(true);
      onDone?.();
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      submitting.current = false;
      setBusy(false);
      onAttempt?.();
    }
  }
  if (done)
    return (
      <>
        <AccountNotice>
          {verified ? t('signup.createdVerified') : t('account.signupSent')}
        </AccountNotice>
        {!verified && (
          <Link href="/resend-verification" className="block text-sm text-accent underline">
            {t('account.resend')}
          </Link>
        )}
      </>
    );
  return (
    <form className="space-y-4" onSubmit={submit}>
      {error && <AccountNotice error>{error}</AccountNotice>}
      <AccountField id="email" label={t('account.email')}>
        <input
          id="email"
          className="ui-input w-full"
          type="email"
          autoComplete="email"
          autoCapitalize="none"
          required
          maxLength={255}
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
      </AccountField>
      <NewPasswordFields
        password={password}
        confirm={confirm}
        onPassword={setPassword}
        onConfirm={setConfirm}
      />
      <button
        className="ui-button ui-button-primary w-full"
        disabled={busy || !ready}
        aria-busy={busy}
      >
        {busy ? t('account.saving') : t('account.signup')}
      </button>
    </form>
  );
}

/** Email sign-up on its own page: the form in the account frame. */
export function SignupForm() {
  const t = useT();
  const [done, setDone] = useState(false);
  return (
    <AccountFrame
      title={done ? t('account.checkEmail') : t('account.signup')}
      description={done ? undefined : t('account.signupDescription')}
    >
      <SignupEmailForm onDone={() => setDone(true)} />
      <BackToLogin />
    </AccountFrame>
  );
}

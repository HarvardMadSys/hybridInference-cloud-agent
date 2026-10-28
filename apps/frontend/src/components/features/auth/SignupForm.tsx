'use client';
import Link from 'next/link';
import { useRef, useState, type FormEvent } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { signupAccount } from '@/lib/api/accounts';
import { getErrorMessage } from '@/lib/utils/errors';
import { AccountField, AccountFrame, AccountNotice, BackToLogin } from './AccountFrame';
import { NewPasswordFields } from './NewPasswordFields';
import { validNewPassword } from './passwordPolicy';

export function SignupForm() {
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
      await signupAccount(email.trim(), password);
      setPassword('');
      setConfirm('');
      setDone(true);
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <AccountFrame
      title={done ? t('account.checkEmail') : t('account.signup')}
      description={done ? undefined : t('account.signupDescription')}
    >
      {done ? (
        <>
          <AccountNotice>{t('account.signupSent')}</AccountNotice>
          <Link href="/resend-verification" className="block text-sm text-accent underline">
            {t('account.resend')}
          </Link>
        </>
      ) : (
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
          <button className="ui-button ui-button-primary w-full" disabled={busy} aria-busy={busy}>
            {busy ? t('account.saving') : t('account.signup')}
          </button>
        </form>
      )}
      <BackToLogin />
    </AccountFrame>
  );
}

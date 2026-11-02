'use client';
import { useRef, useState, type FormEvent } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { forgotPassword, resendVerification } from '@/lib/api/accounts';
import { getErrorMessage } from '@/lib/utils/errors';
import { AccountField, AccountFrame, AccountNotice, BackToLogin } from './AccountFrame';

export function EmailRequestForm({ purpose }: { purpose: 'reset' | 'verify' }) {
  const t = useT();
  const [email, setEmail] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      await (purpose === 'reset' ? forgotPassword(email.trim()) : resendVerification(email.trim()));
      setDone(true);
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <AccountFrame title={purpose === 'reset' ? t('account.forgot') : t('account.resend')}>
      {done ? (
        <AccountNotice>{t('account.mailSent')}</AccountNotice>
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
          <button className="ui-button ui-button-primary w-full" disabled={busy} aria-busy={busy}>
            {busy ? t('account.saving') : t('account.sendLink')}
          </button>
        </form>
      )}
      <BackToLogin />
    </AccountFrame>
  );
}

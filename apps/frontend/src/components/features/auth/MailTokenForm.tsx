'use client';
import { useRef, useState, type FormEvent } from 'react';
import Link from 'next/link';
import { useT } from '@/components/providers/LocaleProvider';
import { resetPassword, verifyAccountEmail } from '@/lib/api/accounts';
import { announceSession } from '@/lib/auth/sessionSync';
import { getErrorMessage } from '@/lib/utils/errors';
import { AccountFrame, AccountNotice, BackToLogin } from './AccountFrame';
import { NewPasswordFields } from './NewPasswordFields';
import { validNewPassword } from './passwordPolicy';
import { useMailToken } from './useMailToken';

export function MailTokenForm({ purpose }: { purpose: 'reset' | 'verify' }) {
  const t = useT();
  const token = useMailToken();
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [busy, setBusy] = useState(false);
  const [reclaimed, setReclaimed] = useState(false);
  const submitting = useRef(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current || !token) return;
    if (purpose === 'reset' && !validNewPassword(password)) {
      setError(t('account.passwordRules'));
      return;
    }
    if (purpose === 'reset' && password !== confirm) {
      setError(t('account.passwordMismatch'));
      return;
    }
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      if (purpose === 'reset') {
        const answer = await resetPassword(token, password);
        setReclaimed(answer.reclaimed === true);
        announceSession(null);
      } else await verifyAccountEmail(token);
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
      title={purpose === 'reset' ? t('account.resetPassword') : t('account.verifyEmail')}
    >
      {done ? (
        <>
          <AccountNotice>
            {purpose === 'reset' ? t('account.resetDone') : t('account.verifyDone')}
          </AccountNotice>
          {reclaimed && <AccountNotice>{t('account.resetReclaimed')}</AccountNotice>}
        </>
      ) : (
        <>
          {token === null ? (
            <p role="status">{t('common.loading')}</p>
          ) : !token ? (
            <AccountNotice error>{t('account.missingToken')}</AccountNotice>
          ) : (
            <form className="space-y-4" onSubmit={submit}>
              {error && <AccountNotice error>{error}</AccountNotice>}
              {purpose === 'reset' ? (
                <NewPasswordFields
                  password={password}
                  confirm={confirm}
                  onPassword={setPassword}
                  onConfirm={setConfirm}
                />
              ) : (
                <p className="text-sm text-muted">{t('account.verifyDescription')}</p>
              )}
              <button
                className="ui-button ui-button-primary w-full"
                disabled={busy}
                aria-busy={busy}
              >
                {busy
                  ? t('account.saving')
                  : purpose === 'reset'
                    ? t('account.resetPassword')
                    : t('account.verifyEmail')}
              </button>
            </form>
          )}
          <Link
            href={purpose === 'reset' ? '/forgot-password' : '/resend-verification'}
            className="block text-sm text-accent underline"
          >
            {t('account.newLink')}
          </Link>
        </>
      )}
      <BackToLogin />
    </AccountFrame>
  );
}

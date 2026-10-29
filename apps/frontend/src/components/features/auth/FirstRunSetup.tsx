'use client';
// First-run setup, adapted from HybridInference app/setup/CreateAdminStep.tsx:
// the one-time code from the control plane's startup log creates the first
// administrator, who signs in with a login name and needs no email or SMTP.
import { useState, type FormEvent } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { createSetupAdmin } from '@/lib/api/accounts';
import { announceSession } from '@/lib/auth/sessionSync';
import { basePath } from '@/config/env';
import { getErrorMessage } from '@/lib/utils/errors';
import { AccountField, AccountFrame, AccountNotice, BackToLogin } from './AccountFrame';
import { NewPasswordFields } from './NewPasswordFields';
import { validNewPassword } from './passwordPolicy';
import { useAuthOptions } from './useAuthOptions';

export function FirstRunSetup() {
  const t = useT();
  const options = useAuthOptions();
  const [code, setCode] = useState('');
  const [loginName, setLoginName] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!validNewPassword(password)) return setError(t('account.passwordRules'));
    if (password !== confirm) return setError(t('account.passwordMismatch'));
    setBusy(true);
    setError(null);
    try {
      const user = await createSetupAdmin({
        setup_code: code.trim(),
        login_name: loginName.trim(),
        password,
        display_name: displayName.trim() || undefined,
      });
      announceSession(user.id);
      window.location.assign(`${basePath}/admin/accounts`);
    } catch (cause) {
      setError(getErrorMessage(cause));
      setBusy(false);
    }
  }
  if (options && !options.setup_required)
    return (
      <AccountFrame title={t('setup.title')}>
        <AccountNotice>{t('setup.done')}</AccountNotice>
        <BackToLogin />
      </AccountFrame>
    );
  return (
    <AccountFrame title={t('setup.title')} description={t('setup.description')}>
      <form className="space-y-4" onSubmit={submit}>
        {error && <AccountNotice error>{error}</AccountNotice>}
        <AccountField id="setup-code" label={t('setup.code')}>
          <input
            id="setup-code"
            className="ui-input w-full font-mono"
            autoComplete="off"
            required
            maxLength={64}
            value={code}
            onChange={(e) => setCode(e.target.value)}
          />
        </AccountField>
        <AccountField id="login-name" label={t('setup.loginName')}>
          <input
            id="login-name"
            className="ui-input w-full"
            autoComplete="username"
            autoCapitalize="none"
            required
            pattern="[a-zA-Z0-9][a-zA-Z0-9_.\-]{2,31}"
            maxLength={32}
            value={loginName}
            onChange={(e) => setLoginName(e.target.value)}
          />
        </AccountField>
        <AccountField id="display-name" label={t('signup.name')}>
          <input
            id="display-name"
            className="ui-input w-full"
            maxLength={50}
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </AccountField>
        <NewPasswordFields
          password={password}
          confirm={confirm}
          onPassword={setPassword}
          onConfirm={setConfirm}
        />
        <button className="ui-button ui-button-primary w-full" disabled={busy}>
          {busy ? t('account.saving') : t('setup.create')}
        </button>
      </form>
    </AccountFrame>
  );
}

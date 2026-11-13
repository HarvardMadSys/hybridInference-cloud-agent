'use client';
// First-run setup, adapted from HybridInference app/setup/CreateAdminStep.tsx:
// the one-time code from the control plane's startup log creates the first
// administrator, who signs in with a login name and needs no email or SMTP.
// The new administrator then confirms the site address, the one setting a
// deployment still needs from a person, and goes on to add the first server.
import { useState, type FormEvent } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { createSetupAdmin } from '@/lib/api/accounts';
import { announceSession } from '@/lib/auth/sessionSync';
import { getErrorMessage } from '@/lib/utils/errors';
import {
  AccountField,
  AccountFrame,
  AccountNotice,
  AccountSteps,
  BackToLogin,
} from './AccountFrame';
import { NewPasswordFields } from './NewPasswordFields';
import { validNewPassword } from './passwordPolicy';
import { SiteAddressStep } from './SiteAddressStep';
import { useAuthOptions } from './useAuthOptions';

/** Where setup ends: Admin → Servers, where the first server is added. */
const FINISH = '/admin/hosts';

function SetupSteps({ current }: { current: number }) {
  const t = useT();
  return (
    <AccountSteps
      steps={[t('setup.stepAdmin'), t('setup.stepSite'), t('setup.stepServer')]}
      current={current}
    />
  );
}

export function FirstRunSetup() {
  const [step, setStep] = useState<'admin' | 'site'>('admin');
  if (step === 'site')
    return (
      <SiteAddressStep
        steps={<SetupSteps current={1} />}
        onDone={() => window.location.assign(FINISH)}
      />
    );
  return <AdministratorStep onCreated={() => setStep('site')} />;
}

function AdministratorStep({ onCreated }: { onCreated: () => void }) {
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
      onCreated();
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
    <AccountFrame
      title={t('setup.title')}
      description={t('setup.description')}
      steps={<SetupSteps current={0} />}
    >
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

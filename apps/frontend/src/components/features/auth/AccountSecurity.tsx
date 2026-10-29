'use client';
// The signed-in local account's own security, adapted from HybridInference
// app/dashboard/settings (EmailBindForm, PhoneAuthForm purpose="bind", the
// change/set password form with OwnerPhoneCode, and DangerZone/DeleteAccount).
// Each change is proven again as the account signs in: its password, or a
// code sent to its verified phone when it has none.
import Link from 'next/link';
import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import type { AccountIdentity, AuthOptions } from '@/lib/api/accounts';
import {
  bindPhone,
  changePassword,
  deleteOwnAccount,
  getOwnAccount,
  requestEmailBind,
  type OwnerProof,
} from '@/lib/api/accountSelf';
import { announceSession } from '@/lib/auth/sessionSync';
import { basePath } from '@/config/env';
import { getErrorMessage } from '@/lib/utils/errors';
import { AccountField, AccountNotice } from './AccountFrame';
import { NewPasswordFields } from './NewPasswordFields';
import { PasswordInput } from './PasswordInput';
import { PhoneCodeFields } from './PhoneCodeFields';
import { validNewPassword } from './passwordPolicy';
import { useAuthOptions } from './useAuthOptions';

type Purpose = 'set_password' | 'bind_email' | 'delete_account';

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-3 rounded-xl border border-border bg-surface p-4">
      <h2 className="text-base font-semibold">{title}</h2>
      {children}
    </section>
  );
}

/** The current password, or a code sent to the account's own verified phone. */
function OwnerProofFields({
  account,
  purpose,
  proof,
  onProof,
  onError,
}: {
  account: AccountIdentity;
  purpose: Purpose;
  proof: OwnerProof;
  onProof: (proof: OwnerProof) => void;
  onError: (message: string | null) => void;
}) {
  const t = useT();
  if (account.has_password)
    return (
      <AccountField id={`proof-${purpose}`} label={t('security.currentPassword')}>
        <PasswordInput
          id={`proof-${purpose}`}
          className="ui-input w-full"
          autoComplete="current-password"
          required
          maxLength={1024}
          value={proof.password ?? ''}
          onChange={(e) => onProof({ password: e.target.value })}
        />
      </AccountField>
    );
  if (account.phone)
    return (
      <PhoneCodeFields
        purpose={purpose}
        phone={account.phone}
        fixedPhone={account.phone}
        code={proof.phone_code ?? ''}
        onPhone={() => undefined}
        onCode={(code) => onProof({ phone_code: code })}
        onError={onError}
      />
    );
  return <p className="text-sm text-muted">{t('security.noProof')}</p>;
}

function useAction() {
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function run(action: () => Promise<string | null>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      setNotice(await action());
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return { error, setError, notice, busy, run };
}

function PasswordSection({
  account,
  onChanged,
}: {
  account: AccountIdentity;
  onChanged: () => void;
}) {
  const t = useT();
  const { error, setError, notice, busy, run } = useAction();
  const [proof, setProof] = useState<OwnerProof>({});
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!validNewPassword(password)) return setError(t('account.passwordRules'));
    if (password !== confirm) return setError(t('account.passwordMismatch'));
    void run(async () => {
      await changePassword({
        old_password: proof.password,
        phone_code: proof.phone_code,
        new_password: password,
      });
      setPassword('');
      setConfirm('');
      setProof({});
      onChanged();
      return t('security.passwordChanged');
    });
  }
  if (!account.has_password && !account.phone) return null;
  return (
    <Section
      title={account.has_password ? t('security.changePassword') : t('security.setPassword')}
    >
      <form className="space-y-3" onSubmit={submit}>
        {error && <AccountNotice error>{error}</AccountNotice>}
        {notice && <AccountNotice>{notice}</AccountNotice>}
        <OwnerProofFields
          account={account}
          purpose="set_password"
          proof={proof}
          onProof={setProof}
          onError={setError}
        />
        <NewPasswordFields
          password={password}
          confirm={confirm}
          onPassword={setPassword}
          onConfirm={setConfirm}
        />
        <button className="ui-button ui-button-primary" disabled={busy}>
          {t('security.savePassword')}
        </button>
      </form>
    </Section>
  );
}

function EmailSection({
  account,
  options,
}: {
  account: AccountIdentity;
  options: AuthOptions | null;
}) {
  const t = useT();
  const { error, setError, notice, busy, run } = useAction();
  const [proof, setProof] = useState<OwnerProof>({});
  const [email, setEmail] = useState('');
  function submit(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      const answer = await requestEmailBind(email.trim(), proof);
      setProof({});
      return answer.email_verified
        ? t('security.emailAlready')
        : t('security.emailSent', { email: answer.email });
    });
  }
  const emailKey = account.email_verified ? 'security.emailVerified' : 'security.emailUnverified';
  const emailLine = account.email ? t(emailKey, { email: account.email }) : t('security.emailNone');
  return (
    <Section title={t('security.email')}>
      <p className="text-sm text-muted">{emailLine}</p>
      {options?.email_delivery === false ? (
        <p className="text-sm text-muted">{t('security.emailUnavailable')}</p>
      ) : (
        <form className="space-y-3" onSubmit={submit}>
          {error && <AccountNotice error>{error}</AccountNotice>}
          {notice && <AccountNotice>{notice}</AccountNotice>}
          <AccountField id="new-email" label={t('security.newEmail')}>
            <input
              id="new-email"
              className="ui-input w-full"
              type="email"
              autoComplete="email"
              required
              maxLength={255}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </AccountField>
          <OwnerProofFields
            account={account}
            purpose="bind_email"
            proof={proof}
            onProof={setProof}
            onError={setError}
          />
          <button className="ui-button ui-button-primary" disabled={busy}>
            {t('security.sendLink')}
          </button>
        </form>
      )}
    </Section>
  );
}

function PhoneSection({
  account,
  options,
  onChanged,
}: {
  account: AccountIdentity;
  options: AuthOptions | null;
  onChanged: () => void;
}) {
  const t = useT();
  const { error, setError, notice, busy, run } = useAction();
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  if (!account.phone && !options?.phone.enabled) return null;
  function submit(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      await bindPhone(phone.trim(), code);
      onChanged();
      return t('security.phoneBound');
    });
  }
  return (
    <Section title={t('security.phone')}>
      {account.phone ? (
        <p className="text-sm text-muted">
          {t('security.phoneVerified', { phone: account.phone })}
        </p>
      ) : (
        <form className="space-y-3" onSubmit={submit}>
          {error && <AccountNotice error>{error}</AccountNotice>}
          {notice && <AccountNotice>{notice}</AccountNotice>}
          <PhoneCodeFields
            purpose="bind"
            phone={phone}
            code={code}
            onPhone={setPhone}
            onCode={setCode}
            onError={setError}
            defaultCountryCode={options?.phone.default_country_code}
          />
          <button className="ui-button ui-button-primary" disabled={busy || code.length !== 6}>
            {t('security.bindPhone')}
          </button>
        </form>
      )}
    </Section>
  );
}

function DeleteSection({ account, isAdmin }: { account: AccountIdentity; isAdmin: boolean }) {
  const t = useT();
  const { error, setError, busy, run } = useAction();
  const [proof, setProof] = useState<OwnerProof>({});
  const [confirm, setConfirm] = useState('');
  function submit(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      await deleteOwnAccount(confirm.trim(), proof);
      announceSession(null);
      window.location.assign(`${basePath}/login`);
      return null;
    });
  }
  return (
    <Section title={t('security.dangerZone')}>
      <p className="text-sm text-muted">{t('security.deleteExplanation')}</p>
      {isAdmin ? (
        <p className="text-sm text-muted">{t('security.adminCannotDelete')}</p>
      ) : (
        <form className="space-y-3" onSubmit={submit}>
          {error && <AccountNotice error>{error}</AccountNotice>}
          <AccountField
            id="delete-confirm"
            label={t('security.typeToConfirm', { label: account.label })}
          >
            <input
              id="delete-confirm"
              className="ui-input w-full"
              autoComplete="off"
              required
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
          </AccountField>
          <OwnerProofFields
            account={account}
            purpose="delete_account"
            proof={proof}
            onProof={setProof}
            onError={setError}
          />
          <button
            className="ui-button ui-button-danger"
            disabled={busy || confirm.trim().toLowerCase() !== account.label.toLowerCase()}
          >
            {t('security.delete')}
          </button>
        </form>
      )}
    </Section>
  );
}

export function AccountSecurity({ isAdmin = false }: { isAdmin?: boolean }) {
  const t = useT();
  const options = useAuthOptions();
  const [account, setAccount] = useState<AccountIdentity | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      setAccount(await getOwnAccount());
    } catch (cause) {
      setError(getErrorMessage(cause));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  if (error) return <AccountNotice error>{error}</AccountNotice>;
  if (!account) return <p role="status">{t('common.loading')}</p>;
  return (
    <div className="space-y-4">
      <Section title={t('security.signInWith')}>
        <ul className="space-y-1 text-sm">
          {account.email && <li>{t('security.methodEmail', { value: account.email })}</li>}
          {account.login_name && <li>{t('security.methodName', { value: account.login_name })}</li>}
          {account.phone && <li>{t('security.methodPhone', { value: account.phone })}</li>}
        </ul>
        <Link className="text-sm text-accent underline" href="/accounts">
          {t('accounts.title')}
        </Link>
      </Section>
      <PasswordSection account={account} onChanged={() => void load()} />
      <EmailSection account={account} options={options} />
      <PhoneSection account={account} options={options} onChanged={() => void load()} />
      <DeleteSection account={account} isAdmin={isAdmin} />
    </div>
  );
}

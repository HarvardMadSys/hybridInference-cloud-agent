'use client';
// The signed-in local account's own settings, adapted from HybridInference
// app/dashboard/settings (EmailBindForm, PhoneAuthForm purpose="bind", the
// change/set password form with OwnerPhoneCode, and DangerZone/DeleteAccount).
// Each change is proven again as the account signs in: its password, or a
// code sent to its verified phone when it has none. Every row shows what the
// account has; its form opens in place only when asked for, and closing it
// unmounts the form, so nothing typed into it (a password, a code) is kept.
import {
  useCallback,
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
} from 'react';
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
import { getErrorMessage } from '@/lib/utils/errors';
import { AccountField, AccountNotice } from './AccountFrame';
import { NewPasswordFields } from './NewPasswordFields';
import { PasswordInput } from './PasswordInput';
import { PhoneCodeFields } from './PhoneCodeFields';
import { validNewPassword } from './passwordPolicy';
import { useAuthOptions } from './useAuthOptions';

type Purpose = 'set_password' | 'bind_email' | 'delete_account';

/** What an open form is given: close with a notice for its row, or cancel. */
interface FormProps {
  onDone: (notice: string | null) => void;
  onCancel: () => void;
}

const PANEL = 'overflow-hidden rounded-xl border border-border bg-surface';

/** Rows under one heading; without `heading`, the rows name themselves. */
function Panel({
  label,
  heading = false,
  children,
}: {
  label: string;
  heading?: boolean;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <section aria-labelledby={heading ? id : undefined} aria-label={heading ? undefined : label}>
      {heading && (
        <h2 id={id} className="mb-3 text-base font-medium text-foreground">
          {label}
        </h2>
      )}
      <div className={PANEL}>{children}</div>
    </section>
  );
}

function Badge({ ok, children }: { ok: boolean; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${ok ? 'bg-status-success/10 text-status-success' : 'bg-surface-muted text-muted'}`}
    >
      {children}
    </span>
  );
}

/**
 * One labelled row. With `form`, its action (short visible text, a full
 * accessible name) opens that form below the value; the form's Cancel or Esc
 * closes it, and focus returns to the action.
 */
function AccountRow({
  label,
  value,
  action,
  actionName,
  danger = false,
  form,
}: {
  label: string;
  value: ReactNode;
  action?: string;
  actionName?: string;
  danger?: boolean;
  form?: (props: FormProps) => ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const button = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const opened = useRef(false);
  useEffect(() => {
    if (open) panel.current?.querySelector<HTMLElement>('input, button')?.focus();
    else if (opened.current) button.current?.focus();
    opened.current = open;
  }, [open]);
  const close = useCallback((message: string | null) => {
    setNotice(message);
    setOpen(false);
  }, []);
  return (
    <div className="border-b border-border px-4 py-4 last:border-b-0 sm:px-5">
      <div className="flex items-center justify-between gap-4">
        <div className="grid min-h-8 min-w-0 flex-1 content-center gap-1 sm:grid-cols-[10rem_minmax(0,1fr)] sm:gap-4">
          <span className="text-sm font-medium text-foreground">{label}</span>
          <div className="flex min-w-0 flex-wrap items-center gap-2 break-words text-sm text-muted">
            {value}
          </div>
        </div>
        {form && action && !open && (
          <button
            ref={button}
            type="button"
            aria-label={actionName}
            className={`ui-button ui-button-secondary shrink-0 ${danger ? 'text-red-700 dark:text-red-300' : ''}`}
            onClick={() => {
              setNotice(null);
              setOpen(true);
            }}
          >
            {action}
          </button>
        )}
      </div>
      {notice && (
        <div className="mt-3 sm:pl-[11rem]">
          <AccountNotice>{notice}</AccountNotice>
        </div>
      )}
      {form && open && (
        <div
          ref={panel}
          role="group"
          aria-label={actionName}
          className="mt-4 max-w-md sm:ml-[11rem]"
        >
          {form({ onDone: close, onCancel: () => close(null) })}
        </div>
      )}
    </div>
  );
}

/** Submit and Cancel. Cancel waits while a request is out, as does Esc. */
function FormActions({
  submit,
  busy,
  disabled = false,
  danger = false,
  onCancel,
}: {
  submit: string;
  busy: boolean;
  disabled?: boolean;
  danger?: boolean;
  onCancel: () => void;
}) {
  const t = useT();
  return (
    <div className="flex flex-wrap items-center gap-2 pt-1">
      <button
        type="submit"
        className={`ui-button ${danger ? 'ui-button-danger' : 'ui-button-primary'}`}
        disabled={busy || disabled}
      >
        {submit}
      </button>
      <button
        type="button"
        className="ui-button ui-button-ghost"
        disabled={busy}
        onClick={onCancel}
      >
        {t('security.cancel')}
      </button>
    </div>
  );
}

function escapeCancels(busy: boolean, onCancel: () => void) {
  return (event: KeyboardEvent<HTMLFormElement>) => {
    if (event.key !== 'Escape' || busy) return;
    event.stopPropagation();
    onCancel();
  };
}

/** The current password, or a code sent to the account's own verified phone. */
function OwnerProofFields({
  account,
  purpose,
  proof,
  onProof,
  onError,
  siteKey,
}: {
  account: AccountIdentity;
  purpose: Purpose;
  proof: OwnerProof;
  onProof: (proof: OwnerProof) => void;
  onError: (message: string | null) => void;
  siteKey?: string | null;
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
        siteKey={siteKey}
      />
    );
  return <p className="text-sm text-muted">{t('security.noProof')}</p>;
}

function useAction() {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return { error, setError, busy, run };
}

function PasswordForm({
  account,
  options,
  onChanged,
  onDone,
  onCancel,
}: FormProps & {
  account: AccountIdentity;
  options: AuthOptions | null;
  onChanged: () => void;
}) {
  const t = useT();
  const { error, setError, busy, run } = useAction();
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
      onDone(t('security.passwordChanged'));
      onChanged();
    });
  }
  return (
    <form className="space-y-3" onSubmit={submit} onKeyDown={escapeCancels(busy, onCancel)}>
      {error && <AccountNotice error>{error}</AccountNotice>}
      <OwnerProofFields
        account={account}
        purpose="set_password"
        proof={proof}
        onProof={setProof}
        onError={setError}
        siteKey={options?.turnstile_site_key}
      />
      <NewPasswordFields
        password={password}
        confirm={confirm}
        onPassword={setPassword}
        onConfirm={setConfirm}
      />
      <FormActions submit={t('security.savePassword')} busy={busy} onCancel={onCancel} />
    </form>
  );
}

function EmailForm({
  account,
  options,
  onDone,
  onCancel,
}: FormProps & { account: AccountIdentity; options: AuthOptions | null }) {
  const t = useT();
  const { error, setError, busy, run } = useAction();
  const [proof, setProof] = useState<OwnerProof>({});
  const [email, setEmail] = useState('');
  function submit(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      const answer = await requestEmailBind(email.trim(), proof);
      onDone(
        answer.email_verified
          ? t('security.emailAlready')
          : t('security.emailSent', { email: answer.email }),
      );
    });
  }
  return (
    <form className="space-y-3" onSubmit={submit} onKeyDown={escapeCancels(busy, onCancel)}>
      {error && <AccountNotice error>{error}</AccountNotice>}
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
        siteKey={options?.turnstile_site_key}
      />
      <FormActions submit={t('security.sendLink')} busy={busy} onCancel={onCancel} />
    </form>
  );
}

function PhoneForm({
  options,
  onChanged,
  onDone,
  onCancel,
}: FormProps & { options: AuthOptions | null; onChanged: () => void }) {
  const t = useT();
  const { error, setError, busy, run } = useAction();
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  function submit(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      await bindPhone(phone.trim(), code);
      onDone(t('security.phoneBound'));
      onChanged();
    });
  }
  return (
    <form className="space-y-3" onSubmit={submit} onKeyDown={escapeCancels(busy, onCancel)}>
      {error && <AccountNotice error>{error}</AccountNotice>}
      <PhoneCodeFields
        purpose="bind"
        phone={phone}
        code={code}
        onPhone={setPhone}
        onCode={setCode}
        onError={setError}
        defaultCountryCode={options?.phone.default_country_code}
        siteKey={options?.turnstile_site_key}
      />
      <FormActions
        submit={t('security.bindPhone')}
        busy={busy}
        disabled={code.length !== 6}
        onCancel={onCancel}
      />
    </form>
  );
}

function DeleteForm({
  account,
  siteKey,
  onCancel,
}: FormProps & { account: AccountIdentity; siteKey?: string | null }) {
  const t = useT();
  const { error, setError, busy, run } = useAction();
  const [proof, setProof] = useState<OwnerProof>({});
  const [confirm, setConfirm] = useState('');
  function submit(event: FormEvent) {
    event.preventDefault();
    void run(async () => {
      await deleteOwnAccount(confirm.trim(), proof);
      announceSession(null);
      window.location.assign('/login');
    });
  }
  return (
    <form className="space-y-3" onSubmit={submit} onKeyDown={escapeCancels(busy, onCancel)}>
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
        siteKey={siteKey}
      />
      <FormActions
        submit={t('security.delete')}
        busy={busy}
        danger
        disabled={confirm.trim().toLowerCase() !== account.label.toLowerCase()}
        onCancel={onCancel}
      />
    </form>
  );
}

function AccountSummary({ account, isAdmin }: { account: AccountIdentity; isAdmin: boolean }) {
  const t = useT();
  const name = account.user_name?.trim() || account.label;
  // A phone number's "+" is no initial: show a person instead.
  const initial = /^\p{L}/u.test(name) ? name.charAt(0).toUpperCase() : null;
  return (
    <div className="flex items-center gap-4">
      <div
        aria-hidden="true"
        className="flex h-12 w-12 shrink-0 items-center justify-center rounded-full bg-surface-muted text-lg font-medium text-foreground"
      >
        {initial ?? (
          <svg
            className="h-5 w-5 text-muted"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.6}
          >
            <path
              d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM5 20a7 7 0 0 1 14 0"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        )}
      </div>
      <div className="min-w-0 flex-1">
        <p className="flex flex-wrap items-center gap-2 text-base font-medium text-foreground">
          <span className="min-w-0 break-all">{name}</span>
          {isAdmin && <Badge ok={false}>{t('security.administrator')}</Badge>}
        </p>
        {name !== account.label && (
          <p className="mt-0.5 break-all text-sm text-muted">{account.label}</p>
        )}
      </div>
    </div>
  );
}

interface RowProps {
  account: AccountIdentity;
  options: AuthOptions | null;
  onChanged: () => void;
}

function EmailRow({ account, options }: Omit<RowProps, 'onChanged'>) {
  const t = useT();
  const canChange = options?.email_delivery !== false;
  return (
    <AccountRow
      label={t('security.emailLabel')}
      value={
        <>
          <span className="min-w-0 break-all">{account.email ?? t('security.notAdded')}</span>
          {account.email && (
            <Badge ok={account.email_verified}>
              {account.email_verified ? t('security.verified') : t('security.unverified')}
            </Badge>
          )}
          {!canChange && <span className="w-full text-xs">{t('security.emailUnavailable')}</span>}
        </>
      }
      action={canChange ? (account.email ? t('security.change') : t('security.add')) : undefined}
      actionName={account.email ? t('security.changeEmail') : t('security.addEmail')}
      form={(props) => <EmailForm account={account} options={options} {...props} />}
    />
  );
}

function PhoneRow({ account, options, onChanged }: RowProps) {
  const t = useT();
  if (!account.phone && !options?.phone.enabled) return null;
  return (
    <AccountRow
      label={t('security.phoneLabel')}
      value={
        account.phone ? (
          <>
            <span className="min-w-0 break-all">{account.phone}</span>
            <Badge ok>{t('security.verified')}</Badge>
          </>
        ) : (
          t('security.notAdded')
        )
      }
      action={account.phone ? undefined : t('security.add')}
      actionName={t('security.bindPhone')}
      form={(props) => <PhoneForm options={options} onChanged={onChanged} {...props} />}
    />
  );
}

function PasswordRow({ account, options, onChanged }: RowProps) {
  const t = useT();
  if (!account.has_password && !account.phone) return null;
  return (
    <AccountRow
      label={t('security.passwordLabel')}
      value={account.has_password ? t('security.passwordSet') : t('security.passwordUnset')}
      action={account.has_password ? t('security.change') : t('security.set')}
      actionName={account.has_password ? t('security.changePassword') : t('security.setPassword')}
      form={(props) => (
        <PasswordForm account={account} options={options} onChanged={onChanged} {...props} />
      )}
    />
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
  const reload = () => void load();
  if (error) return <AccountNotice error>{error}</AccountNotice>;
  if (!account) return <p role="status">{t('common.loading')}</p>;
  return (
    <div className="mt-6 space-y-8">
      <AccountSummary account={account} isAdmin={isAdmin} />
      <Panel label={t('security.signIn')} heading>
        {account.login_name && (
          <AccountRow label={t('security.username')} value={account.login_name} />
        )}
        <EmailRow account={account} options={options} />
        <PhoneRow account={account} options={options} onChanged={reload} />
        <PasswordRow account={account} options={options} onChanged={reload} />
      </Panel>
      <Panel label={t('security.dangerZone')}>
        <AccountRow
          label={t('security.dangerZone')}
          value={isAdmin ? t('security.adminCannotDelete') : t('security.deleteExplanation')}
          action={isAdmin ? undefined : t('security.deleteAction')}
          actionName={t('security.dangerZone')}
          danger
          form={(props) => (
            <DeleteForm account={account} siteKey={options?.turnstile_site_key} {...props} />
          )}
        />
      </Panel>
    </div>
  );
}

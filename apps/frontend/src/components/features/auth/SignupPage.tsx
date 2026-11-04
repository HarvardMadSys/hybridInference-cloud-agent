'use client';
// Sign-up as HybridInference offers it: agree to the Terms, say who you are and
// how you will use Cloud Agent, then create the account with an email and a
// password, or with a phone number and a code. The use case goes with the
// sign-up as the Agent application; signing up never grants Agent use.
import Link from 'next/link';
import { useCallback, useRef, useState, type FormEvent } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import {
  signupAccount,
  signupPhone,
  type AuthOptions,
  type SignupDetails,
} from '@/lib/api/accounts';
import { getErrorMessage } from '@/lib/utils/errors';
import {
  AccountField,
  AccountFrame,
  AccountNotice,
  AccountSteps,
  BackToLogin,
} from './AccountFrame';
import { NewPasswordFields } from './NewPasswordFields';
import { PhoneCodeFields } from './PhoneCodeFields';
import { TermsConsent } from './TermsConsent';
import { TurnstileWidget } from './TurnstileWidget';
import { validNewPassword } from './passwordPolicy';
import { useAuthOptions } from './useAuthOptions';

type SignupMethod = 'email' | 'phone';

function MethodTabs({
  method,
  onMethod,
}: {
  method: SignupMethod;
  onMethod: (value: SignupMethod) => void;
}) {
  const t = useT();
  return (
    <div role="tablist" aria-label={t('signup.method')} className="flex gap-2">
      {(['email', 'phone'] as const).map((value) => (
        <button
          key={value}
          type="button"
          role="tab"
          aria-selected={method === value}
          className={`ui-button ${method === value ? 'ui-button-primary' : 'ui-button-secondary'}`}
          onClick={() => onMethod(value)}
        >
          {value === 'email' ? t('account.email') : t('phone.number')}
        </button>
      ))}
    </div>
  );
}

function chooseMethod(method: SignupMethod, emailOpen: boolean, phoneOpen: boolean): SignupMethod {
  if (method === 'phone' && phoneOpen) return 'phone';
  return emailOpen ? 'email' : 'phone';
}

function signupDetails(
  userName: string,
  useCase: string,
  accepted: boolean,
  captcha: string | null,
): SignupDetails {
  return {
    user_name: userName.trim() || undefined,
    use_case: useCase.trim(),
    accepted_tos: accepted,
    turnstile_token: captcha ?? undefined,
  };
}

function SignupClosed() {
  const t = useT();
  return (
    <AccountFrame title={t('account.unavailable')}>
      <p className="text-sm text-muted">{t('account.registrationClosed')}</p>
      <BackToLogin />
    </AccountFrame>
  );
}

/**
 * Account, then the email check when an email sign-up needs one, then access.
 * A phone sign-up proves its number with the code, so it has no check to wait for.
 */
function SignupSteps({
  method,
  done,
  verifyEmail,
}: {
  method: SignupMethod;
  done: SignupMethod | null;
  verifyEmail: boolean;
}) {
  const t = useT();
  const steps = [t('signup.stepAccount'), t('signup.stepAccess')];
  if ((done ?? method) === 'email' && verifyEmail) steps.splice(1, 0, t('signup.stepVerify'));
  return <AccountSteps steps={steps} current={done ? 1 : 0} />;
}

function SignupCompleted({
  done,
  verifyEmail,
  phoneRequiresApproval,
}: {
  done: SignupMethod;
  verifyEmail: boolean;
  phoneRequiresApproval: boolean;
}) {
  const t = useT();
  if (done === 'phone') {
    return (
      <AccountNotice>
        {phoneRequiresApproval ? t('signup.phoneWaiting') : t('signup.createdVerified')}
      </AccountNotice>
    );
  }
  return (
    <>
      <AccountNotice>
        {verifyEmail ? t('account.signupSent') : t('signup.createdVerified')}
      </AccountNotice>
      {verifyEmail && (
        <Link href="/resend-verification" className="block text-sm text-accent underline">
          {t('account.resend')}
        </Link>
      )}
    </>
  );
}

function EmailCredentialFields({
  email,
  password,
  confirm,
  onEmail,
  onPassword,
  onConfirm,
}: {
  email: string;
  password: string;
  confirm: string;
  onEmail: (value: string) => void;
  onPassword: (value: string) => void;
  onConfirm: (value: string) => void;
}) {
  const t = useT();
  return (
    <>
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
          onChange={(e) => onEmail(e.target.value)}
        />
      </AccountField>
      <NewPasswordFields
        password={password}
        confirm={confirm}
        onPassword={onPassword}
        onConfirm={onConfirm}
      />
    </>
  );
}

function ProfileFields({
  userName,
  useCase,
  onUserName,
  onUseCase,
}: {
  userName: string;
  useCase: string;
  onUserName: (value: string) => void;
  onUseCase: (value: string) => void;
}) {
  const t = useT();
  return (
    <>
      <AccountField id="user-name" label={t('signup.name')}>
        <input
          id="user-name"
          className="ui-input w-full"
          autoComplete="name"
          minLength={2}
          maxLength={50}
          value={userName}
          onChange={(e) => onUserName(e.target.value)}
        />
      </AccountField>
      <AccountField id="use-case" label={t('waitlist.purpose')}>
        <textarea
          id="use-case"
          className="ui-input w-full"
          rows={3}
          maxLength={2000}
          value={useCase}
          onChange={(e) => onUseCase(e.target.value)}
        />
      </AccountField>
    </>
  );
}

function ActiveSignupForm({
  options,
  chosen,
  onMethod,
  onDone,
}: {
  options: AuthOptions;
  chosen: SignupMethod;
  onMethod: (value: SignupMethod) => void;
  onDone: (method: SignupMethod, requiresApproval?: boolean) => void;
}) {
  const t = useT();
  const [accepted, setAccepted] = useState(false);
  const [userName, setUserName] = useState('');
  const [useCase, setUseCase] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [captcha, setCaptcha] = useState<string | null>(null);
  const [captchaReset, setCaptchaReset] = useState(0);
  const submitting = useRef(false);
  const onToken = useCallback((token: string | null) => setCaptcha(token), []);
  const resetCaptcha = useCallback(() => {
    setCaptcha(null);
    setCaptchaReset((value) => value + 1);
  }, []);
  const needsCaptcha = Boolean(options.turnstile_site_key);
  const details = signupDetails(userName, useCase, accepted, captcha);
  const ready = accepted && (!needsCaptcha || Boolean(captcha));
  const submitDisabled = busy || (chosen === 'email' ? !ready : !accepted || code.length !== 6);

  async function submitEmail() {
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
      await signupAccount(email.trim(), password, details);
      setPassword('');
      setConfirm('');
      onDone('email');
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      submitting.current = false;
      setBusy(false);
      if (needsCaptcha) resetCaptcha();
    }
  }

  async function submitPhone() {
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      const res = await signupPhone({
        phone: phone.trim(),
        code,
        user_name: details.user_name,
        use_case: details.use_case,
        accepted_tos: details.accepted_tos,
      });
      onDone('phone', res.requires_approval);
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    void (chosen === 'email' ? submitEmail() : submitPhone());
  }

  return (
    <>
      {options.signup_enabled && options.phone_signup_enabled && (
        <MethodTabs
          method={chosen}
          onMethod={(next) => {
            onMethod(next);
            setError(null);
          }}
        />
      )}
      <form className="space-y-4" onSubmit={handleSubmit}>
        {error && <AccountNotice error>{error}</AccountNotice>}
        {chosen === 'email' ? (
          <EmailCredentialFields
            email={email}
            password={password}
            confirm={confirm}
            onEmail={setEmail}
            onPassword={setPassword}
            onConfirm={setConfirm}
          />
        ) : (
          <PhoneCodeFields
            purpose="signup"
            phone={phone}
            code={code}
            onPhone={setPhone}
            onCode={setCode}
            onError={setError}
            defaultCountryCode={options.phone.default_country_code}
            siteKey={options.turnstile_site_key}
            captchaToken={details.turnstile_token ?? null}
            onCaptchaUsed={resetCaptcha}
          />
        )}
        <ProfileFields
          userName={userName}
          useCase={useCase}
          onUserName={setUserName}
          onUseCase={setUseCase}
        />
        <TermsConsent options={options} accepted={accepted} onAccepted={setAccepted} />
        {needsCaptcha && (
          <TurnstileWidget
            siteKey={options.turnstile_site_key}
            onToken={onToken}
            resetKey={captchaReset}
          />
        )}
        {!ready && <p className="text-xs text-muted">{t('signup.agreeFirst')}</p>}
        <button
          className="ui-button ui-button-primary w-full"
          disabled={submitDisabled}
          aria-busy={busy}
        >
          {busy ? t('account.saving') : t('account.signup')}
        </button>
      </form>
    </>
  );
}

export function SignupPage() {
  const t = useT();
  const options = useAuthOptions();
  const [method, setMethod] = useState<SignupMethod>('email');
  const [done, setDone] = useState<SignupMethod | null>(null);
  const [phoneRequiresApproval, setPhoneRequiresApproval] = useState(false);

  if (!options)
    return (
      <AccountFrame title={t('account.signup')}>
        <p role="status">{t('common.loading')}</p>
      </AccountFrame>
    );
  if (!options.signup_enabled && !options.phone_signup_enabled) return <SignupClosed />;
  const chosen = chooseMethod(method, options.signup_enabled, options.phone_signup_enabled);
  const titleKey =
    done === 'email' && options.require_email_verification
      ? 'account.checkEmail'
      : 'account.signup';

  return (
    <AccountFrame
      title={t(titleKey)}
      description={done ? undefined : t('signup.description')}
      steps={
        <SignupSteps method={chosen} done={done} verifyEmail={options.require_email_verification} />
      }
    >
      {done ? (
        <SignupCompleted
          done={done}
          verifyEmail={options.require_email_verification}
          phoneRequiresApproval={phoneRequiresApproval}
        />
      ) : (
        <ActiveSignupForm
          options={options}
          chosen={chosen}
          onMethod={setMethod}
          onDone={(completedMethod, requiresApproval = false) => {
            setPhoneRequiresApproval(requiresApproval);
            setDone(completedMethod);
          }}
        />
      )}
      <p className="text-sm">
        <Link className="text-accent underline" href="/login">
          {t('signup.haveAccount')}
        </Link>
      </p>
    </AccountFrame>
  );
}

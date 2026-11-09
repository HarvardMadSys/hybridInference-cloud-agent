'use client';
// Sign-up as HybridInference offers it: agree to the Terms, say who you are and
// how you will use Cloud Agent, then create the account with an email and a
// password, or with a phone number and a code. The use case goes with the
// sign-up as the Agent application; signing up never grants Agent use. The
// Terms open in a dialog over the form, so reading them loses nothing typed,
// and nothing is sent until they are agreed to.
import Link from 'next/link';
import { useCallback, useMemo, useRef, useState, type FormEvent } from 'react';
import { useLocale, useT } from '@/components/providers/LocaleProvider';
import {
  signupAccount,
  signupPhone,
  type AuthOptions,
  type SignupDetails,
} from '@/lib/api/accounts';
import { getErrorMessage } from '@/lib/utils/errors';
import { AccountField, AccountNotice, AccountSteps, BackToLogin } from './AccountFrame';
import { AuthFormFrame } from './AuthFormFrame';
import { NewPasswordFields } from './NewPasswordFields';
import { PhoneCodeFields } from './PhoneCodeFields';
import { TermsConsent, useTermsConsent } from './TermsConsent';
import { shownTerms } from './termsContent';
import { TurnstileWidget } from './TurnstileWidget';
import { validNewPassword } from './passwordPolicy';
import { useAuthOptionsState } from './useAuthOptions';

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
    <div role="tablist" aria-label={t('signup.method')} className="auth-method-tabs">
      {(['email', 'phone'] as const).map((value) => (
        <button
          key={value}
          type="button"
          role="tab"
          aria-selected={method === value}
          className="ui-button auth-method-tab"
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
    <AuthFormFrame title={t('account.unavailable')}>
      <p className="text-sm text-muted">{t('account.registrationClosed')}</p>
      <BackToLogin />
    </AuthFormFrame>
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

function SignupUnavailable() {
  const t = useT();
  return (
    <AuthFormFrame title={t('account.signup')}>
      <AccountNotice error>{t('signup.optionsFailed')}</AccountNotice>
      <button
        type="button"
        className="ui-button ui-button-secondary"
        onClick={() => window.location.reload()}
      >
        {t('common.retry')}
      </button>
      <BackToLogin />
    </AuthFormFrame>
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
  const { locale, t } = useLocale();
  const terms = useMemo(() => shownTerms(options, locale), [options, locale]);
  const consent = useTermsConsent(terms);
  const accepted = consent.agreed;
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

  /** Why this form cannot be sent yet. The button waits too; this holds for any submit. */
  function submitProblem(): string | null {
    if (!consent.agreed) return t('signup.termsRequired');
    if (chosen === 'email' && needsCaptcha && !captcha) return t('signup.captchaRequired');
    if (chosen === 'phone' && code.length !== 6) return t('signup.codeRequired');
    return null;
  }

  const problem = submitProblem();

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    if (problem) {
      setError(problem);
      if (!consent.agreed) consent.show();
      return;
    }
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
      <form className="space-y-5" onSubmit={handleSubmit}>
        {error && <AccountNotice error>{error}</AccountNotice>}
        <TermsConsent consent={consent} />
        <fieldset className="min-w-0 space-y-3">
          <legend className="text-sm font-semibold">{t('signup.credentials')}</legend>
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
        </fieldset>
        <fieldset className="min-w-0 space-y-3">
          <legend className="text-sm font-semibold">{t('signup.application')}</legend>
          <ProfileFields
            userName={userName}
            useCase={useCase}
            onUserName={setUserName}
            onUseCase={setUseCase}
          />
        </fieldset>
        {needsCaptcha && (
          <TurnstileWidget
            siteKey={options.turnstile_site_key}
            onToken={onToken}
            resetKey={captchaReset}
          />
        )}
        {!error && problem && <p className="text-xs text-muted">{problem}</p>}
        <button
          className="ui-button ui-button-primary w-full"
          disabled={busy || Boolean(problem)}
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
  const { options, loading } = useAuthOptionsState();
  const [method, setMethod] = useState<SignupMethod>('email');
  const [done, setDone] = useState<SignupMethod | null>(null);
  const [phoneRequiresApproval, setPhoneRequiresApproval] = useState(false);

  if (!options && !loading) return <SignupUnavailable />;
  if (!options)
    return (
      <AuthFormFrame title={t('account.signup')}>
        <p role="status">{t('common.loading')}</p>
      </AuthFormFrame>
    );
  if (!options.signup_enabled && !options.phone_signup_enabled) return <SignupClosed />;
  const chosen = chooseMethod(method, options.signup_enabled, options.phone_signup_enabled);
  const titleKey =
    done === 'email' && options.require_email_verification
      ? 'account.checkEmail'
      : 'account.signup';

  return (
    <AuthFormFrame
      title={t(titleKey)}
      description={done ? undefined : t('signup.description')}
      longForm={!done}
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
      <p className="border-t border-border pt-5 text-center text-sm text-muted">
        {t('signup.haveAccount')}{' '}
        <Link
          className="ui-focus-ring rounded-sm font-medium text-accent hover:underline"
          href="/login"
        >
          {t('account.login')}
        </Link>
      </p>
    </AuthFormFrame>
  );
}

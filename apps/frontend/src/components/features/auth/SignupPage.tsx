'use client';
// Sign-up as HybridInference offers it: agree to the Terms, say who you are and
// how you will use Cloud Agent, then create the account with an email and a
// password, or with a phone number and a code. The use case goes with the
// sign-up as the Agent application; signing up never grants Agent use.
import Link from 'next/link';
import { useCallback, useState, type FormEvent } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { signupPhone, type SignupDetails } from '@/lib/api/accounts';
import { getErrorMessage } from '@/lib/utils/errors';
import { AccountField, AccountFrame, AccountNotice, BackToLogin } from './AccountFrame';
import { PhoneCodeFields } from './PhoneCodeFields';
import { SignupEmailForm } from './SignupForm';
import { TermsConsent } from './TermsConsent';
import { TurnstileWidget } from './TurnstileWidget';
import { useAuthOptions } from './useAuthOptions';

function PhoneSignup({
  details,
  countryCode,
  siteKey,
  onCaptchaUsed,
}: {
  details: SignupDetails;
  countryCode: string;
  siteKey?: string | null;
  onCaptchaUsed: () => void;
}) {
  const t = useT();
  const [phone, setPhone] = useState('');
  const [code, setCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ requires_approval: boolean } | null>(null);
  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      setResult(
        await signupPhone({
          phone: phone.trim(),
          code,
          user_name: details.user_name,
          use_case: details.use_case,
          accepted_tos: details.accepted_tos,
        }),
      );
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  if (result)
    return (
      <AccountNotice>
        {result.requires_approval ? t('signup.phoneWaiting') : t('signup.createdVerified')}
      </AccountNotice>
    );
  return (
    <form className="space-y-4" onSubmit={submit}>
      {error && <AccountNotice error>{error}</AccountNotice>}
      <PhoneCodeFields
        purpose="signup"
        phone={phone}
        code={code}
        onPhone={setPhone}
        onCode={setCode}
        onError={setError}
        defaultCountryCode={countryCode}
        siteKey={siteKey}
        captchaToken={details.turnstile_token ?? null}
        onCaptchaUsed={onCaptchaUsed}
      />
      <button className="ui-button ui-button-primary w-full" disabled={busy || code.length !== 6}>
        {busy ? t('account.saving') : t('account.signup')}
      </button>
    </form>
  );
}

function MethodTabs({
  method,
  onMethod,
}: {
  method: 'email' | 'phone';
  onMethod: (value: 'email' | 'phone') => void;
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

function chooseMethod(
  method: 'email' | 'phone',
  emailOpen: boolean,
  phoneOpen: boolean,
): 'email' | 'phone' {
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

export function SignupPage() {
  const t = useT();
  const options = useAuthOptions();
  const [accepted, setAccepted] = useState(false);
  const [userName, setUserName] = useState('');
  const [useCase, setUseCase] = useState('');
  const [captcha, setCaptcha] = useState<string | null>(null);
  const [method, setMethod] = useState<'email' | 'phone'>('email');
  const [sent, setSent] = useState(false);
  const [captchaReset, setCaptchaReset] = useState(0);
  const onToken = useCallback((token: string | null) => setCaptcha(token), []);
  // A token is good for one request: after each, a fresh challenge replaces it.
  const resetCaptcha = useCallback(() => {
    setCaptcha(null);
    setCaptchaReset((value) => value + 1);
  }, []);
  if (!options)
    return (
      <AccountFrame title={t('account.signup')}>
        <p role="status">{t('common.loading')}</p>
      </AccountFrame>
    );
  const emailOpen = options.signup_enabled;
  const phoneOpen = options.phone_signup_enabled;
  if (!emailOpen && !phoneOpen) return <SignupClosed />;
  const chosen = chooseMethod(method, emailOpen, phoneOpen);
  const needsCaptcha = Boolean(options.turnstile_site_key);
  const details = signupDetails(userName, useCase, accepted, captcha);
  const ready = accepted && (!needsCaptcha || Boolean(captcha));
  return (
    <AccountFrame
      title={t(
        sent && options.require_email_verification ? 'account.checkEmail' : 'account.signup',
      )}
      description={t('signup.description')}
    >
      <div className="space-y-4">
        <AccountField id="user-name" label={t('signup.name')}>
          <input
            id="user-name"
            className="ui-input w-full"
            autoComplete="name"
            minLength={2}
            maxLength={50}
            value={userName}
            onChange={(e) => setUserName(e.target.value)}
          />
        </AccountField>
        <AccountField id="use-case" label={t('waitlist.purpose')}>
          <textarea
            id="use-case"
            className="ui-input w-full"
            rows={3}
            maxLength={2000}
            value={useCase}
            onChange={(e) => setUseCase(e.target.value)}
          />
        </AccountField>
        <TermsConsent options={options} accepted={accepted} onAccepted={setAccepted} />
        {needsCaptcha && (
          <TurnstileWidget
            siteKey={options.turnstile_site_key}
            onToken={onToken}
            resetKey={captchaReset}
          />
        )}
        {emailOpen && phoneOpen && <MethodTabs method={chosen} onMethod={setMethod} />}
      </div>
      {!ready && <p className="text-xs text-muted">{t('signup.agreeFirst')}</p>}
      {chosen === 'email' ? (
        <SignupEmailForm
          details={details}
          verified={!options.require_email_verification}
          ready={ready}
          onDone={() => setSent(true)}
          onAttempt={needsCaptcha ? resetCaptcha : undefined}
        />
      ) : (
        // The consent is what the form waits for; the code button waits for a token.
        accepted && (
          <PhoneSignup
            details={details}
            countryCode={options.phone.default_country_code}
            siteKey={options.turnstile_site_key}
            onCaptchaUsed={resetCaptcha}
          />
        )
      )}
      <p className="text-sm">
        <Link className="text-accent underline" href="/login">
          {t('signup.haveAccount')}
        </Link>
      </p>
    </AccountFrame>
  );
}

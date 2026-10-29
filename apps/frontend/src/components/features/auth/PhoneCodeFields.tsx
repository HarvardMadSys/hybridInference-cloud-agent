'use client';
// A number and the six-digit code sent to it, adapted from HybridInference
// components/auth/PhoneAuthForm.tsx and OwnerPhoneCode.tsx. Asking for a code
// answers the same for every number; the button waits out the cooldown the
// API returns. Where the deployment configured Turnstile, every code is asked
// for with a fresh token: this form renders its own challenge, unless the page
// shares one (sign-up), and either way the spent token is replaced after each
// request.
import { useCallback, useEffect, useState } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { sendPhoneCode, type PhonePurpose } from '@/lib/api/accounts';
import { getErrorMessage } from '@/lib/utils/errors';
import { AccountField } from './AccountFrame';
import { TurnstileWidget } from './TurnstileWidget';

export function PhoneCodeFields({
  purpose,
  phone,
  code,
  onPhone,
  onCode,
  onError,
  defaultCountryCode,
  siteKey,
  captchaToken,
  onCaptchaUsed,
  fixedPhone,
}: {
  purpose: PhonePurpose;
  phone: string;
  code: string;
  onPhone: (value: string) => void;
  onCode: (value: string) => void;
  onError: (message: string | null) => void;
  defaultCountryCode?: string;
  /** The deployment's Turnstile site key: a code is then sent only with a token. */
  siteKey?: string | null;
  /** A token from a challenge the page shows itself; this form then shows none. */
  captchaToken?: string | null;
  /** Called after each request with the page's token, which is spent. */
  onCaptchaUsed?: () => void;
  /** The account's own verified number: shown, not typed. */
  fixedPhone?: string;
}) {
  const t = useT();
  const [wait, setWait] = useState(0);
  const [sending, setSending] = useState(false);
  const [sent, setSent] = useState(false);
  const shared = captchaToken !== undefined;
  const [ownToken, setOwnToken] = useState<string | null>(null);
  const [resetKey, setResetKey] = useState(0);
  const onToken = useCallback((token: string | null) => setOwnToken(token), []);
  const token = shared ? captchaToken : ownToken;
  const needsToken = Boolean(siteKey);
  useEffect(() => {
    if (wait <= 0) return;
    const timer = window.setTimeout(() => setWait((value) => value - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [wait]);
  async function send() {
    setSending(true);
    onError(null);
    try {
      const answer = await sendPhoneCode(fixedPhone ?? phone, purpose, token ?? undefined);
      setSent(true);
      setWait(answer.retry_after);
    } catch (cause) {
      onError(getErrorMessage(cause));
    } finally {
      setSending(false);
      // The token went with this request, answered or not: never send it again.
      if (needsToken) {
        if (shared) onCaptchaUsed?.();
        else setResetKey((value) => value + 1);
      }
    }
  }
  return (
    <>
      {fixedPhone ? (
        <p className="text-sm text-muted">{t('phone.codeTo', { phone: fixedPhone })}</p>
      ) : (
        <AccountField id={`phone-${purpose}`} label={t('phone.number')}>
          <input
            id={`phone-${purpose}`}
            className="ui-input w-full"
            type="tel"
            autoComplete="tel"
            inputMode="tel"
            placeholder={defaultCountryCode ? `+${defaultCountryCode} …` : '+1 202 555 0123'}
            maxLength={32}
            required
            value={phone}
            onChange={(e) => onPhone(e.target.value)}
          />
        </AccountField>
      )}
      {needsToken && !shared && siteKey && (
        <TurnstileWidget siteKey={siteKey} onToken={onToken} resetKey={resetKey} />
      )}
      <div className="flex items-end gap-2">
        <div className="min-w-0 flex-1">
          <AccountField id={`code-${purpose}`} label={t('phone.code')}>
            <input
              id={`code-${purpose}`}
              className="ui-input w-full"
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="[0-9]{6}"
              maxLength={6}
              required
              value={code}
              onChange={(e) => onCode(e.target.value.replace(/\D/g, ''))}
            />
          </AccountField>
        </div>
        <button
          type="button"
          className="ui-button ui-button-secondary shrink-0"
          disabled={sending || wait > 0 || !(fixedPhone ?? phone).trim() || (needsToken && !token)}
          onClick={() => void send()}
        >
          {wait > 0
            ? t('phone.resendIn', { seconds: wait })
            : sent
              ? t('phone.resend')
              : t('phone.send')}
        </button>
      </div>
      {sent && <p className="text-xs text-muted">{t('phone.sentHint')}</p>}
    </>
  );
}

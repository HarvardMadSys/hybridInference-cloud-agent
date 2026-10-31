'use client';
// The Terms of Service a new account agrees to, as HybridInference's sign-up
// consent step asks. Agreeing is required whatever the deployment published:
// a link to its terms, its own text, or neither.
import Link from 'next/link';
import { useT } from '@/components/providers/LocaleProvider';
import type { AuthOptions } from '@/lib/api/accounts';

export function TermsConsent({
  options,
  accepted,
  onAccepted,
}: {
  options: AuthOptions | null;
  accepted: boolean;
  onAccepted: (value: boolean) => void;
}) {
  const t = useT();
  const link = options?.terms_url ? (
    <a className="text-accent underline" href={options.terms_url} target="_blank" rel="noreferrer">
      {t('terms.title')}
    </a>
  ) : options?.terms_text ? (
    <Link className="text-accent underline" href="/terms" target="_blank">
      {t('terms.title')}
    </Link>
  ) : (
    <span>{t('terms.title')}</span>
  );
  return (
    <label className="flex items-start gap-2 text-sm">
      <input
        type="checkbox"
        className="mt-1"
        checked={accepted}
        onChange={(e) => onAccepted(e.target.checked)}
        required
      />
      <span>
        {t('terms.agree')} {link}
      </span>
    </label>
  );
}

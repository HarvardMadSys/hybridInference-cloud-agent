'use client';
// The deployment's Terms of Service, as HybridInference's /terms page shows
// its own: the operator's text, or a link to where they published it.
import { useT } from '@/components/providers/LocaleProvider';
import { AccountFrame, BackToLogin } from './AccountFrame';
import { useAuthOptions } from './useAuthOptions';

export function TermsPage() {
  const t = useT();
  const options = useAuthOptions();
  const paragraphs = (options?.terms_text ?? '').split(/\n\s*\n/).filter((part) => part.trim());
  return (
    <AccountFrame title={t('terms.title')}>
      {!options ? (
        <p role="status">{t('common.loading')}</p>
      ) : options.terms_url ? (
        <p className="text-sm">
          <a className="text-accent underline" href={options.terms_url} rel="noreferrer">
            {t('terms.readAt')}
          </a>
        </p>
      ) : paragraphs.length ? (
        <div className="space-y-3 text-sm leading-6">
          {paragraphs.map((part, index) => (
            <p key={index} className="whitespace-pre-wrap">
              {part}
            </p>
          ))}
        </div>
      ) : (
        <p className="text-sm text-muted">{t('terms.none')}</p>
      )}
      <BackToLogin />
    </AccountFrame>
  );
}

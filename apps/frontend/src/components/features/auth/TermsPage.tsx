'use client';
// The deployment's Terms of Service, as HybridInference's /terms page shows
// its own: the operator's text, or a link to where they published it.
import { useLocale } from '@/components/providers/LocaleProvider';
import { localizedText } from '@/config/site';
import { AccountFrame, BackToLogin } from './AccountFrame';
import { useAuthOptions } from './useAuthOptions';

export function TermsPage() {
  const { locale, t } = useLocale();
  const options = useAuthOptions();
  // The deployment's Terms files come in the reader's language when it wrote one.
  const text = options?.terms_by_locale
    ? localizedText(options.terms_by_locale, locale)
    : options?.terms_text;
  const paragraphs = (text || options?.terms_text || '')
    .split(/\n\s*\n/)
    .filter((part) => part.trim());
  return (
    <AccountFrame title={t('terms.title')} layout="content">
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

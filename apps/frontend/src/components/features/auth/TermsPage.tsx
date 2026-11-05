'use client';
// The deployment's Terms of Service, as HybridInference's /terms page shows
// its own: the operator's text in a reading column, or a link to where they
// published it. Laid out like the Team page around it.
import { useMemo, type ReactNode } from 'react';
import { PublicPageFrame } from '@/components/PublicPageFrame';
import { useLocale } from '@/components/providers/LocaleProvider';
import { AccountNotice, BackToLogin } from './AccountFrame';
import { TermsBody, TermsDates, termsLang, type TextTerms } from './TermsDocument';
import { parseTerms, shownTerms, type ShownTerms } from './termsContent';
import { useAuthOptionsState } from './useAuthOptions';

function Article({ header, children }: { header?: ReactNode; children: ReactNode }) {
  const { t } = useLocale();
  return (
    <PublicPageFrame className="bg-surface-muted/60">
      <main className="mx-auto w-full max-w-[1200px] flex-1 px-3 pb-16 pt-6 sm:px-7 sm:pb-20 sm:pt-12">
        <article
          aria-labelledby="terms-heading"
          className="mx-auto max-w-[46rem] rounded-2xl border border-border/50 bg-surface px-5 py-8 shadow-subtle sm:rounded-3xl sm:px-12 sm:py-12"
        >
          <header className="border-b border-border/70 pb-6 sm:pb-7">
            <p className="text-xs font-semibold uppercase tracking-[0.18em] text-accent">
              {t('terms.eyebrow')}
            </p>
            <h2
              id="terms-heading"
              className="mt-3 break-words font-serif text-[32px] font-semibold leading-[1.15] tracking-[-0.03em] text-foreground sm:text-[40px]"
            >
              {t('terms.title')}
            </h2>
            {header}
          </header>
          <div className="pt-7 sm:pt-8">{children}</div>
          <footer className="mt-10 border-t border-border/70 pt-6">
            <BackToLogin />
          </footer>
        </article>
      </main>
    </PublicPageFrame>
  );
}

function TextArticle({ terms }: { terms: TextTerms }) {
  const model = useMemo(() => parseTerms(terms.text), [terms.text]);
  return (
    <Article header={<TermsDates model={model} lang={termsLang(terms)} />}>
      <TermsBody terms={terms} model={model} />
    </Article>
  );
}

function OtherArticle({ terms }: { terms: Exclude<ShownTerms, TextTerms> }) {
  const { t } = useLocale();
  if (terms.kind === 'none')
    return (
      <Article>
        <p className="text-[15px] leading-7 text-muted">{t('terms.none')}</p>
      </Article>
    );
  return (
    <Article>
      <div className="space-y-4 text-[15px] leading-7">
        <p>{t('terms.externalIntro')}</p>
        <a className="ui-button ui-button-secondary inline-flex" href={terms.url} rel="noreferrer">
          {t('terms.externalOpen')}
          <span aria-hidden="true">↗</span>
        </a>
      </div>
    </Article>
  );
}

export function TermsPage() {
  const { locale, t } = useLocale();
  const { options, loading } = useAuthOptionsState();
  const terms = useMemo(() => (options ? shownTerms(options, locale) : null), [options, locale]);
  if (!terms)
    return (
      <Article>
        {loading ? (
          <p role="status" className="text-sm text-muted">
            {t('common.loading')}
          </p>
        ) : (
          <div className="space-y-3">
            <AccountNotice error>{t('terms.loadFailed')}</AccountNotice>
            <button
              type="button"
              className="ui-button ui-button-secondary"
              onClick={() => window.location.reload()}
            >
              {t('common.retry')}
            </button>
          </div>
        )}
      </Article>
    );
  return terms.kind === 'text' ? <TextArticle terms={terms} /> : <OtherArticle terms={terms} />;
}

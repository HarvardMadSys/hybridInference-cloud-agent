'use client';
// The Terms text as both the /terms page and the sign-up dialog show it: the
// date under the title, then the numbered sections with their numbers hung
// in the margin. Rendered as text only, so whatever the deployment wrote,
// markup included, reads as written.
import { useLocale } from '@/components/providers/LocaleProvider';
import { LOCALE_HTML_LANG, LOCALE_LABELS, isLocale } from '@/lib/i18n/locales';
import type { ShownTerms, TermsDocumentModel, TermsSection } from './termsContent';

export type TextTerms = Extract<ShownTerms, { kind: 'text' }>;

const PARAGRAPH = 'whitespace-pre-wrap break-words [overflow-wrap:anywhere]';
const HANGING = 'relative pl-8 sm:pl-10';
const MARKER = 'absolute left-0 top-0 font-semibold tabular-nums text-accent';

/** The `lang` of a deployment Terms file, when its locale is one the UI knows. */
export function termsLang(terms: TextTerms): string | undefined {
  return terms.locale && isLocale(terms.locale) ? LOCALE_HTML_LANG[terms.locale] : undefined;
}

/** The "Last updated" lines, for under a title. */
export function TermsDates({ model, lang }: { model: TermsDocumentModel; lang?: string }) {
  if (!model.dates.length) return null;
  return (
    <div lang={lang} className="mt-2 space-y-0.5 text-[13px] leading-5 text-muted sm:text-sm">
      {model.dates.map((date, index) => (
        <p key={index} className={PARAGRAPH}>
          {date}
        </p>
      ))}
    </div>
  );
}

function Section({ section }: { section: TermsSection }) {
  const [first, ...rest] = section.paragraphs;
  const body = section.title ? section.paragraphs : rest;
  return (
    <section>
      {section.title ? (
        <h3
          className={`${HANGING} text-[15.5px] font-semibold leading-7 tracking-[-0.01em] text-foreground sm:text-[17px]`}
        >
          <span className={MARKER}>{section.marker}</span>
          <span className={PARAGRAPH}>{section.title}</span>
        </h3>
      ) : (
        <p className={`${HANGING} ${PARAGRAPH}`}>
          <span className={MARKER}>{section.marker}</span>
          {first}
        </p>
      )}
      {body.length > 0 && (
        <div className={`mt-1.5 space-y-3 ${HANGING}`}>
          {body.map((paragraph, index) => (
            <p key={index} className={PARAGRAPH}>
              {paragraph}
            </p>
          ))}
        </div>
      )}
    </section>
  );
}

/**
 * Everything after the dates: a note when the reader's language was not
 * published, the paragraphs and sections, and any copy published elsewhere.
 */
export function TermsBody({ terms, model }: { terms: TextTerms; model: TermsDocumentModel }) {
  const { locale, t } = useLocale();
  const language =
    terms.locale && terms.locale !== locale
      ? isLocale(terms.locale)
        ? LOCALE_LABELS[terms.locale]
        : terms.locale
      : '';
  return (
    <div className="space-y-6 text-[15px] leading-7 text-foreground/[0.86] sm:space-y-7 sm:text-base sm:leading-[1.8]">
      {language && (
        <p className="rounded-lg border border-border bg-surface-muted/70 px-3 py-2 text-[13px] leading-5 text-muted">
          {t('terms.shownIn', { language })}
        </p>
      )}
      <div lang={termsLang(terms)} className="space-y-6 sm:space-y-7">
        {model.preamble.map((paragraph, index) => (
          <p key={index} className={PARAGRAPH}>
            {paragraph}
          </p>
        ))}
        {model.sections.map((section, index) => (
          <Section key={index} section={section} />
        ))}
      </div>
      {terms.url && (
        <p className="text-sm">
          <a
            className="ui-focus-ring rounded text-accent underline underline-offset-4"
            href={terms.url}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t('terms.alsoPublished')}
            <span aria-hidden="true"> ↗</span>
          </a>
        </p>
      )}
    </div>
  );
}

'use client';
// The Terms of Service a new account agrees to, read in a dialog over the
// sign-up form so nothing typed is lost. Agreeing is required whatever the
// deployment published: its own text, a link to its terms, or neither.
//
// Only the Agree button consents. Closing the dialog leaves the answer as it
// was, Disagree withdraws it, and an answer belongs to the Terms it was given
// for: when they change, sign-up asks again. Text shown here can be agreed to
// once its end has been on screen; a link elsewhere cannot be measured, and
// the dialog says so rather than pretending.
import { useCallback, useEffect, useId, useMemo, useRef, useState, type RefObject } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers/LocaleProvider';
import { useModalDialog } from '@/components/ui/useModalDialog';
import { TermsBody, TermsDates, termsLang, type TextTerms } from './TermsDocument';
import { parseTerms, type ShownTerms } from './termsContent';

export type TermsConsentStatus = 'pending' | 'agreed' | 'declined' | 'changed';

export interface TermsConsentState {
  terms: ShownTerms;
  agreed: boolean;
  status: TermsConsentStatus;
  open: boolean;
  /** Whether Agree is available: the text's end was on screen, or there is no text to read. */
  reached: boolean;
  opener: RefObject<HTMLButtonElement>;
  show: () => void;
  close: () => void;
  markReached: () => void;
  agree: () => void;
  disagree: () => void;
}

/**
 * The reader's answer to the Terms shown. The dialog opens by itself once for
 * each version of Terms there is something to read for.
 */
export function useTermsConsent(terms: ShownTerms): TermsConsentState {
  const [decision, setDecision] = useState<{ key: string; agreed: boolean } | null>(null);
  const [readKey, setReadKey] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const opener = useRef<HTMLButtonElement>(null);
  const autoOpened = useRef<string | null>(null);
  const { key, kind } = terms;
  useEffect(() => {
    if (kind === 'none' || autoOpened.current === key) return;
    autoOpened.current = key;
    setOpen(true);
  }, [key, kind]);
  const agreed = decision?.key === key && decision.agreed;
  const reached = kind !== 'text' || readKey === key;
  let status: TermsConsentStatus = 'pending';
  if (agreed) status = 'agreed';
  else if (decision?.key === key) status = 'declined';
  else if (decision?.agreed) status = 'changed';
  const markReached = useCallback(() => setReadKey(key), [key]);
  return {
    terms,
    agreed,
    status,
    open,
    reached,
    opener,
    show: () => setOpen(true),
    close: () => setOpen(false),
    markReached,
    agree: () => {
      if (!reached) return;
      setDecision({ key, agreed: true });
      setOpen(false);
    },
    disagree: () => {
      setDecision({ key, agreed: false });
      setOpen(false);
    },
  };
}

/**
 * Calls `onReached` once the end of the text is on screen: scrolled to, or
 * there from the start because everything fits. The sentinel is observed
 * against the viewport, so whichever box scrolls (the text, or the whole
 * dialog on a short or zoomed screen) clips it. Without IntersectionObserver
 * the text box's own scroll position decides.
 */
function useEndReached(
  box: RefObject<HTMLElement>,
  end: RefObject<HTMLElement>,
  active: boolean,
  onReached: () => void,
) {
  useEffect(() => {
    const scroller = box.current;
    const sentinel = end.current;
    if (!active || !scroller || !sentinel) return;
    if (typeof IntersectionObserver === 'function') {
      const observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => entry.isIntersecting)) onReached();
      });
      observer.observe(sentinel);
      return () => observer.disconnect();
    }
    const check = () => {
      if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2) onReached();
    };
    check();
    scroller.addEventListener('scroll', check, { passive: true });
    return () => scroller.removeEventListener('scroll', check);
  }, [box, end, active, onReached]);
}

function ExternalTerms({ url }: { url: string }) {
  const t = useT();
  return (
    <div className="space-y-4 text-[15px] leading-7">
      <p>{t('terms.externalIntro')}</p>
      <a
        className="ui-button ui-button-secondary inline-flex"
        href={url}
        target="_blank"
        rel="noopener noreferrer"
      >
        {t('terms.externalOpen')}
        <span aria-hidden="true">↗</span>
      </a>
    </div>
  );
}

function DialogText({ terms }: { terms: TextTerms }) {
  const model = useMemo(() => parseTerms(terms.text), [terms.text]);
  return (
    <>
      <TermsDates model={model} lang={termsLang(terms)} />
      <div className="mt-5">
        <TermsBody terms={terms} model={model} />
      </div>
    </>
  );
}

function hintFor(consent: TermsConsentState, t: ReturnType<typeof useT>): string {
  if (consent.terms.kind === 'url') return t('terms.externalNote');
  if (consent.terms.kind === 'none') return t('terms.noneConsent');
  return consent.reached ? t('terms.reachedEnd') : t('terms.scrollHint');
}

function TermsDialog({ consent, scroll }: { consent: TermsConsentState; scroll: { top: number } }) {
  const t = useT();
  const id = useId();
  const panel = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const end = useRef<HTMLDivElement>(null);
  const { terms } = consent;
  useModalDialog(panel, {
    onClose: consent.close,
    initialFocus: '[data-terms-text]',
    returnFocus: consent.opener,
  });
  useEndReached(box, end, terms.kind === 'text' && !consent.reached, consent.markReached);
  // Reopening returns to where the reader stopped.
  useEffect(() => {
    if (box.current) box.current.scrollTop = scroll.top;
  }, [scroll]);
  if (typeof document === 'undefined') return null;
  return createPortal(
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-2 sm:p-6">
      <button
        type="button"
        tabIndex={-1}
        aria-label={t('terms.close')}
        onClick={consent.close}
        className="absolute inset-0 cursor-default bg-gray-900/30 backdrop-blur-[2px] dark:bg-black/60"
      />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
        aria-describedby={`${id}-hint`}
        className="relative flex max-h-full w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-border bg-surface text-foreground shadow-card [@media(max-height:32rem)]:overflow-y-auto"
      >
        <div className="flex items-start justify-between gap-4 border-b border-border px-5 pb-4 pt-5 sm:px-8 sm:pt-6">
          <h2
            id={`${id}-title`}
            className="font-serif text-2xl font-semibold leading-tight tracking-[-0.02em] sm:text-[28px]"
          >
            {t('terms.title')}
          </h2>
          <button
            type="button"
            onClick={consent.close}
            aria-label={t('terms.close')}
            className="ui-focus-ring -mr-2 -mt-1 shrink-0 rounded-md px-2 py-1 text-xl leading-none text-muted hover:bg-surface-hover hover:text-foreground"
          >
            ×
          </button>
        </div>
        <div
          ref={box}
          data-terms-text=""
          tabIndex={0}
          role="region"
          aria-label={t('terms.textLabel')}
          onScroll={(event) => {
            scroll.top = event.currentTarget.scrollTop;
          }}
          className="ui-focus-ring min-h-0 flex-1 overflow-y-auto overscroll-contain px-5 py-5 focus-visible:-outline-offset-2 focus-visible:outline-focus/50 sm:px-8 sm:py-6 [@media(max-height:32rem)]:flex-none [@media(max-height:32rem)]:overflow-visible"
        >
          {terms.kind === 'text' ? (
            <DialogText terms={terms} />
          ) : terms.kind === 'url' ? (
            <ExternalTerms url={terms.url} />
          ) : (
            <p className="text-[15px] leading-7">{t('terms.none')}</p>
          )}
          <div ref={end} aria-hidden="true" className="h-px" />
        </div>
        <div className="flex flex-col gap-3 border-t border-border bg-surface-muted/50 px-5 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-8">
          <p id={`${id}-hint`} aria-live="polite" className="text-[13px] leading-5 text-muted">
            {hintFor(consent, t)}
          </p>
          <div className="grid grid-cols-2 gap-2 sm:flex sm:shrink-0">
            <button
              type="button"
              className="ui-button ui-button-secondary"
              onClick={consent.disagree}
            >
              {t('terms.disagreeButton')}
            </button>
            <button
              type="button"
              className="ui-button ui-button-primary"
              disabled={!consent.reached}
              onClick={consent.agree}
            >
              {t('terms.agreeButton')}
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

const STATUS_KEYS = {
  pending: 'terms.statusPending',
  agreed: 'terms.statusAgreed',
  declined: 'terms.statusDeclined',
  changed: 'terms.statusChanged',
} as const;

function StatusIcon({ agreed }: { agreed: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={`mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full ${agreed ? 'bg-status-success/15 text-status-success' : 'bg-accent/10 text-accent'}`}
    >
      <svg
        viewBox="0 0 16 16"
        className="h-4 w-4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
      >
        {agreed ? (
          <path d="M3.5 8.5l3 3 6-7" strokeLinecap="round" strokeLinejoin="round" />
        ) : (
          <path d="M4.5 2.5h5l2.5 2.5v8.5h-7.5zM6.5 7.5h3.5M6.5 10h3.5" strokeLinejoin="round" />
        )}
      </svg>
    </span>
  );
}

/** Where the form shows the answer, with the way back into the dialog. */
export function TermsConsent({ consent }: { consent: TermsConsentState }) {
  const t = useT();
  // One scroll position per version of the Terms, kept while the dialog is closed.
  const { key } = consent.terms;
  const scroll = useMemo(() => ({ top: 0, key }), [key]);
  return (
    <div
      className={`flex flex-wrap items-center gap-3 rounded-xl border p-3.5 ${consent.agreed ? 'border-status-success/40 bg-status-success/[0.06]' : 'border-border bg-surface-muted/60'}`}
    >
      <div className="flex min-w-[12rem] flex-1 items-start gap-3">
        <StatusIcon agreed={consent.agreed} />
        <div className="min-w-0">
          <p className="text-sm font-medium text-foreground">{t('terms.title')}</p>
          <p aria-live="polite" className="mt-0.5 text-[13px] leading-5 text-muted">
            {t(STATUS_KEYS[consent.status])}
          </p>
        </div>
      </div>
      <button
        ref={consent.opener}
        type="button"
        className="ui-button ui-button-secondary shrink-0"
        onClick={consent.show}
      >
        {consent.agreed ? t('terms.readAgain') : t('terms.read')}
      </button>
      {consent.open && <TermsDialog consent={consent} scroll={scroll} />}
    </div>
  );
}

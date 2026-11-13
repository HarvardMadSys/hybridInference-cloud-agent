'use client';
import type { ReactNode } from 'react';
import Link from 'next/link';
import { useT } from '@/components/providers/LocaleProvider';
import { PublicPageFrame } from '@/components/PublicPageFrame';

/**
 * The public pages share the app's mark and footer. Account forms use a narrow,
 * centered column; informational pages use a wider column starting at the top.
 */
export function AccountFrame({
  title,
  description,
  steps,
  layout = 'form',
  children,
}: {
  title: string;
  description?: string;
  /** Where this page sits in a longer flow, shown above the title. */
  steps?: ReactNode;
  layout?: 'form' | 'content';
  children: ReactNode;
}) {
  return (
    <PublicPageFrame>
      <main
        className={`mx-auto w-full flex-1 px-[18px] pb-16 ${
          layout === 'content'
            ? 'max-w-[880px] pt-10 sm:px-7 sm:pt-14'
            : 'flex max-w-[420px] flex-col justify-center pt-6'
        }`}
      >
        <section
          aria-labelledby="account-heading"
          className={layout === 'form' ? 'account-form' : undefined}
        >
          {steps}
          <h2
            id="account-heading"
            className="text-[28px] font-semibold leading-tight tracking-[-0.03em] text-foreground"
          >
            {title}
          </h2>
          {description && <p className="mt-2 text-[14.5px] leading-6 text-muted">{description}</p>}
          <div className="mt-7 space-y-5">{children}</div>
        </section>
      </main>
    </PublicPageFrame>
  );
}

/** The steps of a flow, the current one marked. */
export function AccountSteps({ steps, current }: { steps: string[]; current: number }) {
  return (
    <ol className="mb-5 flex flex-wrap items-center gap-2 text-xs text-muted">
      {steps.map((step, index) => (
        <li
          key={step}
          aria-current={index === current ? 'step' : undefined}
          className="inline-flex items-center gap-2 whitespace-nowrap aria-[current=step]:font-medium aria-[current=step]:text-foreground"
        >
          {index > 0 && <span aria-hidden="true" className="h-px w-6 bg-border" />}
          <span
            aria-hidden="true"
            className={`h-2 w-2 rounded-full border-[1.5px] ${index === current ? 'border-accent bg-accent' : index < current ? 'border-status-success bg-status-success' : 'border-border-strong'}`}
          />
          {step}
        </li>
      ))}
    </ol>
  );
}
export function AccountNotice({ error, children }: { error?: boolean; children: ReactNode }) {
  return (
    <p
      role={error ? 'alert' : 'status'}
      className={`rounded-lg border p-3 text-sm leading-6 ${error ? 'border-red-300 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950 dark:text-red-200' : 'border-border bg-surface-muted text-muted'}`}
    >
      {children}
    </p>
  );
}
export function BackToLogin() {
  const t = useT();
  return (
    <Link className="ui-focus-ring text-sm text-accent underline" href="/login">
      {t('account.backLogin')}
    </Link>
  );
}
export function AccountField({
  id,
  label,
  hint,
  action,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <label className="block text-sm font-medium" htmlFor={id}>
          {label}
        </label>
        {action}
      </div>
      {children}
      {hint && (
        <p className="text-xs leading-5 text-muted" id={`${id}-hint`}>
          {hint}
        </p>
      )}
    </div>
  );
}

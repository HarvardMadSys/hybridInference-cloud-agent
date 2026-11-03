'use client';
import type { ReactNode } from 'react';
import Link from 'next/link';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/LocaleProvider';
import { BrandMark } from '@/components/BrandMark';
import { DeploymentFooter } from '@/components/DeploymentFooter';

/**
 * The signed-out account pages: the app's mark (a way back to the home page),
 * then one form in a narrow column, in the home page's type. No card, no
 * motion: the form is the whole page.
 */
export function AccountFrame({
  title,
  description,
  steps,
  children,
}: {
  title: string;
  description?: string;
  /** Where this page sits in a longer flow, shown above the title. */
  steps?: ReactNode;
  children: ReactNode;
}) {
  const { branding } = useSiteConfig();
  return (
    <div className="flex min-h-screen flex-col bg-surface">
      <header className="mx-auto flex h-16 w-full max-w-[1200px] items-center px-[18px] sm:px-7">
        <Link
          href="/"
          className="ui-focus-ring inline-flex min-w-0 items-center gap-2.5 rounded-md text-sm font-semibold tracking-tight text-foreground"
        >
          <BrandMark />
          {/* The deployment's name is the page's h1, as the runtime site check
              requires; the form's title is the section heading below it. */}
          <h1 className="truncate">{branding.appName}</h1>
        </Link>
      </header>
      <main className="mx-auto flex w-full max-w-[420px] flex-1 flex-col justify-center px-[18px] pb-16 pt-6">
        <section aria-labelledby="account-heading" className="account-form">
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
      <DeploymentFooter />
    </div>
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
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <label className="block text-sm font-medium" htmlFor={id}>
        {label}
      </label>
      {children}
      {hint && (
        <p className="text-xs leading-5 text-muted" id={`${id}-hint`}>
          {hint}
        </p>
      )}
    </div>
  );
}

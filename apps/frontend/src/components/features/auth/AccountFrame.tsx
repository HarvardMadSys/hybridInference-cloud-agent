'use client';
import type { ReactNode } from 'react';
import Link from 'next/link';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/LocaleProvider';
import { DeploymentFooter } from '@/components/DeploymentFooter';

export function AccountFrame({
  title,
  description,
  children,
}: {
  title: string;
  description?: string;
  children: ReactNode;
}) {
  const { branding } = useSiteConfig();
  return (
    <div className="flex min-h-screen flex-col bg-surface-muted">
      <main className="mx-auto flex w-full max-w-md flex-1 flex-col justify-center px-4 py-10">
        <section
          aria-labelledby="account-heading"
          className="rounded-xl border border-border bg-surface p-6 shadow-sm"
        >
          <h1 className="text-sm font-semibold text-muted">{branding.appName}</h1>
          <h2 id="account-heading" className="mt-3 text-2xl font-semibold tracking-tight">
            {title}
          </h2>
          {description && <p className="mt-2 text-sm leading-6 text-muted">{description}</p>}
          <div className="mt-6 space-y-4">{children}</div>
        </section>
      </main>
      <DeploymentFooter />
    </div>
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

'use client';

import { useT } from '@/components/providers';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { BookIcon } from './icons';

export function SettingsPage({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  const t = useT();
  const { docsUrl } = useSiteConfig().branding;
  return (
    <section className="mx-auto w-full max-w-5xl px-5 py-6 sm:px-8 sm:py-8 lg:px-12 lg:py-10">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-border pb-5">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-foreground">{title}</h1>
          <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">{description}</p>
        </div>
        {docsUrl ? (
          <a
            href={docsUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="ui-focus-ring inline-flex min-h-8 items-center gap-1.5 rounded-md text-sm text-muted hover:text-foreground"
          >
            <BookIcon />
            {t('nav.docs')}
          </a>
        ) : null}
      </header>
      {children}
    </section>
  );
}

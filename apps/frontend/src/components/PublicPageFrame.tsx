'use client';

import type { ReactNode } from 'react';
import Link from 'next/link';
import { BrandMark } from '@/components/BrandMark';
import { DeploymentFooter } from '@/components/DeploymentFooter';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';

/** The shared brand and footer around public pages, including account forms. */
export function PublicPageFrame({
  children,
  className = 'bg-surface',
}: {
  children: ReactNode;
  className?: string;
}) {
  const { branding } = useSiteConfig();
  return (
    <div className={`flex min-h-screen flex-col ${className}`}>
      <header className="mx-auto flex h-16 w-full max-w-[1200px] items-center px-[18px] sm:px-7">
        <Link
          href="/"
          className="ui-focus-ring inline-flex min-w-0 items-center gap-2.5 rounded-md text-sm font-semibold tracking-tight text-foreground"
        >
          <BrandMark />
          {/* Runtime site checks require the configured brand as the page's h1. */}
          <h1 className="truncate">{branding.appName}</h1>
        </Link>
      </header>
      {children}
      <DeploymentFooter />
    </div>
  );
}

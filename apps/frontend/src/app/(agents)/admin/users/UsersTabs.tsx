'use client';

// Admin → Users holds two views of the same people: one row per account, and
// one row per team. Each is its own address, so a link opens either directly.
// A view's own tools (People's sign-up policy and mail) sit on the same row.

import Link from 'next/link';
import type { ReactNode } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import type { TranslationKey } from '@/lib/i18n/translate';

const VIEWS: { key: 'people' | 'teams'; label: TranslationKey; href: string }[] = [
  { key: 'people', label: 'adminAccounts.view.people', href: '/admin/users' },
  { key: 'teams', label: 'adminAccounts.view.teams', href: '/admin/users/teams' },
];

/** A quiet link to a view's own tool, for the row beside the views. */
export const USERS_TOOL =
  'ui-focus-ring inline-flex min-h-8 items-center rounded-md border border-border bg-surface px-2.5 py-1 text-[13px] font-medium text-muted transition-colors hover:bg-surface-hover hover:text-foreground';

export function UsersTabs({
  current,
  children,
}: {
  current: (typeof VIEWS)[number]['key'];
  /** The view's own tools, shown at the end of the row. */
  children?: ReactNode;
}) {
  const t = useT();
  return (
    <div className="mt-4 flex flex-wrap items-center justify-between gap-2">
      <nav
        aria-label={t('adminAccounts.views')}
        className="inline-flex gap-1 rounded-lg border border-border bg-surface-muted p-0.5"
      >
        {VIEWS.map((view) => (
          <Link
            key={view.key}
            href={view.href}
            aria-current={view.key === current ? 'page' : undefined}
            className={`ui-focus-ring inline-flex min-h-8 items-center rounded-md px-3 py-1 text-[13px] font-medium transition-colors ${
              view.key === current
                ? 'bg-surface text-foreground shadow-subtle'
                : 'text-muted hover:bg-surface-hover hover:text-foreground'
            }`}
          >
            {t(view.label)}
          </Link>
        ))}
      </nav>
      {children}
    </div>
  );
}

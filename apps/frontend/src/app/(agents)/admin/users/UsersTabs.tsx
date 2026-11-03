'use client';

// Admin → Users holds two views of the same people: one row per account, and
// one row per team. Each is its own address, so a link opens either directly.

import Link from 'next/link';

const VIEWS = [
  { key: 'people', label: 'People', href: '/admin/users' },
  { key: 'teams', label: 'Teams', href: '/admin/users/teams' },
] as const;

export function UsersTabs({ current }: { current: (typeof VIEWS)[number]['key'] }) {
  return (
    <nav
      aria-label="Users views"
      className="mt-5 inline-flex gap-1 rounded-xl border border-border bg-surface-muted p-1"
    >
      {VIEWS.map((view) => (
        <Link
          key={view.key}
          href={view.href}
          aria-current={view.key === current ? 'page' : undefined}
          className={`ui-focus-ring inline-flex min-h-9 items-center rounded-lg px-3 py-1.5 text-sm font-medium transition-colors ${
            view.key === current
              ? 'bg-surface text-foreground shadow-subtle'
              : 'text-muted hover:bg-surface-hover hover:text-foreground'
          }`}
        >
          {view.label}
        </Link>
      ))}
    </nav>
  );
}

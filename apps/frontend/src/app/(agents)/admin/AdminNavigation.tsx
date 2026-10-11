'use client';

import Link from 'next/link';
import { useEffect, useRef } from 'react';
import { AdminSetupNotice } from './AdminSetupNotice';

export function AdminNavigation({
  current,
}: {
  current:
    | 'servers'
    | 'sandboxes'
    | 'runs'
    | 'activity'
    | 'usage'
    | 'measurements'
    | 'settings'
    | 'database'
    | 'audit';
}) {
  const section = current === 'runs' ? 'activity' : current;
  const selected = useRef<HTMLAnchorElement>(null);
  const navigation = useRef<HTMLElement>(null);
  useEffect(() => {
    const reveal = () => {
      const element = navigation.current;
      const link = selected.current;
      if (!element || !link) return;
      const strip = element.getBoundingClientRect();
      const item = link.getBoundingClientRect();
      const inset = 8;
      // Move only this strip so a resize never resets the workspace's vertical
      // scroll position while an administrator is reading further down a page.
      if (item.left < strip.left + inset) {
        element.scrollLeft = Math.max(0, element.scrollLeft + item.left - strip.left - inset);
      } else if (item.right > strip.right - inset) {
        element.scrollLeft += item.right - strip.right + inset;
      }
    };
    reveal();
    const element = navigation.current;
    if (!element || typeof ResizeObserver === 'undefined') return;
    let width = element.clientWidth;
    // Keep the active section in view when the workspace narrows. Reading the
    // width explicitly avoids repositioning the strip after a user scrolls it.
    const observer = new ResizeObserver(() => {
      if (element.clientWidth === width) return;
      width = element.clientWidth;
      reveal();
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, [section]);
  return (
    <>
      {current !== 'settings' && <AdminSetupNotice />}
      <nav
        ref={navigation}
        aria-label="Admin sections"
        className="mt-6 flex gap-1 overflow-x-auto overflow-y-hidden rounded-xl border border-border bg-surface p-1.5 shadow-subtle"
      >
        {[
          { key: 'servers', label: 'Servers', href: '/admin/hosts' },
          { key: 'sandboxes', label: 'Sandboxes', href: '/admin/sandboxes' },
          { key: 'activity', label: 'Activity', href: '/admin/activity' },
          { key: 'usage', label: 'Usage', href: '/admin/usage' },
          { key: 'measurements', label: 'Measurements', href: '/admin/measurements' },
          { key: 'settings', label: 'Settings', href: '/admin/settings' },
          { key: 'database', label: 'Database', href: '/admin/database' },
          { key: 'audit', label: 'Audit log', href: '/admin/audit' },
        ].map((item) => (
          <Link
            key={item.key}
            href={item.href}
            ref={section === item.key ? selected : undefined}
            aria-current={section === item.key ? 'page' : undefined}
            className={`ui-focus-ring inline-flex min-h-11 shrink-0 items-center justify-center rounded-lg px-4 py-2 text-sm font-medium transition-colors focus-visible:-outline-offset-2 ${
              section === item.key
                ? 'bg-accent-subtle text-accent'
                : 'text-muted hover:bg-surface-hover hover:text-foreground'
            }`}
          >
            {item.label}
          </Link>
        ))}
      </nav>
    </>
  );
}

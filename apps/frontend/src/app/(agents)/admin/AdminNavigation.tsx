'use client';

import Link from 'next/link';
import { useEffect, useRef } from 'react';
import { AdminSetupNotice } from './AdminSetupNotice';

const SECTIONS = [
  { key: 'servers', label: 'Servers', href: '/admin/hosts' },
  { key: 'sandboxes', label: 'Sandboxes', href: '/admin/sandboxes' },
  { key: 'activity', label: 'Activity', href: '/admin/activity' },
  { key: 'usage', label: 'Usage', href: '/admin/usage' },
  { key: 'teams', label: 'Teams', href: '/admin/teams' },
  { key: 'measurements', label: 'Measurements', href: '/admin/measurements' },
  { key: 'settings', label: 'Settings', href: '/admin/settings' },
  { key: 'database', label: 'Database', href: '/admin/database' },
  { key: 'audit', label: 'Audit log', href: '/admin/audit' },
] as const;

export function AdminNavigation({
  current,
}: {
  current:
    | 'servers'
    | 'sandboxes'
    | 'runs'
    | 'activity'
    | 'usage'
    | 'teams'
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
        className="mt-3 flex gap-1 overflow-x-auto overflow-y-hidden border-b border-border"
      >
        {SECTIONS.map((item) => {
          const active = section === item.key;
          return (
            <Link
              key={item.key}
              href={item.href}
              ref={active ? selected : undefined}
              aria-current={active ? 'page' : undefined}
              className={`ui-focus-ring -mb-px inline-flex min-h-8 shrink-0 items-center border-b-2 px-2.5 py-1.5 text-[13px] font-medium transition-colors focus-visible:-outline-offset-2 [@media(pointer:coarse)]:min-h-11 ${
                active
                  ? 'border-accent text-foreground'
                  : 'border-transparent text-muted hover:text-foreground'
              }`}
            >
              {item.label}
            </Link>
          );
        })}
      </nav>
    </>
  );
}

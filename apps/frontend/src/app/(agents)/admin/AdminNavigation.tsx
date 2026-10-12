'use client';

import Link from 'next/link';
import { useEffect, useRef } from 'react';
import { AdminSetupNotice } from './AdminSetupNotice';

const SECTIONS = [
  { key: 'servers', label: 'Servers', href: '/admin/hosts' },
  { key: 'sandboxes', label: 'Sandboxes', href: '/admin/sandboxes' },
  { key: 'activity', label: 'Activity', href: '/admin/activity' },
  { key: 'usage', label: 'Usage', href: '/admin/usage' },
  { key: 'measurements', label: 'Measurements', href: '/admin/measurements' },
  { key: 'settings', label: 'Settings', href: '/admin/settings' },
  { key: 'database', label: 'Database', href: '/admin/database' },
  { key: 'audit', label: 'Audit log', href: '/admin/audit' },
] as const;

function SectionIcon({ id }: { id: (typeof SECTIONS)[number]['key'] }) {
  const common = {
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth: 1.75,
    'aria-hidden': true as const,
    className: 'h-4 w-4 shrink-0',
  };
  if (id === 'servers') {
    return (
      <svg {...common}>
        <rect x="3.5" y="3.5" width="17" height="6.5" rx="1.5" />
        <rect x="3.5" y="14" width="17" height="6.5" rx="1.5" />
        <path d="M7 6.75h.01M7 17.25h.01" strokeLinecap="round" />
      </svg>
    );
  }
  if (id === 'sandboxes') {
    return (
      <svg {...common}>
        <path d="M12 3.5 20 8v8l-8 4.5L4 16V8l8-4.5Z" strokeLinejoin="round" />
        <path d="M12 12.5 20 8M12 12.5v8M12 12.5 4 8" strokeLinejoin="round" />
      </svg>
    );
  }
  if (id === 'activity') {
    return (
      <svg {...common}>
        <path d="M3 12h4l2.2-5.5L13 18l2.2-6H21" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (id === 'usage') {
    return (
      <svg {...common}>
        <path d="M5 19V10M12 19V5M19 19v-7" strokeLinecap="round" />
      </svg>
    );
  }
  if (id === 'measurements') {
    return (
      <svg {...common}>
        <circle cx="12" cy="12" r="8" />
        <path d="M12 8v4.5l2.5 1.5" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
    );
  }
  if (id === 'settings') {
    return (
      <svg {...common}>
        <path d="M4 7h16M4 12h16M4 17h16" strokeLinecap="round" />
        <circle cx="8" cy="7" r="1.6" fill="currentColor" stroke="none" />
        <circle cx="15" cy="12" r="1.6" fill="currentColor" stroke="none" />
        <circle cx="10" cy="17" r="1.6" fill="currentColor" stroke="none" />
      </svg>
    );
  }
  if (id === 'database') {
    return (
      <svg {...common}>
        <ellipse cx="12" cy="6.5" rx="7" ry="2.75" />
        <path d="M5 6.5v11c0 1.5 3.1 2.75 7 2.75s7-1.25 7-2.75v-11" />
        <path d="M5 12c0 1.5 3.1 2.75 7 2.75s7-1.25 7-2.75" />
      </svg>
    );
  }
  return (
    <svg {...common}>
      <rect x="6" y="4.5" width="12" height="15" rx="2" />
      <path d="M9 4.5h6V7H9z" strokeLinejoin="round" />
      <path d="M9 11.5h6M9 15h4" strokeLinecap="round" />
    </svg>
  );
}

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
        className="mt-4 flex gap-1 overflow-x-auto overflow-y-hidden rounded-2xl border border-border bg-surface p-1.5 shadow-subtle"
      >
        {SECTIONS.map((item) => {
          const active = section === item.key;
          return (
            <Link
              key={item.key}
              href={item.href}
              ref={active ? selected : undefined}
              aria-current={active ? 'page' : undefined}
              className={`ui-focus-ring inline-flex min-h-11 shrink-0 items-center justify-center gap-2 rounded-xl px-3.5 py-2 text-sm font-medium transition-colors focus-visible:-outline-offset-2 ${
                active
                  ? 'bg-accent-subtle text-accent shadow-sm ring-1 ring-inset ring-accent/20'
                  : 'text-muted hover:bg-surface-hover hover:text-foreground'
              }`}
            >
              <SectionIcon id={item.key} />
              {item.label}
            </Link>
          );
        })}
      </nav>
    </>
  );
}

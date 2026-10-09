'use client';

import Link from 'next/link';
import { useEffect, useRef } from 'react';

export function AdminNavigation({
  current,
}: {
  current: 'servers' | 'runs' | 'activity' | 'usage' | 'measurements' | 'settings';
}) {
  const section = current === 'runs' ? 'activity' : current;
  const selected = useRef<HTMLAnchorElement>(null);
  useEffect(() => {
    selected.current?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
  }, [section]);
  return (
    <nav
      aria-label="Admin sections"
      className="mt-6 flex gap-1 overflow-x-auto overflow-y-hidden border-b border-gray-200 dark:border-white/10"
    >
      {[
        { key: 'servers', label: 'Servers', href: '/admin/hosts' },
        { key: 'activity', label: 'Activity', href: '/admin/activity' },
        { key: 'usage', label: 'Usage', href: '/admin/usage' },
        { key: 'measurements', label: 'Measurements', href: '/admin/measurements' },
        { key: 'settings', label: 'Settings', href: '/admin/settings' },
      ].map((item) => (
        <Link
          key={item.key}
          href={item.href}
          ref={section === item.key ? selected : undefined}
          aria-current={section === item.key ? 'page' : undefined}
          className={`-mb-px shrink-0 border-b-2 px-4 py-3 text-sm font-medium transition-colors ${
            section === item.key
              ? 'border-crimson text-crimson dark:border-crimson-light dark:text-crimson-light'
              : 'border-transparent text-gray-500 hover:border-gray-300 hover:text-gray-900 dark:text-gray-400 dark:hover:border-white/20 dark:hover:text-gray-100'
          }`}
        >
          {item.label}
        </Link>
      ))}
    </nav>
  );
}

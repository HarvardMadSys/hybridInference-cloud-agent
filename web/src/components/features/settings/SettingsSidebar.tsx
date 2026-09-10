'use client';

import Link from 'next/link';
import { useRef, useState } from 'react';
import { useAuth, useT } from '@/components/providers';
import { SettingsDialog } from './SettingsDialog';
import { SunIcon } from './icons';

const NAV =
  'flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-[13px] text-gray-600 transition-colors hover:bg-gray-200/60 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/30 dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-gray-100';

/** Settings navigation used by the full archive management page. */
export function SettingsSidebar({ collapsed = false }: { collapsed?: boolean }) {
  const t = useT();
  const { state } = useAuth();
  const [section, setSection] = useState<'appearance' | 'language' | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const name = state.user?.user_name || state.user?.email || '';

  return (
    <>
      {!collapsed ? (
        <aside
          id="agents-sidebar"
          aria-label={t('settings.title')}
          className="hidden w-56 shrink-0 flex-col border-r border-gray-200/70 bg-gray-100/75 px-3 py-5 dark:border-white/10 dark:bg-gray-900 md:flex"
        >
          <Link href="/" className={NAV}>
            <svg
              aria-hidden="true"
              className="h-4 w-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={1.8}
            >
              <path d="m10 6-6 6 6 6M4 12h16" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            {t('settings.backToWorkspace')}
          </Link>
          <nav className="mt-8" aria-label={t('settings.title')}>
            <p className="mb-2 px-3 text-[11px] font-medium text-gray-400 dark:text-gray-500">
              {t('settings.preferences')}
            </p>
            <button
              type="button"
              className={NAV}
              onClick={(event) => {
                trigger.current = event.currentTarget;
                setSection('appearance');
              }}
            >
              <SunIcon />
              {t('settings.appearance.title')}
            </button>
            <button
              type="button"
              className={NAV}
              onClick={(event) => {
                trigger.current = event.currentTarget;
                setSection('language');
              }}
            >
              <svg
                aria-hidden="true"
                className="h-4 w-4"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1.8}
              >
                <circle cx="12" cy="12" r="9" />
                <path d="M3 12h18M12 3a17 17 0 0 0 0 18 17 17 0 0 0 0-18Z" />
              </svg>
              {t('settings.language.title')}
            </button>
            <p className="mb-2 mt-7 px-3 text-[11px] font-medium text-gray-400 dark:text-gray-500">
              {t('settings.conversations')}
            </p>
            <Link
              href="/archived"
              aria-current="page"
              className="flex items-center gap-2.5 rounded-lg bg-white px-3 py-2 text-[13px] font-medium text-gray-900 shadow-sm ring-1 ring-gray-200/60 dark:bg-white/10 dark:text-gray-100 dark:ring-white/5"
            >
              <svg
                aria-hidden="true"
                className="h-4 w-4"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1.8}
              >
                <rect x="3" y="4" width="18" height="5" rx="1.5" />
                <path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9M10 13h4" strokeLinecap="round" />
              </svg>
              {t('settings.archived')}
            </Link>
          </nav>
          {name ? (
            <div className="mt-auto flex items-center gap-2.5 px-3 pt-8 text-xs text-gray-500 dark:text-gray-400">
              <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-gray-200 text-[10px] font-semibold text-gray-600 dark:bg-white/10 dark:text-gray-300">
                {name.slice(0, 2).toUpperCase()}
              </span>
              <span className="truncate">{name}</span>
            </div>
          ) : null}
        </aside>
      ) : null}
      {section ? (
        <SettingsDialog
          initialSection={section}
          onClose={() => {
            setSection(null);
            trigger.current?.focus();
          }}
        />
      ) : null}
    </>
  );
}

'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useT } from '@/components/providers';
import { AccountMenu } from '@/components/features/agents/AccountMenu';
import { PaneResizer } from '@/components/features/agents/PaneResizer';
import type { ResizablePane } from '@/components/features/agents/useResizablePane';
import { HelpIcon } from './icons';
import { SETTINGS_PATHS } from './navigation';

const ITEMS = [
  {
    href: SETTINGS_PATHS.profile,
    label: 'settings.title',
    icon: 'M4 7h9m4 0h3M4 17h3m4 0h9M13 4v6M7 14v6',
  },
  {
    href: SETTINGS_PATHS.memory,
    label: 'settings.memory.title',
    icon: 'M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18ZM12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18ZM15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4',
  },
  {
    href: SETTINGS_PATHS.shortcuts,
    label: 'settings.shortcuts.title',
    icon: 'M4 4h16a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2ZM6 8h.01M10 8h.01M14 8h.01M18 8h.01M8 12h.01M12 12h.01M16 12h.01M7 16h10',
  },
  {
    href: SETTINGS_PATHS.usage,
    label: 'settings.usage.title',
    icon: 'M4 20V11m6 9V5m6 15v-7m4 7H3',
  },
  {
    href: SETTINGS_PATHS.plugins,
    label: 'settings.plugins',
    icon: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM17.5 14v7M14 17.5h7',
  },
  {
    href: SETTINGS_PATHS.integrations,
    label: 'settings.integrations',
    icon: 'M10 13a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-2 2M14 11a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l2-2',
  },
  {
    href: SETTINGS_PATHS.ssh,
    label: 'settings.ssh',
    icon: 'M5 4h14a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2ZM5 13h14a2 2 0 0 1 2 2v3a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2ZM7 7.5h.01M7 16.5h.01',
  },
  {
    href: SETTINGS_PATHS.environments,
    label: 'settings.environments',
    icon: 'm12 3 9 5-9 5-9-5 9-5ZM3 8v9l9 5 9-5V8M12 13v9',
  },
  {
    href: SETTINGS_PATHS.teams,
    label: 'teams.title',
    icon: 'M16 19v-1a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v1M9 10a3 3 0 1 0 0-6 3 3 0 0 0 0 6M22 19v-1a4 4 0 0 0-3-3.87M16 4.13a3 3 0 0 1 0 5.74',
  },
] as const;

export function SettingsSidebar({
  collapsed,
  headerAction,
  pane,
  backHref,
}: {
  collapsed: boolean;
  headerAction?: React.ReactNode;
  pane: ResizablePane;
  backHref: string;
}) {
  const t = useT();
  const pathname = usePathname();
  if (collapsed) return null;
  const itemClass = (current: boolean) =>
    `ui-focus-ring flex min-h-10 items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors [@media(pointer:coarse)]:min-h-11 ${current ? 'bg-surface-hover font-medium text-foreground' : 'text-muted hover:bg-surface-hover hover:text-foreground'}`;
  return (
    <>
      <aside
        id="agents-sidebar"
        aria-label={t('settings.navigation')}
        style={{ width: pane.width }}
        className="flex min-h-0 shrink-0 flex-col border-r border-border/70 bg-surface-muted"
      >
        <div className="flex items-center gap-1 px-3 pb-3 pt-4">
          <Link
            href={backHref}
            className="ui-focus-ring flex min-h-9 min-w-0 flex-1 items-center gap-2 rounded-lg px-2 text-sm text-muted hover:bg-surface-hover hover:text-foreground"
          >
            <svg
              aria-hidden="true"
              className="h-4 w-4 shrink-0"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.8}
            >
              <path d="m14 6-6 6 6 6" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <span className="truncate">{t('settings.backToChats')}</span>
          </Link>
          {headerAction}
        </div>
        <nav aria-label={t('settings.navigation')} className="min-h-0 flex-1 overflow-y-auto px-3">
          <p className="px-3 pb-3 pt-2 text-xs font-medium text-muted">
            {t('settings.navigation')}
          </p>
          <div className="space-y-1">
            {ITEMS.map(({ href, label, icon }) => (
              <Link
                key={href}
                href={href}
                aria-current={pathname === href ? 'page' : undefined}
                className={`${itemClass(pathname === href)} ${href === SETTINGS_PATHS.plugins ? '!mt-5' : ''}`}
              >
                <svg
                  aria-hidden="true"
                  className="h-[18px] w-[18px] shrink-0"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.6}
                >
                  <path d={icon} strokeLinecap="round" strokeLinejoin="round" />
                </svg>
                {t(label)}
              </Link>
            ))}
          </div>
        </nav>
        <div className="shrink-0 px-3 pb-3">
          <Link
            href={SETTINGS_PATHS.help}
            aria-current={pathname === SETTINGS_PATHS.help ? 'page' : undefined}
            className={itemClass(pathname === SETTINGS_PATHS.help)}
          >
            <HelpIcon className="h-[18px] w-[18px] shrink-0" />
            {t('gettingStarted.title')}
          </Link>
        </div>
        <div className="shrink-0 border-t border-border/70 px-2 py-2">
          <AccountMenu variant="sidebar" />
        </div>
      </aside>
      <PaneResizer pane={pane} label={t('settings.resizeNavigation')} controls="agents-sidebar" />
    </>
  );
}

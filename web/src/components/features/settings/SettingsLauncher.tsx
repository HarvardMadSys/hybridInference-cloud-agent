'use client';

import { useCallback, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { GearIcon, AccountIcon } from './icons';
import { SettingsDialog } from './SettingsDialog';

/**
 * The sidebar's bottom row: who you are, and the way into settings.
 *
 * This row already existed as static text. Making the whole row the control —
 * rather than bolting a gear onto the end of it — matches where every other
 * app of this shape puts settings, and gives the target the full width of the
 * sidebar instead of a 28px icon.
 */
export function SettingsLauncher() {
  const t = useT();
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);

  const displayName = t('settings.account');

  // Closing has to hand focus back to the trigger. Without it focus falls to
  // <body> and a keyboard user restarts from the top of the document.
  const close = useCallback(() => {
    setOpen(false);
    trigger.current?.focus();
  }, []);

  return (
    <>
      <button
        ref={trigger}
        type="button"
        onClick={() => setOpen(true)}
        aria-haspopup="dialog"
        aria-expanded={open}
        title={t('sidebar.openSettings')}
        className="group flex w-full shrink-0 items-center gap-2.5 border-t border-gray-200/80 bg-white/50 px-4 py-3 text-left transition-colors hover:bg-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-crimson/25 dark:border-white/10 dark:bg-white/[0.02] dark:hover:bg-white/[0.06]"
      >
        <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-crimson/[0.08] text-[11px] font-semibold text-crimson ring-1 ring-inset ring-crimson/10 dark:bg-crimson/20 dark:text-crimson-light dark:ring-crimson/25">
          <AccountIcon />
        </span>
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-gray-800 dark:text-gray-200">
          {displayName}
        </span>
        <GearIcon className="h-4 w-4 shrink-0 text-gray-400 transition-colors group-hover:text-gray-600 dark:text-gray-500 dark:group-hover:text-gray-300" />
        <span className="sr-only">{t('sidebar.openSettings')}</span>
      </button>

      {open ? <SettingsDialog onClose={close} /> : null}
    </>
  );
}

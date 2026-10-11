'use client';

import { useCallback, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { GearIcon } from './icons';
import { SettingsDialog } from './SettingsDialog';

/** Opens preferences from the sidebar's bottom row. */
export function SettingsLauncher() {
  const t = useT();
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);

  const close = useCallback(() => {
    setOpen(false);
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
        className="ui-focus-ring group flex min-h-14 w-full shrink-0 items-center gap-3 border-t border-border bg-surface/60 px-4 py-3 text-left transition-colors hover:bg-surface-hover focus-visible:-outline-offset-2"
      >
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent-subtle text-accent">
          <GearIcon />
        </span>
        <span className="min-w-0 flex-1 truncate text-sm font-medium text-foreground">
          {t('settings.title')}
        </span>
        <span className="sr-only">{t('sidebar.openSettings')}</span>
      </button>

      {open ? <SettingsDialog onClose={close} returnFocus={trigger} /> : null}
    </>
  );
}

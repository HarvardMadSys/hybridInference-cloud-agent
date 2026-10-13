'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { useFirstRun } from '@/components/features/agents/firstRun';
import { useT } from '@/components/providers';
import { CheckIcon, HelpIcon } from './icons';

/** Restore the sidebar guide for a newcomer who skipped it. */
export function GettingStartedSettings() {
  const firstRun = useFirstRun();
  const t = useT();
  const [shown, setShown] = useState(false);
  const [helpOpen, setHelpOpen] = useState(false);
  const helpId = useId();
  const shownNote = useRef<HTMLSpanElement>(null);
  const restored = shown && !firstRun.hidden;

  // Keep keyboard focus on the confirmation when Show again is replaced.
  useEffect(() => {
    if (restored) shownNote.current?.focus();
  }, [restored]);

  if (!firstRun.hidden && !shown) return null;

  return (
    <section
      aria-labelledby="settings-getting-started"
      className="ui-card mt-4 bg-surface p-3 sm:p-4"
    >
      <div className="flex flex-wrap items-center gap-1">
        <h2 id="settings-getting-started" className="text-base font-semibold text-foreground">
          {t('gettingStarted.title')}
        </h2>
        <span className="group/help relative inline-flex">
          <button
            type="button"
            onClick={() => setHelpOpen((open) => !open)}
            aria-label={t('settings.gettingStarted.about')}
            aria-describedby={helpId}
            aria-expanded={helpOpen}
            className="ui-icon-button ui-button-ghost h-11 w-11 rounded-full"
          >
            <HelpIcon />
          </button>
          <span
            id={helpId}
            role="tooltip"
            className={`pointer-events-none absolute bottom-full left-1/2 z-10 mb-1.5 w-56 max-w-[calc(100vw-4rem)] -translate-x-1/2 rounded-lg border border-border bg-surface-raised px-3 py-2 text-xs leading-relaxed text-muted shadow-lg transition-opacity group-hover/help:opacity-100 group-focus-within/help:opacity-100 ${
              helpOpen ? 'opacity-100' : 'opacity-0'
            }`}
          >
            {t('settings.gettingStarted.help')}
          </span>
        </span>
        {restored ? (
          <span
            ref={shownNote}
            role="status"
            tabIndex={-1}
            className="ml-auto inline-flex min-h-11 shrink-0 items-center gap-1.5 text-sm text-status-success outline-none"
          >
            <CheckIcon className="h-3.5 w-3.5 shrink-0" />
            {t('settings.gettingStarted.shown')}
          </span>
        ) : (
          <button
            type="button"
            onClick={() => {
              firstRun.show();
              setShown(true);
            }}
            className="ui-button ui-button-secondary ml-auto min-h-11 shrink-0"
          >
            {t('settings.gettingStarted.show')}
          </button>
        )}
      </div>
    </section>
  );
}

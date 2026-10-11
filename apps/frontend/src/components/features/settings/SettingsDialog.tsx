'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useFirstRun } from '@/components/features/agents/firstRun';
import { useLocale, useTheme } from '@/components/providers';
import { LOCALES, LOCALE_LABELS, isLocale } from '@/lib/i18n/locales';
import { THEME_PREFERENCES, type ThemePreference } from '@/lib/theme';
import { CheckIcon, CloseIcon, GearIcon, HelpIcon, MonitorIcon, MoonIcon, SunIcon } from './icons';
import { NotificationSettings } from './NotificationSettings';

const THEME_ICONS: Record<ThemePreference, typeof SunIcon> = {
  light: SunIcon,
  dark: MoonIcon,
  system: MonitorIcon,
};

const THEME_LABEL_KEYS = {
  light: 'settings.appearance.light',
  dark: 'settings.appearance.dark',
  system: 'settings.appearance.system',
} as const;

const SECTION_TITLE = 'text-xs font-semibold uppercase tracking-[0.08em] text-muted';
const FIELD_LABEL = 'text-sm font-medium text-foreground';

/** Elements that can hold focus, for the tab cycle below. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function SettingsDialog({ onClose }: { onClose: () => void }) {
  const { t, locale, setLocale } = useLocale();
  const { preference, setPreference } = useTheme();
  const firstRun = useFirstRun();
  // Brought back from here: the row stays to say so until the dialog closes.
  const [gettingStartedShown, setGettingStartedShown] = useState(false);
  // Open on click too, for a screen with no hover.
  const [helpOpen, setHelpOpen] = useState(false);
  const helpId = useId();

  const panel = useRef<HTMLDivElement>(null);
  const titleId = 'settings-dialog-title';

  const shownNote = useRef<HTMLSpanElement>(null);

  // Focus has to land inside the dialog, or a keyboard user's next Tab
  // continues through the page behind it — which is still rendered, just
  // covered.
  useEffect(() => {
    panel.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
  }, []);

  // For the same reason, Show again hands its place to the note that replaces
  // it: a pressed button that disappears drops focus to <body>, and Escape
  // and Tab would stop reaching the dialog.
  useEffect(() => {
    if (gettingStartedShown) shownNote.current?.focus();
  }, [gettingStartedShown]);

  const onKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLDivElement>) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== 'Tab') return;
      // Cycle within the dialog rather than trapping hard: the list is read
      // fresh on every Tab, so a control that appears later (a longer language
      // list, a section added below) joins the cycle without extra wiring.
      const focusable = [...(panel.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    },
    [onClose],
  );

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-3 sm:p-6"
      onKeyDown={onKeyDown}
      role="presentation"
    >
      {/* The backdrop is a sibling button rather than a click handler on the
          wrapper: a click that starts inside the panel and ends on the overlay
          — a sloppy drag while selecting text — would otherwise close the
          dialog and lose the selection. */}
      <button
        type="button"
        aria-label={t('settings.close')}
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-gray-900/35 backdrop-blur-sm dark:bg-black/60"
      />

      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="ui-card relative flex max-h-[calc(100dvh-1.5rem)] w-full max-w-lg flex-col overflow-hidden rounded-2xl bg-surface shadow-xl sm:max-h-[calc(100dvh-3rem)]"
      >
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-5 py-4 sm:px-6">
          <div className="flex items-center gap-3">
            <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent-subtle text-accent">
              <GearIcon className="h-5 w-5" />
            </span>
            <h2 id={titleId} className="text-lg font-semibold tracking-tight text-foreground">
              {t('settings.title')}
            </h2>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t('settings.close')}
            className="ui-icon-button ui-button-ghost -mr-2 h-11 w-11"
          >
            <CloseIcon />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-6 sm:px-6">
          <section aria-labelledby="settings-appearance">
            <h3 id="settings-appearance" className={SECTION_TITLE}>
              {t('settings.appearance.title')}
            </h3>
            <div className="mt-3">
              <fieldset>
                <legend className={FIELD_LABEL}>{t('settings.appearance.theme')}</legend>
                {/* Native radios, visually hidden behind their labels. A
                    hand-rolled segmented control would have to reimplement
                    arrow-key movement and the grouped announcement; these come
                    with the element. */}
                <div className="mt-3 grid grid-cols-3 gap-2 sm:gap-3">
                  {THEME_PREFERENCES.map((option) => {
                    const Icon = THEME_ICONS[option];
                    const checked = preference === option;
                    return (
                      <label
                        key={option}
                        className={`min-w-0 cursor-pointer rounded-xl border p-2 text-sm font-medium transition-colors focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-focus sm:p-3 ${
                          checked
                            ? 'border-accent bg-accent-subtle text-accent shadow-subtle'
                            : 'border-border bg-surface text-muted hover:border-border-strong hover:bg-surface-muted hover:text-foreground'
                        }`}
                      >
                        <input
                          type="radio"
                          name="theme-preference"
                          value={option}
                          checked={checked}
                          onChange={() => setPreference(option)}
                          className="sr-only"
                        />
                        {/* Light and Dark previews keep their demonstrated palette
                            regardless of the currently selected application theme. */}
                        <div
                          aria-hidden="true"
                          className={`flex h-14 overflow-hidden rounded-md border shadow-subtle sm:h-16 ${
                            option === 'light'
                              ? 'border-gray-200 bg-white dark:border-gray-200 dark:bg-white'
                              : option === 'dark'
                                ? 'border-gray-700 bg-gray-900 dark:border-gray-700 dark:bg-gray-900'
                                : 'border-border bg-surface'
                          }`}
                        >
                          <div
                            className={`w-1/4 border-r p-1.5 ${
                              option === 'light'
                                ? 'border-gray-200 bg-gray-50 dark:border-gray-200 dark:bg-gray-50'
                                : option === 'dark'
                                  ? 'border-gray-700 bg-gray-800 dark:border-gray-700 dark:bg-gray-800'
                                  : 'border-border bg-surface-muted'
                            }`}
                          >
                            <div className="h-1.5 rounded-full bg-crimson/70" />
                            <div className="mt-2 h-1 rounded-full bg-muted/40" />
                            <div className="mt-1 h-1 rounded-full bg-muted/40" />
                          </div>
                          <div className="flex flex-1 flex-col justify-between p-2">
                            <div className="h-1 w-2/3 rounded-full bg-muted/40" />
                            <div className="h-3 rounded border border-muted/25 bg-muted/10" />
                          </div>
                        </div>
                        <span className="mt-2 flex min-h-8 items-center justify-center gap-1.5">
                          <Icon className="h-4 w-4 shrink-0" />
                          <span className="break-words">{t(THEME_LABEL_KEYS[option])}</span>
                        </span>
                      </label>
                    );
                  })}
                </div>
              </fieldset>
            </div>
          </section>

          <section aria-labelledby="settings-language" className="mt-6 border-t border-border pt-6">
            <h3 id="settings-language" className={SECTION_TITLE}>
              {t('settings.language.title')}
            </h3>
            <div className="mt-3">
              <label className="block">
                <span className={FIELD_LABEL}>{t('settings.language.label')}</span>
                <select
                  value={locale}
                  onChange={(event) => {
                    if (isLocale(event.target.value)) setLocale(event.target.value);
                  }}
                  className="ui-input mt-2 min-h-11 w-full cursor-pointer font-medium transition-colors hover:border-accent"
                >
                  {LOCALES.map((option) => (
                    <option key={option} value={option}>
                      {LOCALE_LABELS[option]}
                    </option>
                  ))}
                </select>
              </label>
            </div>
          </section>

          <NotificationSettings />

          {/* Only for a newcomer who skipped the sidebar's getting-started
              row: the one way back to it. Nobody else has anything here. */}
          {firstRun.hidden || gettingStartedShown ? (
            <section
              aria-labelledby="settings-getting-started"
              className="mt-6 border-t border-border pt-5"
            >
              <div className="flex flex-wrap items-center gap-1">
                <h3 id="settings-getting-started" className={FIELD_LABEL}>
                  {t('gettingStarted.title')}
                </h3>
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
                  {/* Above the row, which sits at the bottom of a scrolling
                      panel: below it, the panel would cut it off. */}
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
                {gettingStartedShown ? (
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
                      setGettingStartedShown(true);
                    }}
                    className="ui-button ui-button-secondary ml-auto min-h-11 shrink-0"
                  >
                    {t('settings.gettingStarted.show')}
                  </button>
                )}
              </div>
            </section>
          ) : null}
        </div>

        <div className="flex shrink-0 justify-end border-t border-border bg-surface-muted/50 px-5 py-4 sm:px-6">
          {/* Every control here applies on change, so this closes rather than
              saves. It is named Done, not Save, so it does not promise a
              commit step that does not exist. */}
          <button
            type="button"
            onClick={onClose}
            className="ui-button ui-button-primary min-h-11 min-w-24 shadow-subtle"
          >
            {t('settings.done')}
          </button>
        </div>
      </div>
    </div>
  );
}

'use client';

import { useCallback, useEffect, useRef } from 'react';
import { useLocale, useTheme } from '@/components/providers';
import { LOCALES, LOCALE_LABELS, isLocale } from '@/lib/i18n/locales';
import { THEME_PREFERENCES, type ThemePreference } from '@/lib/theme';
import { CloseIcon, MonitorIcon, MoonIcon, SunIcon } from './icons';

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

const SECTION_TITLE =
  'text-[11px] font-semibold uppercase tracking-[0.08em] text-gray-400 dark:text-gray-500';
const FIELD_LABEL = 'text-[13px] font-medium text-gray-800 dark:text-gray-200';

/** Elements that can hold focus, for the tab cycle below. */
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function SettingsDialog({ onClose }: { onClose: () => void }) {
  const { t, locale, setLocale } = useLocale();
  const { preference, setPreference } = useTheme();

  const panel = useRef<HTMLDivElement>(null);
  const titleId = 'settings-dialog-title';

  // Focus has to land inside the dialog, or a keyboard user's next Tab
  // continues through the page behind it — which is still rendered, just
  // covered.
  useEffect(() => {
    panel.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
  }, []);

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
      className="fixed inset-0 z-[60] flex items-center justify-center p-4"
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
        className="absolute inset-0 cursor-default bg-gray-900/25 backdrop-blur-[2px] dark:bg-black/50"
      />

      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="relative flex max-h-full w-full max-w-md flex-col overflow-hidden rounded-xl border border-gray-200/80 bg-white shadow-card dark:border-white/10 dark:bg-gray-900"
      >
        <div className="flex shrink-0 items-center justify-between border-b border-gray-200/80 px-5 py-3.5 dark:border-white/10">
          <h2
            id={titleId}
            className="text-[15px] font-semibold tracking-[-0.01em] text-gray-900 dark:text-gray-100"
          >
            {t('settings.title')}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={t('settings.close')}
            className="-mr-1.5 rounded-md p-1.5 text-gray-400 transition-colors hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/25 dark:text-gray-500 dark:hover:bg-white/5 dark:hover:text-gray-200"
          >
            <CloseIcon />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
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
                <div className="mt-2 grid grid-cols-3 gap-1.5 rounded-lg bg-gray-100/80 p-1 dark:bg-white/5">
                  {THEME_PREFERENCES.map((option) => {
                    const Icon = THEME_ICONS[option];
                    const checked = preference === option;
                    return (
                      <label
                        key={option}
                        className={`flex cursor-pointer items-center justify-center gap-1.5 rounded-md px-2 py-1.5 text-[13px] font-medium transition-colors focus-within:ring-2 focus-within:ring-crimson/30 ${
                          checked
                            ? 'bg-white text-gray-900 shadow-subtle dark:bg-gray-700 dark:text-gray-50'
                            : 'text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
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
                        <Icon className="h-3.5 w-3.5 shrink-0" />
                        {t(THEME_LABEL_KEYS[option])}
                      </label>
                    );
                  })}
                </div>
              </fieldset>
            </div>
          </section>

          <section aria-labelledby="settings-language" className="mt-6">
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
                  className="mt-2 w-full cursor-pointer rounded-lg border border-gray-200 bg-white px-3 py-2 text-[13px] font-medium text-gray-800 transition-colors hover:border-gray-300 focus:border-crimson/40 focus:outline-none focus:ring-2 focus:ring-crimson/20 dark:border-white/10 dark:bg-gray-800 dark:text-gray-100 dark:hover:border-white/20"
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
        </div>

        <div className="flex shrink-0 justify-end border-t border-gray-200/80 px-5 py-3 dark:border-white/10">
          {/* Every control here applies on change, so this closes rather than
              saves. It is named Done, not Save, so it does not promise a
              commit step that does not exist. */}
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg bg-crimson px-3.5 py-1.5 text-[13px] font-medium text-white shadow-sm transition-colors hover:bg-crimson-dark focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/30"
          >
            {t('settings.done')}
          </button>
        </div>
      </div>
    </div>
  );
}

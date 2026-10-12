'use client';

import { useLocale } from '@/components/providers/LocaleProvider';
import { useTheme } from '@/components/providers/ThemeProvider';
import { LOCALES, LOCALE_LABELS, isLocale } from '@/lib/i18n/locales';
import { THEME_PREFERENCES, isThemePreference, type ThemePreference } from '@/lib/theme';
import { MonitorIcon, MoonIcon, SunIcon } from './icons';

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

const SELECT =
  'ui-focus-ring min-h-9 cursor-pointer rounded-lg border border-border bg-surface px-2 py-1.5 text-xs font-medium text-foreground transition-colors hover:border-border-strong [@media(pointer:coarse)]:min-h-11';

/** Website preferences stay within reach even when the task list is hidden. */
export function WebsitePreferences({ compact = false }: { compact?: boolean }) {
  const { t, locale, setLocale } = useLocale();
  const { preference, setPreference } = useTheme();
  const ThemeIcon = THEME_ICONS[preference];
  const labelClass = compact
    ? 'relative flex items-center max-sm:h-10 max-sm:w-10 max-sm:rounded-lg max-sm:hover:bg-surface-hover max-sm:focus-within:outline max-sm:focus-within:outline-2 max-sm:focus-within:outline-offset-2 max-sm:focus-within:outline-focus [@media(pointer:coarse)]:max-sm:h-11 [@media(pointer:coarse)]:max-sm:w-11'
    : 'relative flex items-center';
  const compactSelect = compact
    ? 'max-sm:absolute max-sm:inset-0 max-sm:w-full max-sm:opacity-0'
    : '';

  return (
    <div className="flex shrink-0 items-center gap-2">
      <label className={labelClass}>
        <span
          className={`pointer-events-none absolute left-2 text-muted ${compact ? 'max-sm:inset-0 max-sm:flex max-sm:items-center max-sm:justify-center' : ''}`}
          aria-hidden="true"
        >
          <ThemeIcon />
        </span>
        <span className="sr-only">{t('settings.appearance.theme')}</span>
        <select
          value={preference}
          onChange={(event) => {
            if (isThemePreference(event.target.value)) setPreference(event.target.value);
          }}
          title={`${t('settings.appearance.theme')}: ${t(THEME_LABEL_KEYS[preference])}`}
          className={`${SELECT} pl-8 ${compactSelect}`}
        >
          {THEME_PREFERENCES.map((option) => (
            <option key={option} value={option}>
              {t(THEME_LABEL_KEYS[option])}
            </option>
          ))}
        </select>
      </label>
      <label className={labelClass}>
        {compact && (
          <span aria-hidden="true" className="pointer-events-none text-muted sm:hidden">
            <svg
              className="mx-3 h-4 w-4"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={1.7}
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M3 5h12M9 3v2m4 0c-1 6-4 9-9 12m2-9c1 4 4 7 8 9m0 4 5-12 5 12m-8-4h6" />
            </svg>
          </span>
        )}
        <span className="sr-only">{t('settings.language.label')}</span>
        <select
          value={locale}
          onChange={(event) => {
            if (isLocale(event.target.value)) setLocale(event.target.value);
          }}
          title={`${t('settings.language.label')}: ${LOCALE_LABELS[locale]}`}
          className={`${SELECT} ${compactSelect}`}
        >
          {LOCALES.map((option) => (
            <option key={option} value={option}>
              {LOCALE_LABELS[option]}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

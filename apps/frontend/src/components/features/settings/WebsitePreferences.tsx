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
export function WebsitePreferences() {
  const { t, locale, setLocale } = useLocale();
  const { preference, setPreference } = useTheme();
  const ThemeIcon = THEME_ICONS[preference];

  return (
    <div className="flex shrink-0 items-center gap-2">
      <label className="relative flex items-center">
        <span className="pointer-events-none absolute left-2 text-muted">
          <ThemeIcon />
        </span>
        <span className="sr-only">{t('settings.appearance.theme')}</span>
        <select
          value={preference}
          onChange={(event) => {
            if (isThemePreference(event.target.value)) setPreference(event.target.value);
          }}
          title={t('settings.appearance.theme')}
          className={`${SELECT} pl-8`}
        >
          {THEME_PREFERENCES.map((option) => (
            <option key={option} value={option}>
              {t(THEME_LABEL_KEYS[option])}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span className="sr-only">{t('settings.language.label')}</span>
        <select
          value={locale}
          onChange={(event) => {
            if (isLocale(event.target.value)) setLocale(event.target.value);
          }}
          title={t('settings.language.label')}
          className={SELECT}
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

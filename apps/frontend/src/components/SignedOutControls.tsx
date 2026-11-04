'use client';

import { GlobeIcon, MoonIcon, SunIcon } from '@/components/features/settings/icons';
import { useLocale } from '@/components/providers/LocaleProvider';
import { useTheme } from '@/components/providers/ThemeProvider';
import { LOCALE_LABELS, type Locale } from '@/lib/i18n/locales';

const CONTROL =
  'ui-focus-ring inline-flex h-8 items-center gap-1.5 rounded-md px-2 text-xs font-medium text-muted transition-colors hover:bg-surface-hover hover:text-foreground [@media(pointer:coarse)]:min-h-11';

/** Language and theme controls for signed-out headers, where the account menu is absent. */
export function SignedOutControls() {
  const { locale, setLocale, t } = useLocale();
  const { resolved, setPreference } = useTheme();
  const nextLocale: Locale = locale === 'zh-CN' ? 'en' : 'zh-CN';
  const nextLanguageLabel = LOCALE_LABELS[nextLocale];
  const languageTitle = `${t('settings.language.title')}: ${nextLanguageLabel}`;
  const nextTheme = resolved === 'dark' ? 'light' : 'dark';
  const themeTitle = `${t('settings.appearance.title')}: ${t(
    nextTheme === 'dark' ? 'settings.appearance.dark' : 'settings.appearance.light',
  )}`;

  return (
    <div className="flex shrink-0 items-center gap-1">
      <button
        type="button"
        lang={nextLocale}
        aria-label={languageTitle}
        title={languageTitle}
        onClick={() => setLocale(nextLocale)}
        className={CONTROL}
      >
        <GlobeIcon className="h-3.5 w-3.5 shrink-0" />
        <span>{nextLanguageLabel}</span>
      </button>
      <button
        type="button"
        aria-label={themeTitle}
        title={themeTitle}
        onClick={() => setPreference(nextTheme)}
        className={`${CONTROL} w-8 justify-center px-0 [@media(pointer:coarse)]:w-11`}
      >
        <SunIcon className="hidden h-4 w-4 shrink-0 dark:block" />
        <MoonIcon className="block h-4 w-4 shrink-0 dark:hidden" />
      </button>
    </div>
  );
}

import { en, type TranslationKey } from './dictionaries/en';
import { zhCN } from './dictionaries/zh-CN';
import { DEFAULT_LOCALE, type Locale } from './locales';

export type { TranslationKey };

export const DICTIONARIES: Record<Locale, Record<TranslationKey, string>> = {
  en,
  'zh-CN': zhCN,
};

/** Values a placeholder can take. Numbers are formatted by the caller's locale. */
export type TranslationVars = Record<string, string | number>;

const PLACEHOLDER = /\{(\w+)\}/g;

/**
 * Look up `key` in `locale` and substitute `{placeholders}`.
 *
 * A placeholder with no matching variable is left in the output verbatim rather
 * than blanked. `Show {count} more` is a visible bug worth fixing; `Show  more`
 * reads like a design choice and survives to production.
 */
export function translate(locale: Locale, key: TranslationKey, vars?: TranslationVars): string {
  const dictionary = DICTIONARIES[locale] ?? DICTIONARIES[DEFAULT_LOCALE];
  const template = dictionary[key] ?? en[key] ?? key;
  if (!vars) return template;
  return template.replace(PLACEHOLDER, (match, name: string) =>
    name in vars ? String(vars[name]) : match,
  );
}

export type Translator = (key: TranslationKey, vars?: TranslationVars) => string;

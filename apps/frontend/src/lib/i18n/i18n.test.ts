// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { en } from './dictionaries/en';
import {
  DEFAULT_LOCALE,
  LOCALES,
  LOCALE_STORAGE_KEY,
  isLocale,
  negotiateLocale,
  readStoredLocale,
  writeStoredLocale,
} from './locales';
import { DICTIONARIES, translate } from './translate';

function storage(initial: Record<string, string> = {}) {
  const map = new Map(Object.entries(initial));
  return {
    getItem: (key: string) => map.get(key) ?? null,
    setItem: (key: string, value: string) => void map.set(key, value),
    read: (key: string) => map.get(key) ?? null,
  };
}

describe('negotiateLocale', () => {
  it('takes an exact tag', () => {
    expect(negotiateLocale(['zh-CN'])).toBe('zh-CN');
  });

  it('matches on the primary subtag, so zh-TW lands on Chinese', () => {
    expect(negotiateLocale(['zh-TW', 'en-US'])).toBe('zh-CN');
    expect(negotiateLocale(['zh'])).toBe('zh-CN');
  });

  it('respects the order the browser gave', () => {
    expect(negotiateLocale(['fr-FR', 'en-GB', 'zh-CN'])).toBe('en');
  });

  it('falls back to the default when nothing is shipped', () => {
    expect(negotiateLocale(['fr', 'de'])).toBe(DEFAULT_LOCALE);
    expect(negotiateLocale([])).toBe(DEFAULT_LOCALE);
  });
});

describe('stored locale', () => {
  it('round-trips, and reports null when nothing was ever chosen', () => {
    const store = storage();
    // Null rather than the default: "never chose" has to stay distinguishable
    // from "chose English", or negotiation could never run.
    expect(readStoredLocale(store)).toBeNull();
    writeStoredLocale('zh-CN', store);
    expect(store.read(LOCALE_STORAGE_KEY)).toBe('zh-CN');
    expect(readStoredLocale(store)).toBe('zh-CN');
  });

  it('ignores an unrecognised value', () => {
    expect(readStoredLocale(storage({ [LOCALE_STORAGE_KEY]: 'klingon' }))).toBeNull();
  });
});

describe('translate', () => {
  it('substitutes placeholders', () => {
    expect(translate('en', 'sidebar.showMore', { count: 7 })).toBe('Show 7 more');
    expect(translate('zh-CN', 'sidebar.showMore', { count: 7 })).toBe('显示另外 7 条');
  });

  it('leaves an unsupplied placeholder visible rather than blanking it', () => {
    expect(translate('en', 'sidebar.showMore')).toBe('Show {count} more');
  });

  it('answers in the requested language', () => {
    expect(translate('en', 'settings.title')).toBe('Setting');
    expect(translate('zh-CN', 'settings.title')).toBe('设置');
  });
});

describe('dictionaries', () => {
  it('leave no string empty', () => {
    for (const locale of LOCALES) {
      const blank = Object.entries(DICTIONARIES[locale])
        .filter(([, value]) => value.trim() === '')
        .map(([key]) => `${locale}:${key}`);
      expect(blank).toEqual([]);
    }
  });

  it('use the same placeholders in every language', () => {
    // The failure this catches is silent: a translation that drops `{count}`
    // renders a sentence that reads fine and has lost its number.
    const holes = (value: string) => [...value.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
    const mismatched = (Object.keys(en) as (keyof typeof en)[])
      .filter((key) => {
        const source = holes(en[key]);
        return LOCALES.some(
          (locale) => JSON.stringify(holes(DICTIONARIES[locale][key])) !== JSON.stringify(source),
        );
      })
      .map(String);
    expect(mismatched).toEqual([]);
  });
});

describe('isLocale', () => {
  it('accepts shipped tags only', () => {
    expect(LOCALES.every(isLocale)).toBe(true);
    expect([null, 'zh', 'en-US', ''].some(isLocale)).toBe(false);
  });
});

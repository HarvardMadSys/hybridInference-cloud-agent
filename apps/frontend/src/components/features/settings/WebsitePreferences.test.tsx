// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { ThemeProvider } from '@/components/providers/ThemeProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { DARK_CLASS, THEME_STORAGE_KEY } from '@/lib/theme';
import { WebsitePreferences } from './WebsitePreferences';

let systemDark = false;
let themeListeners: Set<(event: MediaQueryListEvent) => void>;

beforeEach(() => {
  localStorage.clear();
  document.documentElement.classList.remove(DARK_CLASS);
  document.documentElement.style.colorScheme = '';
  document.documentElement.lang = 'en';
  systemDark = false;
  themeListeners = new Set();
  vi.stubGlobal(
    'matchMedia',
    vi.fn((media: string) => ({
      get matches() {
        return systemDark;
      },
      media,
      addEventListener: (_event: string, listener: (event: MediaQueryListEvent) => void) =>
        themeListeners.add(listener),
      removeEventListener: (_event: string, listener: (event: MediaQueryListEvent) => void) =>
        themeListeners.delete(listener),
    })),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  document.documentElement.classList.remove(DARK_CLASS);
  document.documentElement.style.colorScheme = '';
  document.documentElement.lang = 'en';
});

function renderPreferences() {
  return render(
    <ThemeProvider>
      <LocaleProvider>
        <WebsitePreferences />
      </LocaleProvider>
    </ThemeProvider>,
  );
}

describe('website preferences', () => {
  it('applies and persists light, dark, and system choices immediately', () => {
    renderPreferences();
    const theme = screen.getByRole('combobox', { name: 'Theme' });

    fireEvent.change(theme, { target: { value: 'dark' } });
    expect(document.documentElement).toHaveClass(DARK_CLASS);
    expect(document.documentElement.style.colorScheme).toBe('dark');
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark');

    fireEvent.change(theme, { target: { value: 'light' } });
    expect(document.documentElement).not.toHaveClass(DARK_CLASS);
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');

    fireEvent.change(theme, { target: { value: 'system' } });
    expect(localStorage.getItem(THEME_STORAGE_KEY)).toBe('system');
    act(() => {
      systemDark = true;
      themeListeners.forEach((listener) => listener({ matches: true } as MediaQueryListEvent));
    });
    expect(document.documentElement).toHaveClass(DARK_CLASS);
    expect(theme).toHaveValue('system');
  });

  it('restores stored website preferences when the controls mount', () => {
    localStorage.setItem(THEME_STORAGE_KEY, 'dark');
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    renderPreferences();

    expect(screen.getByRole('combobox', { name: '主题' })).toHaveValue('dark');
    expect(screen.getByRole('combobox', { name: '界面语言' })).toHaveValue('zh-CN');
    expect(document.documentElement).toHaveClass(DARK_CLASS);
    expect(document.documentElement.lang).toBe('zh-Hans');
  });

  it('retranslates controls and remembers the display language', () => {
    renderPreferences();
    expect(screen.getByRole('option', { name: 'English' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '简体中文' })).toBeInTheDocument();

    fireEvent.change(screen.getByRole('combobox', { name: 'Display language' }), {
      target: { value: 'zh-CN' },
    });

    expect(screen.getByRole('combobox', { name: '主题' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: '深色' })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe('zh-Hans');
    expect(localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('zh-CN');
  });
});

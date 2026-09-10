// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { ThemeProvider } from '@/components/providers/ThemeProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { DARK_CLASS, THEME_STORAGE_KEY } from '@/lib/theme';
import { SettingsLauncher } from './SettingsLauncher';

vi.mock('@/components/providers/SiteConfigProvider', () => ({
  useSiteConfig: () => ({ branding: { gatewayUrl: 'https://gateway.example' } }),
}));

function renderSettings() {
  return render(
    <ThemeProvider>
      <LocaleProvider>
        <SettingsLauncher />
      </LocaleProvider>
    </ThemeProvider>,
  );
}

async function openDialog() {
  fireEvent.click(await screen.findByRole('button', { name: /My account|我的账户/ }));
  return screen.findByRole('dialog');
}

beforeEach(() => {
  window.localStorage.clear();
  document.documentElement.className = '';
  document.documentElement.lang = 'en';
});

afterEach(cleanup);

describe('the settings entry', () => {
  it('links to archive management from settings', async () => {
    renderSettings();
    await openDialog();
    expect(screen.getByRole('link', { name: 'Archived sessions' })).toHaveAttribute(
      'href',
      '/archived',
    );
  });
  it('shows who is signed in, and opens settings when the row is clicked', async () => {
    renderSettings();
    const trigger = await screen.findByRole('button', { name: /My account|我的账户/ });
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    fireEvent.click(trigger);

    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
  });

  it('hands focus back to the row when the dialog closes', async () => {
    renderSettings();
    const trigger = await screen.findByRole('button', { name: /My account|我的账户/ });
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole('button', { name: 'Done' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    // Otherwise focus falls to <body> and a keyboard user restarts from the
    // top of the document.
    expect(document.activeElement).toBe(trigger);
  });

  it('closes on Escape', async () => {
    renderSettings();
    const dialog = await openDialog();
    fireEvent.keyDown(dialog, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('moves focus into the dialog when it opens', async () => {
    renderSettings();
    const dialog = await openDialog();
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
  });
});

describe('theme', () => {
  it('paints the document dark and remembers the choice', async () => {
    renderSettings();
    await openDialog();

    fireEvent.click(screen.getByRole('radio', { name: 'Dark' }));

    await waitFor(() => expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(true));
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('dark');
  });

  it('goes back to light, and stores `system` as `system`', async () => {
    renderSettings();
    await openDialog();

    fireEvent.click(screen.getByRole('radio', { name: 'Dark' }));
    await waitFor(() => expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(true));

    fireEvent.click(screen.getByRole('radio', { name: 'Light' }));
    await waitFor(() =>
      expect(document.documentElement.classList.contains(DARK_CLASS)).toBe(false),
    );
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('light');

    fireEvent.click(screen.getByRole('radio', { name: 'System' }));
    await waitFor(() => expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe('system'));
  });

  it('reopens on the choice that was made', async () => {
    window.localStorage.setItem(THEME_STORAGE_KEY, 'dark');
    renderSettings();
    await openDialog();
    await waitFor(() => expect(screen.getByRole('radio', { name: 'Dark' })).toBeChecked());
  });
});

describe('language', () => {
  it('retranslates the interface and updates the document language', async () => {
    renderSettings();
    await openDialog();
    expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument();

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'zh-CN' } });

    expect(await screen.findByRole('heading', { name: '设置' })).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: '深色' })).toBeInTheDocument();
    // Not `zh-CN`: the html attribute wants a script-qualified tag.
    await waitFor(() => expect(document.documentElement.lang).toBe('zh-Hans'));
    expect(window.localStorage.getItem(LOCALE_STORAGE_KEY)).toBe('zh-CN');
  });

  it('lists each language under its own name', async () => {
    renderSettings();
    await openDialog();
    // "Chinese (Simplified)" is no help to a reader who cannot read the
    // English around it.
    expect(screen.getByRole('option', { name: '简体中文' })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: 'English' })).toBeInTheDocument();
  });

  it('starts in the stored language', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: /My account|我的账户/ }));
    expect(await screen.findByRole('heading', { name: '设置' })).toBeInTheDocument();
  });
});

it('shows only an account-management link, without a copied profile', async () => {
  renderSettings();
  await openDialog();
  expect(screen.getByText('Your account is managed by the Gateway.')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'Manage account' })).toHaveAttribute(
    'href',
    'https://gateway.example/dashboard',
  );
  expect(screen.queryByText('Ada')).not.toBeInTheDocument();
  expect(screen.queryByText('ada@example.com')).not.toBeInTheDocument();
});

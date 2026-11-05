// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useSyncExternalStore } from 'react';

import { getAgentAdminSettings } from '@/lib/api/adminSettings';
import { chooseOption } from '@/components/ui/__tests__/select';
import AdminSettingsPage from './page';
import { accountProviderSnapshot } from './settingsTestFixtures';

const search = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  return {
    value: '',
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    read: () => search.value,
    set(next: string) {
      search.value = next;
      for (const listener of listeners) listener();
    },
  };
});
vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(useSyncExternalStore(search.subscribe, search.read)),
}));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      user: {
        id: 'admin-a',
        role: 'free',
        agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
      },
    },
  }),
}));
vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: vi.fn(),
  setAgentAdminSettings: vi.fn(),
}));
vi.mock('@/lib/api/admin', async (original) => ({
  ...(await original<typeof import('@/lib/api/admin')>()),
  getAgentRunnerHosts: vi.fn(async () => ({
    hosts: [],
    host_setting_definitions: [],
    runtime_versions: {},
    host_settings: {},
  })),
}));
vi.mock('@/lib/api/adminArchiveSettings', async (original) => ({
  ...(await original<typeof import('@/lib/api/adminArchiveSettings')>()),
  getSandboxArchiveSettings: vi.fn(async () => null),
}));
vi.mock('@/lib/api/adminRuntimeUpgrades', async (original) => ({
  ...(await original<typeof import('@/lib/api/adminRuntimeUpgrades')>()),
  getRuntimeUpgrades: vi.fn(async () => ({ runtimes: [] })),
}));

beforeEach(() => {
  vi.clearAllMocks();
  search.value = '';
  window.history.replaceState(null, '', '/admin/settings');
  const replace = window.history.replaceState.bind(window.history);
  vi.spyOn(window.history, 'replaceState').mockImplementation((data, unused, url) => {
    replace(data, unused, url);
    search.set(window.location.search);
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, '', '/');
});

function searchResults(query: string): string[] {
  fireEvent.change(screen.getByRole('searchbox', { name: 'Search settings' }), {
    target: { value: query },
  });
  const region = screen.queryByRole('region', { name: 'Settings search results' });
  if (!region) return [];
  return within(region)
    .queryAllByRole('button')
    .map((button) => button.getAttribute('aria-label') ?? '');
}

function field(key: string): HTMLElement | null {
  return document.getElementById(`application-setting-${key}`);
}

describe('settings search over provider settings', () => {
  it('finds only fields that apply and leads to the provider that governs the rest', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(
      accountProviderSnapshot({ 'accounts.sms_provider': 'tencent' }),
    );
    render(<AdminSettingsPage />);
    await screen.findByRole('tab', { name: 'Identity & access' });

    expect(searchResults('SMTP host')).toEqual(['Open SMTP host in Identity & access']);
    expect(searchResults('Tencent SES sender')).toEqual([]);
    expect(searchResults('Tencent SMS template')).toEqual([]);
    expect(searchResults('SecretKey')).toEqual([]);
    // Searching a hidden provider by name still finds the setting that selects it.
    expect(searchResults('tencent sms')).toEqual(['Open Phone sign-in in Identity & access']);
    expect(searchResults('tencent ses')).toEqual(['Open Email provider in Identity & access']);

    fireEvent.click(
      within(screen.getByRole('region', { name: 'Settings search results' })).getByRole('button', {
        name: 'Open Email provider in Identity & access',
      }),
    );
    await waitFor(() => expect(field('accounts.email_provider')).toHaveFocus());
  });

  it('follows an unsaved provider choice and never opens a field it hides', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(accountProviderSnapshot());
    render(<AdminSettingsPage />);
    await screen.findByRole('tab', { name: 'Identity & access' });
    fireEvent.click(screen.getByRole('button', { name: 'Edit Account email settings' }));
    chooseOption(screen.getByRole('combobox', { name: 'Email provider' }), 'Tencent SES');

    expect(searchResults('SMTP host')).toEqual([]);
    expect(searchResults('SecretKey')).toEqual([
      'Open Tencent Cloud SecretKey in Identity & access',
    ]);
    fireEvent.click(
      within(screen.getByRole('region', { name: 'Settings search results' })).getByRole('button', {
        name: 'Open Tencent Cloud SecretKey in Identity & access',
      }),
    );
    await waitFor(() => expect(field('accounts.tencent_secret_key')).toHaveFocus());

    // The unsaved choice is withdrawn: an old link to a credential opens the provider instead.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel editing Account email' }));
    expect(searchResults('SecretKey')).toEqual([]);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Edit Account email settings' })).toHaveFocus(),
    );
    window.location.hash = '#application-setting-accounts.tencent_secret_key';
    await waitFor(() => expect(field('accounts.email_provider')).toHaveFocus());
    expect(field('accounts.tencent_secret_key')).toBeNull();
  });
});

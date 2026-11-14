// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useSyncExternalStore } from 'react';

import { listOperatorModelConnections } from '@/lib/api/adminOperatorModels';
import { getAgentAdminSettings } from '@/lib/api/adminSettings';

import AdminSettingsPage from './page';
import { providedList, refusal } from './providedModelsTestFixtures';
import { applicationSnapshot } from './settingsTestFixtures';

// The Models tab of Admin → Settings: where it sits, when it reads the server,
// and how search and a link reach it.

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
vi.mock('@/components/providers', async () => ({
  // The shared connection pieces the section reuses translate through here.
  useT: (await import('@/components/providers/LocaleProvider')).useT,
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
vi.mock('@/lib/api/compute', async (original) => ({
  ...(await original<typeof import('@/lib/api/compute')>()),
  getCloudLabDefaults: vi.fn(() => new Promise(() => {})),
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
vi.mock('@/lib/api/adminOperatorModels', async (original) => ({
  ...(await original<typeof import('@/lib/api/adminOperatorModels')>()),
  listOperatorModelConnections: vi.fn(),
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
  vi.mocked(getAgentAdminSettings).mockResolvedValue(applicationSnapshot());
  vi.mocked(listOperatorModelConnections).mockResolvedValue(providedList());
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, '', '/');
});

function tabNames(): (string | null)[] {
  return within(screen.getByRole('tablist', { name: 'Settings sections' }))
    .getAllByRole('tab')
    .map((tab) => tab.textContent);
}

describe('the Models tab of admin Settings', () => {
  it('sits after Sandbox, and reads nothing until it is opened', async () => {
    render(<AdminSettingsPage />);
    await waitFor(() => expect(getAgentAdminSettings).toHaveBeenCalled());
    const names = tabNames();
    expect(names.slice(-4)).toEqual(['Agent versions', 'Sandbox', 'Models', 'CloudLab']);
    expect(screen.getByRole('tab', { name: 'Models' })).toHaveAttribute('aria-selected', 'false');
    expect(listOperatorModelConnections).not.toHaveBeenCalled();
    expect(screen.queryByRole('region', { name: 'Models this deployment provides' })).toBeNull();
  });

  it('shows the provided connections as the one visible panel when selected', async () => {
    render(<AdminSettingsPage />);
    const tab = screen.getByRole('tab', { name: 'Models' });
    fireEvent.click(tab);
    expect(tab).toHaveAttribute('aria-selected', 'true');
    expect(tab).toHaveAttribute('tabindex', '0');
    const panel = screen.getByRole('tabpanel', { name: 'Models' });
    expect(screen.getAllByRole('tabpanel')).toEqual([panel]);
    expect(tab).toHaveAttribute('aria-controls', panel.id);
    expect(panel).toHaveAttribute('aria-labelledby', tab.id);
    expect(await within(panel).findByRole('article', { name: 'House models' })).toBeVisible();
    expect(window.location.search).toBe('?tab=models');
    expect(listOperatorModelConnections).toHaveBeenCalledTimes(1);
  });

  it('is reached with the arrow keys between Sandbox and CloudLab', async () => {
    render(<AdminSettingsPage />);
    const sandbox = screen.getByRole('tab', { name: 'Sandbox' });
    fireEvent.click(sandbox);
    sandbox.focus();
    fireEvent.keyDown(sandbox, { key: 'ArrowRight' });
    const models = screen.getByRole('tab', { name: 'Models' });
    expect(models).toHaveFocus();
    expect(models).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(models, { key: 'ArrowRight' });
    expect(screen.getByRole('tab', { name: 'CloudLab' })).toHaveFocus();
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowLeft' });
    expect(models).toHaveFocus();
  });

  it('opens from a link, and keeps what it loaded while another tab is shown', async () => {
    window.history.replaceState(null, '', '/admin/settings?tab=models');
    render(<AdminSettingsPage />);
    expect(screen.getByRole('tab', { name: 'Models' })).toHaveAttribute('aria-selected', 'true');
    const panel = screen.getByRole('tabpanel', { name: 'Models' });
    await within(panel).findByRole('article', { name: 'House models' });

    fireEvent.click(screen.getByRole('tab', { name: 'Sandbox' }));
    expect(panel).not.toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: 'Models' }));
    expect(within(panel).getByRole('article', { name: 'House models' })).toBeVisible();
    // Switching away and back asked nothing again.
    expect(listOperatorModelConnections).toHaveBeenCalledTimes(1);
  });

  it('is found by settings search, which opens the tab at its heading', async () => {
    render(<AdminSettingsPage />);
    await waitFor(() => expect(getAgentAdminSettings).toHaveBeenCalled());
    for (const query of ['provided models', 'allowance', 'daily requests', 'model relay']) {
      fireEvent.change(screen.getByRole('searchbox', { name: 'Search settings' }), {
        target: { value: query },
      });
      expect(
        within(screen.getByRole('region', { name: 'Settings search results' }))
          .getAllByRole('button')
          .map((button) => button.getAttribute('aria-label')),
      ).toContain('Open Provided models in Models');
    }
    fireEvent.click(screen.getByRole('button', { name: 'Open Provided models in Models' }));
    expect(screen.getByRole('tab', { name: 'Models' })).toHaveAttribute('aria-selected', 'true');
    // Focus lands on the section's heading at once, before its list has loaded.
    await waitFor(() =>
      expect(
        screen.getByRole('heading', { name: 'Models this deployment provides' }),
      ).toHaveFocus(),
    );
    expect(await screen.findByRole('button', { name: 'Add a provided connection' })).toBeVisible();
  });

  it('closes the whole page when the server says admin access has ended', async () => {
    vi.mocked(listOperatorModelConnections).mockRejectedValue(
      refusal('forbidden', 'Admin access required.', 403),
    );
    render(<AdminSettingsPage />);
    fireEvent.click(screen.getByRole('tab', { name: 'Models' }));
    expect(await screen.findByRole('heading', { name: 'Admin access required' })).toBeVisible();
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
  });
});

// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useSyncExternalStore } from 'react';

import { getAgentAdminSettings } from '@/lib/api/adminSettings';
import AdminSettingsPage from './page';
import { applicationSnapshot } from './settingsTestFixtures';

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
  window.history.replaceState(null, '', '/admin/settings');
  search.value = '';
  const replace = window.history.replaceState.bind(window.history);
  vi.spyOn(window.history, 'replaceState').mockImplementation((data, unused, url) => {
    replace(data, unused, url);
    search.set(window.location.search);
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function selectSettingsTab(name: string) {
  fireEvent.click(screen.getByRole('tab', { name }));
}
function renderSettings(tab?: string) {
  render(<AdminSettingsPage />);
  if (tab) selectSettingsTab(tab);
}
function editField(label: string) {
  let field = screen.getByLabelText(label);
  if (!field.matches('input,select,textarea,[role="combobox"]')) {
    const group = field.closest('form')!.parentElement!;
    fireEvent.click(within(group).getByRole('button', { name: /^Edit .* settings$/ }));
    field = screen.getByLabelText(label);
  }
  return field;
}
async function loadedField(label: string) {
  await screen.findByLabelText(label);
  return editField(label);
}
function searchFor(query: string) {
  fireEvent.change(screen.getByRole('searchbox', { name: 'Search settings' }), {
    target: { value: query },
  });
  return screen.getByRole('region', { name: 'Settings search results' });
}

describe('deployment settings navigation', () => {
  it('mounts one Deployment editor at the end and keeps application drafts across tabs', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(
      applicationSnapshot({
        bootstrap: [
          {
            id: 'database',
            key: 'AGENT_DATABASE_URL',
            label: 'Database connection',
            description: 'Deployment-managed connection.',
            state: 'configured',
            actions: [{ target: 'database', label: 'Open Admin Database' }],
          },
        ],
      }),
    );
    renderSettings();
    fireEvent.change(await loadedField('Application name'), {
      target: { value: 'Draft application' },
    });
    selectSettingsTab('Deployment');
    expect(screen.getByRole('tabpanel', { name: 'Deployment' })).toBeVisible();
    expect(document.querySelectorAll('#deployment-database')).toHaveLength(1);
    expect(screen.getByText('Configuration present')).toBeVisible();
    selectSettingsTab('Appearance');
    selectSettingsTab('Deployment');
    selectSettingsTab('Appearance');
    expect(editField('Application name')).toHaveValue('Draft application');
    expect(document.querySelectorAll('#deployment-database')).toHaveLength(1);
  });

  it('opens Deployment from a bootstrap search result and focuses the row', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(
      applicationSnapshot({
        bootstrap: [
          {
            key: 'AGENT_DATABASE_URL',
            label: 'Database connection',
            description: 'Deployment-managed connection.',
          },
        ],
      }),
    );
    renderSettings('Sandbox');
    await screen.findByRole('tab', { name: 'Appearance' });
    fireEvent.click(
      within(searchFor('agent_database_url')).getByRole('button', {
        name: 'Open Database connection in Deployment setup',
      }),
    );
    const row = document.getElementById('deployment-AGENT_DATABASE_URL')!;
    await waitFor(() => expect(row).toHaveFocus());
    expect(screen.getByRole('tabpanel', { name: 'Deployment' })).toBeVisible();
    expect(new URLSearchParams(window.location.search).get('tab')).toBe('deployment');
    expect(document.getElementById('application-bootstrap-AGENT_DATABASE_URL')).not.toBeNull();
  });

  it.each(['#deployment-database', '#application-bootstrap-AGENT_DATABASE_URL'])(
    'selects Deployment and focuses the row for the %s hash',
    async (hash) => {
      vi.mocked(getAgentAdminSettings).mockResolvedValue(
        applicationSnapshot({
          bootstrap: [
            {
              id: 'database',
              key: 'AGENT_DATABASE_URL',
              label: 'Database connection',
              description: 'Deployment-managed connection.',
            },
          ],
        }),
      );
      window.history.replaceState(null, '', `/admin/settings${hash}`);
      renderSettings();
      await waitFor(() => expect(document.getElementById('deployment-database')).toHaveFocus());
      expect(screen.getByRole('tabpanel', { name: 'Deployment' })).toBeVisible();
      expect(window.location.search).toBe('?tab=deployment');
      expect(document.getElementById('application-bootstrap-AGENT_DATABASE_URL')).not.toBeNull();
    },
  );

  it('updates the deep link when a deployment action reveals a setting', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(
      applicationSnapshot({
        definitions: [
          {
            key: 'pgadmin_url',
            label: 'pgAdmin URL',
            description: '',
            category: 'Database tools',
            kind: 'string',
            default: '',
            secret: false,
          },
        ],
        values: { pgadmin_url: '' },
        bootstrap: [
          {
            id: 'pgadmin_bootstrap',
            key: 'AGENT_PGADMIN_BOOTSTRAP_URL',
            label: 'pgAdmin setup',
            description: '',
            actions: [{ target: 'pgadmin_url', label: 'Edit pgAdmin URL' }],
          },
        ],
      }),
    );
    window.history.replaceState(null, '', '/admin/settings#deployment-pgadmin_bootstrap');
    renderSettings();
    const action = await screen.findByRole('button', { name: 'Edit pgAdmin URL' });
    fireEvent.click(action);
    await waitFor(() => expect(window.location.hash).toBe('#application-setting-pgadmin_url'));
    expect(screen.getByRole('tabpanel', { name: 'Operations' })).toBeVisible();
    expect(screen.getByLabelText('pgAdmin URL')).toHaveFocus();
  });
});

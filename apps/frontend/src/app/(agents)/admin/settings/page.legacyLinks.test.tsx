// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useSyncExternalStore } from 'react';

import { getAgentAdminSettings, type AgentAdminSettings } from '@/lib/api/adminSettings';
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
vi.mock('@/lib/api/adminManagement', () => import('./settingsTestFixtures').then((f) => f.noApi));
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

// The keys of the removed Deployment rows, as the API still returns them.
const BOOTSTRAP: AgentAdminSettings['bootstrap'] = [
  { id: 'database', key: 'AGENT_DATABASE_URL', label: 'Database connection', description: '' },
  {
    id: 'github_app_key_file',
    key: 'AGENT_GITHUB_APP_PRIVATE_KEY_PATH',
    label: 'Mounted GitHub App key',
    description: '',
  },
  {
    id: 'compute_config_file',
    key: 'AGENT_COMPUTE_CONFIG',
    label: 'Compute deployment profiles',
    description: '',
  },
];

function snapshot(): AgentAdminSettings {
  const base = applicationSnapshot();
  return applicationSnapshot({
    definitions: [
      ...base.definitions,
      {
        key: 'github_app_private_key',
        label: 'GitHub App private key',
        description: 'PEM private key.',
        category: 'Source control',
        kind: 'string',
        default: '',
        secret: true,
      },
      {
        key: 'pgadmin_url',
        label: 'pgAdmin URL',
        description: 'Database tools address.',
        category: 'Database tools',
        kind: 'string',
        default: '',
        secret: false,
      },
    ],
    values: { ...base.values, github_app_private_key: null, pgadmin_url: '' },
    secret_configured: { github_app_private_key: false },
    bootstrap: BOOTSTRAP,
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getAgentAdminSettings).mockResolvedValue(snapshot());
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
  window.history.replaceState(null, '', '/');
});

function open(path: string) {
  window.history.replaceState(null, '', path);
  render(<AdminSettingsPage />);
}

function expectNoDeploymentSection() {
  expect(screen.queryByRole('tab', { name: 'Deployment' })).not.toBeInTheDocument();
  expect(screen.queryByText('Deployment setup')).not.toBeInTheDocument();
  expect(document.querySelector('[id^="deployment-"]')).toBeNull();
  expect(document.querySelector('[id^="application-bootstrap-"]')).toBeNull();
}

describe('links into the removed Deployment section', () => {
  it('describes the saved GitHub key without claiming all processes have applied it', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue({
      ...snapshot(),
      secret_configured: { github_app_private_key: true },
    });
    open('/admin/settings?tab=application-Source%20control');
    expect(
      await screen.findByText(/A saved key is configured and takes precedence.*when applied/),
    ).toBeVisible();
    expect(screen.queryByText(/mounted key file is not read/)).not.toBeInTheDocument();
  });

  it.each([
    ['#application-bootstrap-AGENT_GITHUB_APP_PRIVATE_KEY_PATH', 'Source control'],
    ['#deployment-github_app_key_file', 'Source control'],
    [
      '#application-bootstrap-AGENT_PGADMIN_BOOTSTRAP_URL%20%2F%20AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL%20%2F%20AGENT_PGADMIN_BOOTSTRAP_DIR',
      'Operations',
    ],
    ['#deployment-pgadmin_bootstrap', 'Operations'],
  ])('opens the editable setting that manages %s', async (hash, tab) => {
    open(`/admin/settings?tab=deployment${hash}`);
    const key = /pgadmin/i.test(hash) ? 'pgadmin_url' : 'github_app_private_key';
    const id = `application-setting-${key}`;
    await waitFor(() => expect(document.getElementById(id)).toHaveFocus());
    expect(screen.getByRole('tabpanel', { name: tab })).toBeVisible();
    expect(window.location.hash).toBe(`#${id}`);
    expect(new URLSearchParams(window.location.search).get('tab')).toBe(
      `application-${encodeURIComponent(tab)}`,
    );
    expectNoDeploymentSection();
  });

  it.each([
    '#deployment-database',
    '#application-bootstrap-AGENT_DATABASE_URL',
    '#deployment-compute_config_file',
    '#application-bootstrap-AGENT_COMPUTE_CONFIG',
    '#deployment-settings_root_key',
    '#deployment-unknown-row',
    '#application-bootstrap-%E0%A4%A',
  ])('opens the settings page normally for %s, which no setting manages', async (hash) => {
    open(`/admin/settings?tab=deployment${hash}`);
    await waitFor(() => expect(window.location.hash).toBe(''));
    expect(screen.getByRole('tab', { name: 'Appearance' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.getByRole('tabpanel', { name: 'Appearance' })).toBeVisible();
    expectNoDeploymentSection();
    // The former row actions are gone; nothing stands in for what this page cannot manage.
    for (const name of [
      /Open Compute setup/,
      /Open Admin Database/,
      /View deployment guide/,
      /Read key management notes/,
    ]) {
      expect(screen.queryByRole('link', { name })).not.toBeInTheDocument();
    }
    expect(screen.queryByRole('button', { name: /rotat|migrat/i })).not.toBeInTheDocument();
  });

  it('keeps a legacy link pending until definitions show whether its setting exists', async () => {
    let resolve!: (value: AgentAdminSettings) => void;
    vi.mocked(getAgentAdminSettings).mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    open('/admin/settings#deployment-pgadmin_bootstrap');
    expect(window.location.hash).toBe('#deployment-pgadmin_bootstrap');
    await act(async () => resolve(snapshot()));
    await waitFor(() => expect(screen.getByLabelText('pgAdmin URL')).toHaveFocus());
    expect(window.location.hash).toBe('#application-setting-pgadmin_url');
  });

  it('falls back to the page when the managing setting is not offered', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(
      applicationSnapshot({ bootstrap: BOOTSTRAP }),
    );
    open('/admin/settings#deployment-github_app_key_file');
    await waitFor(() => expect(window.location.hash).toBe(''));
    expect(screen.getByRole('tabpanel', { name: 'Appearance' })).toBeVisible();
  });

  it('handles a legacy link followed later and does not refocus on refresh', async () => {
    open('/admin/settings');
    await screen.findByRole('tab', { name: 'Source control' });
    act(() => {
      window.location.hash = 'deployment-github_app_key_file';
    });
    const field = screen.getByLabelText('GitHub App private key');
    await waitFor(() => expect(field).toHaveFocus());
    expect(window.location.hash).toBe('#application-setting-github_app_private_key');

    const searchbox = screen.getByRole('searchbox', { name: 'Search settings' });
    act(() => searchbox.focus());
    fireEvent.click(screen.getByRole('button', { name: 'Refresh settings' }));
    await waitFor(() => expect(getAgentAdminSettings).toHaveBeenCalledTimes(2));
    await act(async () => {});
    expect(searchbox).toHaveFocus();
  });
});

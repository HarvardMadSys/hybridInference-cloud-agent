// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useSyncExternalStore } from 'react';

import type { AgentRunnerHost, AgentRunnerHostPool, HostSettingDefinition } from '@/lib/api/admin';
import type { SandboxArchiveSettings } from '@/lib/api/adminArchiveSettings';
import type { AgentAccessApi } from '@/lib/api/session';
import { APIError } from '@/lib/utils/errors';

const auth = vi.hoisted(() => ({ id: 'admin-a', access: undefined as AgentAccessApi | undefined }));
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
  useAuth: () => ({ state: { user: { id: auth.id, role: 'free', agent_access: auth.access } } }),
}));
vi.mock('@/components/providers/AuthProvider', async () => {
  const actual = await vi.importActual<typeof import('@/components/providers/AuthProvider')>(
    '@/components/providers/AuthProvider',
  );
  return { hasAgentPermission: actual.hasAgentPermission };
});
vi.mock('@/lib/api/admin', () => ({
  getAgentRunnerHosts: vi.fn(),
  setAgentHostSettings: vi.fn(),
  setAgentRuntimeVersion: vi.fn(),
  HOST_SETTINGS_CAPABILITY: 'host-settings-v1',
}));
vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: vi.fn(),
  setAgentAdminSettings: vi.fn(),
}));
vi.mock('@/lib/api/compute', () => ({
  getCloudLabDefaults: vi.fn(),
  saveCloudLabDefaults: vi.fn(),
}));
vi.mock('@/lib/api/adminArchiveSettings', () => ({
  getSandboxArchiveSettings: vi.fn(),
  updateSandboxArchiveSettings: vi.fn(),
  SANDBOX_ARCHIVE_SETTINGS_CAPABILITY: 'sandbox-archive-settings-v1',
}));

import { getAgentRunnerHosts, setAgentHostSettings, setAgentRuntimeVersion } from '@/lib/api/admin';
import {
  getSandboxArchiveSettings,
  updateSandboxArchiveSettings,
} from '@/lib/api/adminArchiveSettings';
import { getAgentAdminSettings, setAgentAdminSettings } from '@/lib/api/adminSettings';
import { getCloudLabDefaults, saveCloudLabDefaults } from '@/lib/api/compute';

import AdminSettingsPage from './page';

const list = vi.mocked(getAgentRunnerHosts);
const setLimit = vi.mocked(setAgentHostSettings);
const setVersion = vi.mocked(setAgentRuntimeVersion);
const cloudLabDefaults = {
  project: 'mad-cloudlab',
  duration: 4,
  profile_project: 'cloud-agent',
  profile_name: 'kata-node',
  ssh_username: 'researcher',
};

const getArchiveSettings = vi.mocked(getSandboxArchiveSettings);
const updateArchiveSettings = vi.mocked(updateSandboxArchiveSettings);

const archiveSettings: SandboxArchiveSettings = {
  enabled: false,
  s3_uri: 's3://freeinference/cloud-agent/sandbox',
  region: 'us-east-1',
  kms_key_arn: 'arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000',
  expected_bucket_owner: '123456789012',
  max_archive_bytes: 2_147_483_648,
  multipart_threshold_bytes: 8_388_608,
  multipart_part_bytes: 8_388_608,
  revision: 1,
  updated_at: null,
};

const definitions: HostSettingDefinition[] = [
  {
    key: 'agent_timeout_s',
    env: 'AGENT_TIMEOUT_S',
    label: 'Turn time limit',
    description: 'How long one agent turn may run before the host stops it.',
    unit: 'seconds',
    default: 3600,
    minimum: 60,
    maximum: 86400,
    scope: 'runner',
  },
  {
    key: 'source_import_max_mib',
    env: 'AGENT_SOURCE_IMPORT_MAX_MIB',
    label: 'Repository size limit',
    description: 'Maximum checkout size.',
    unit: 'MiB',
    default: 1024,
    minimum: 1,
    maximum: 65536,
    scope: 'runner',
  },
  {
    key: 'chat_idle_timeout_s',
    env: 'AGENT_CHAT_IDLE_TIMEOUT_S',
    label: 'Sandbox idle timeout',
    description: 'When an idle sandbox stops.',
    unit: 'seconds',
    default: 1800,
    minimum: 60,
    maximum: 604800,
    scope: 'both',
  },
  {
    key: 'shared_resident_capacity',
    env: 'AGENT_SHARED_RESIDENT_CAPACITY',
    label: 'Sessions per sandbox',
    description: 'Capacity of a new sandbox. Existing sandboxes keep theirs.',
    unit: 'count',
    default: 8,
    minimum: 2,
    maximum: 128,
    scope: 'runner',
  },
  {
    key: 'terminal_idle_ttl_s',
    env: 'AGENT_TERMINAL_IDLE_TTL_SECONDS',
    label: 'Terminal idle timeout',
    description: 'When an idle terminal closes.',
    unit: 'seconds',
    default: 1800,
    minimum: 60,
    maximum: 86400,
    scope: 'broker',
  },
  {
    key: 'terminal_hard_ttl_s',
    env: 'AGENT_TERMINAL_HARD_TTL_SECONDS',
    label: 'Terminal max lifetime',
    description: 'Maximum terminal lifetime.',
    unit: 'seconds',
    default: 28800,
    minimum: 60,
    maximum: 86400,
    scope: 'broker',
  },
  {
    key: 'terminal_max_sessions',
    env: 'AGENT_TERMINAL_MAX_SESSIONS',
    label: 'Terminals per server',
    description: 'How many interactive terminals one server may hold open at once.',
    unit: 'count',
    default: 64,
    minimum: 1,
    maximum: 4096,
    scope: 'broker',
  },
];

const resourceDefinitions: HostSettingDefinition[] = [
  {
    key: 'sandbox_cpu_millicores',
    env: 'AGENT_SANDBOX_CPU_MILLICORES',
    label: 'Container CPU',
    description: 'CPU limit for new sandboxes. Existing sandboxes keep their limit.',
    unit: 'millicores',
    default: 2000,
    minimum: 100,
    maximum: 256000,
    scope: 'runner',
  },
  {
    key: 'sandbox_memory_mib',
    env: 'AGENT_SANDBOX_MEMORY_MIB',
    label: 'Container memory',
    description: 'Memory limit for new sandboxes. Existing sandboxes keep their limit.',
    unit: 'MiB',
    default: 4096,
    minimum: 128,
    maximum: 1048576,
    scope: 'runner',
  },
  {
    key: 'sandbox_disk_mib',
    env: 'AGENT_SANDBOX_DISK_MIB',
    label: 'Container disk',
    description: 'Disk quota for new sandboxes. Zero disables the per-container quota.',
    unit: 'MiB',
    default: 0,
    minimum: 0,
    maximum: 16777216,
    scope: 'runner',
  },
];

function host(over: Partial<AgentRunnerHost> = {}): AgentRunnerHost {
  return {
    host: 'runner-a',
    status: 'online',
    enrolled: true,
    active: false,
    sandbox_runtime: 'io.containerd.kata.v2',
    labels: {},
    capabilities: ['host-settings-v1', 'sandbox-archive-settings-v1'],
    runtime_versions: { 'claude-code': '2.1.220' },
    runtime_versions_available: { 'claude-code': ['2.1.220', '2.1.235'] },
    settings_desired: {},
    settings_applied: { agent_timeout_s: 3600, terminal_max_sessions: 64 },
    settings_local: { agent_timeout_s: 3600, terminal_max_sessions: 64 },
    sandbox_archive_settings_applied_revision: 1,
    ...over,
  } as AgentRunnerHost;
}

function pool(over: Partial<AgentRunnerHostPool> = {}): AgentRunnerHostPool {
  return {
    hosts: [host()],
    active_host: null,
    runtime_versions: {},
    execution_profile: 'untrusted-kata-v1',
    observed_at: '2026-10-08T00:00:00Z',
    host_settings: {},
    host_setting_definitions: definitions,
    ...over,
  };
}

function submit(control: HTMLElement) {
  fireEvent.submit(control.closest('form')!);
}

type SettingsTab = 'Application' | 'Agent versions' | 'Sandbox' | 'CloudLab';

function selectSettingsTab(name: SettingsTab) {
  fireEvent.click(screen.getByRole('tab', { name }));
}

function renderSettings(tab: SettingsTab = 'Application') {
  const view = render(<AdminSettingsPage />);
  if (tab !== 'Application') selectSettingsTab(tab);
  return view;
}

beforeEach(() => {
  auth.id = 'admin-a';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  vi.clearAllMocks();
  window.history.replaceState(null, '', '/admin/settings');
  search.value = '';
  const replace = window.history.replaceState.bind(window.history);
  vi.spyOn(window.history, 'replaceState').mockImplementation((data, unused, url) => {
    replace(data, unused, url);
    search.set(window.location.search);
  });
  list.mockReset().mockResolvedValue(pool());
  setLimit.mockReset();
  setVersion.mockReset();
  vi.mocked(getCloudLabDefaults).mockReset().mockResolvedValue({ defaults: cloudLabDefaults });
  vi.mocked(saveCloudLabDefaults).mockReset();
  getArchiveSettings.mockReset().mockResolvedValue(archiveSettings);
  updateArchiveSettings.mockReset();
  vi.mocked(getAgentAdminSettings).mockReset().mockResolvedValue({
    revision: 1,
    definitions: [],
    values: {},
    secret_configured: {},
    bootstrap: [],
  });
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(false);
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('admin Settings', () => {
  it('requires an explicit admin permission before fetching', () => {
    auth.access = { allowed: true, permissions: ['agent.use'] };
    render(<AdminSettingsPage />);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(list).not.toHaveBeenCalled();
    expect(getArchiveSettings).not.toHaveBeenCalled();
    expect(getAgentAdminSettings).not.toHaveBeenCalled();
    expect(getCloudLabDefaults).not.toHaveBeenCalled();
  });

  it('admits a free account with admin permission and opens Application settings', async () => {
    renderSettings();
    await screen.findByText('No application settings are available.');
    expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('tabpanel', { name: 'Application' })).toBeVisible();
    expect(screen.getAllByRole('heading', { level: 2 }).map((h) => h.textContent)).toEqual([
      'Application settings',
    ]);
    selectSettingsTab('Agent versions');
    expect(screen.getByRole('heading', { name: 'Agent versions' })).toBeVisible();
    expect(screen.queryByLabelText('New sandbox location')).not.toBeInTheDocument();
    expect(screen.queryByText('Runtime settings')).not.toBeInTheDocument();
    expect(
      screen.getByText(/Existing sessions and their forks keep their version/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/EXTRA_VERSIONS/)).not.toBeInTheDocument();
    selectSettingsTab('Sandbox');
    expect(screen.getByRole('heading', { name: 'Sandbox' })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Sandbox archives' })).toBeVisible();
  });

  it('exposes one selected tab and its related panel at a time', async () => {
    renderSettings();
    await screen.findByText('No application settings are available.');
    const tablist = screen.getByRole('tablist', { name: 'Settings sections' });
    const names: SettingsTab[] = ['Application', 'Agent versions', 'Sandbox', 'CloudLab'];
    expect(within(tablist).getAllByRole('tab')).toHaveLength(4);
    expect(screen.getAllByRole('tabpanel', { hidden: true })).toHaveLength(4);

    for (const name of names) {
      selectSettingsTab(name);
      const tab = within(tablist).getByRole('tab', { name });
      const panel = screen.getByRole('tabpanel', { name });
      expect(screen.getAllByRole('tabpanel')).toEqual([panel]);
      expect(panel).toBeVisible();
      expect(tab).toHaveAttribute('aria-selected', 'true');
      expect(tab).toHaveAttribute('tabindex', '0');
      expect(tab).toHaveAttribute('aria-controls', panel.id);
      expect(panel).toHaveAttribute('aria-labelledby', tab.id);
      for (const otherName of names.filter((other) => other !== name)) {
        const otherTab = within(tablist).getByRole('tab', { name: otherName });
        expect(otherTab).toHaveAttribute('aria-selected', 'false');
        expect(otherTab).toHaveAttribute('tabindex', '-1');
        expect(document.getElementById(otherTab.getAttribute('aria-controls')!)).not.toBeVisible();
      }
    }
  });

  it('selects and focuses tabs with Arrow, Home, and End keys', async () => {
    renderSettings();
    await screen.findByText('No application settings are available.');
    screen.getByRole('tab', { name: 'Application' }).focus();
    const move = (key: string, name: SettingsTab) => {
      fireEvent.keyDown(document.activeElement!, { key });
      const tab = screen.getByRole('tab', { name });
      expect(tab).toHaveFocus();
      expect(tab).toHaveAttribute('aria-selected', 'true');
      expect(screen.getByRole('tabpanel', { name })).toBeVisible();
    };
    move('ArrowRight', 'Agent versions');
    move('End', 'CloudLab');
    move('ArrowRight', 'Application');
    move('ArrowLeft', 'CloudLab');
    move('Home', 'Application');
  });

  it('opens CloudLab from a deep link and keeps it out of the main Admin navigation', async () => {
    window.history.replaceState(null, '', '/admin/settings?tab=cloudlab');
    renderSettings();
    expect(screen.getByRole('tab', { name: 'CloudLab' })).toHaveAttribute('aria-selected', 'true');
    expect(await screen.findByLabelText('CloudLab project')).toHaveValue('mad-cloudlab');
    expect(screen.getByRole('tabpanel', { name: 'CloudLab' })).toBeVisible();
    const admin = screen.getByRole('navigation', { name: 'Admin sections' });
    expect(within(admin).queryByRole('link', { name: 'CloudLab' })).not.toBeInTheDocument();
    expect(within(admin).getByRole('link', { name: 'Settings' })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });

  it('loads CloudLab on first selection and keeps its draft when switching tabs', async () => {
    renderSettings();
    await screen.findByText('No application settings are available.');
    expect(getCloudLabDefaults).not.toHaveBeenCalled();
    selectSettingsTab('CloudLab');
    const project = await screen.findByLabelText('CloudLab project');
    fireEvent.change(project, { target: { value: 'draft-project' } });
    selectSettingsTab('Application');
    selectSettingsTab('CloudLab');
    expect(screen.getByLabelText('CloudLab project')).toHaveValue('draft-project');
    expect(getCloudLabDefaults).toHaveBeenCalledTimes(1);
  });

  it('keeps tab selection in the URL and follows external navigation without losing drafts', async () => {
    window.history.replaceState(null, '', '/admin/settings?context=keep#details');
    renderSettings();
    selectSettingsTab('CloudLab');
    expect(window.location.search).toBe('?context=keep&tab=cloudlab');
    expect(window.location.hash).toBe('#details');
    const project = await screen.findByLabelText('CloudLab project');
    fireEvent.change(project, { target: { value: 'draft-project' } });
    act(() => window.history.replaceState(null, '', '/admin/settings?tab=versions'));
    expect(screen.getByRole('tab', { name: 'Agent versions' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    act(() => window.history.replaceState(null, '', '/admin/settings?tab=cloudlab'));
    expect(screen.getByRole('tabpanel', { name: 'CloudLab' })).toBeVisible();
    expect(screen.getByLabelText('CloudLab project')).toHaveValue('draft-project');
    selectSettingsTab('Application');
    expect(window.location.search).toBe('');
  });

  it('uses Application for an unknown tab', async () => {
    window.history.replaceState(null, '', '/admin/settings?tab=unknown');
    renderSettings();
    expect(screen.getByRole('tab', { name: 'Application' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    await screen.findByText('No application settings are available.');
    expect(getCloudLabDefaults).not.toHaveBeenCalled();
  });

  it('hides all Settings content when CloudLab administration is revoked', async () => {
    vi.mocked(getCloudLabDefaults).mockRejectedValue(new APIError('FORBIDDEN', 'Forbidden', 403));
    renderSettings('CloudLab');
    await screen.findByRole('heading', { name: 'Admin access required' });
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('CloudLab project')).not.toBeInTheDocument();
  });

  it('preserves unsaved application, version, and sandbox drafts when switching tabs', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue({
      revision: 1,
      definitions: [
        {
          key: 'site.app_name',
          label: 'Application name',
          description: 'Name shown in the application.',
          category: 'Appearance',
          kind: 'string',
          default: 'Cloud Agent',
          secret: false,
        },
      ],
      values: { 'site.app_name': 'Cloud Agent' },
      secret_configured: {},
      bootstrap: [],
    });
    renderSettings();
    fireEvent.change(await screen.findByLabelText('Application name'), {
      target: { value: 'My agent' },
    });
    selectSettingsTab('Agent versions');
    fireEvent.change(await screen.findByLabelText('Claude Code'), {
      target: { value: '2.1.235' },
    });
    selectSettingsTab('Sandbox');
    fireEvent.change(await screen.findByLabelText('Turn time limit for all servers'), {
      target: { value: '2' },
    });

    selectSettingsTab('Application');
    expect(screen.getByLabelText('Application name')).toHaveValue('My agent');
    expect(screen.getByRole('button', { name: 'Save Application name' })).toBeEnabled();
    selectSettingsTab('Agent versions');
    expect(screen.getByLabelText('Claude Code')).toHaveValue('2.1.235');
    selectSettingsTab('Sandbox');
    expect(screen.getByLabelText('Turn time limit for all servers')).toHaveValue(2);
    expect(getAgentAdminSettings).toHaveBeenCalledTimes(1);
    expect(list).toHaveBeenCalledTimes(1);
    expect(setAgentAdminSettings).not.toHaveBeenCalled();
    expect(setVersion).not.toHaveBeenCalled();
    expect(setLimit).not.toHaveBeenCalled();
  });

  it('recovers from an initial read failure without claiming the settings are empty', async () => {
    list.mockRejectedValueOnce(new Error('Service unavailable'));
    renderSettings('Agent versions');
    expect(await screen.findByRole('alert')).toHaveTextContent('Service unavailable');
    expect(
      screen.queryByText('No server has reported available versions yet.'),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByLabelText('Claude Code');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('lists a setting this frontend has never heard of with the others', async () => {
    list.mockResolvedValue(
      pool({
        host_setting_definitions: [
          ...definitions,
          { ...definitions[0], key: 'future_limit', label: 'Future limit' },
        ],
      }),
    );
    renderSettings('Sandbox');
    await screen.findByLabelText('Future limit for all servers');
    const card = screen.getByRole('heading', { name: 'Sandbox' }).parentElement!;
    expect(within(card).getByLabelText('Future limit for all servers')).toBeInTheDocument();
  });
});

describe('agent versions', () => {
  it('offers the server default before every version the pool holds', async () => {
    renderSettings('Agent versions');
    const select = await screen.findByLabelText<HTMLSelectElement>('Claude Code');
    expect([...select.options].map((option) => option.textContent)).toEqual([
      'Server default (2.1.220)',
      '2.1.220',
      '2.1.235',
    ]);
    expect(select).toHaveValue('');
    expect(within(select.closest('form')!).getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('shows the global selection rather than the installed server default', async () => {
    list.mockResolvedValue(pool({ runtime_versions: { 'claude-code': '2.1.235' } }));
    renderSettings('Agent versions');
    expect(await screen.findByLabelText('Claude Code')).toHaveValue('2.1.235');
  });

  it('waits for Save, sends the choice, and takes the returned snapshot', async () => {
    setVersion.mockResolvedValue(pool({ runtime_versions: { 'claude-code': '2.1.235' } }));
    renderSettings('Agent versions');
    const select = await screen.findByLabelText('Claude Code');
    fireEvent.change(select, { target: { value: '2.1.235' } });
    expect(setVersion).not.toHaveBeenCalled();
    submit(select);
    await waitFor(() => expect(setVersion).toHaveBeenCalledWith('claude-code', '2.1.235'));
    expect(await screen.findByRole('status')).toHaveTextContent(
      'New Claude Code sandboxes use 2.1.235. Existing sessions keep their version.',
    );
    expect(screen.getByLabelText('Claude Code')).toHaveValue('2.1.235');
    expect(
      within(screen.getByLabelText('Claude Code').closest('form')!).getByRole('button', {
        name: 'Save',
      }),
    ).toBeDisabled();
  });

  it('clears to the server default with null, not an empty version string', async () => {
    list.mockResolvedValue(pool({ runtime_versions: { 'claude-code': '2.1.235' } }));
    setVersion.mockResolvedValue(pool());
    renderSettings('Agent versions');
    const select = await screen.findByLabelText('Claude Code');
    fireEvent.change(select, { target: { value: '' } });
    submit(select);
    await waitFor(() => expect(setVersion).toHaveBeenCalledWith('claude-code', null));
  });

  it('explains an unavailable menu without hiding the section', async () => {
    list.mockResolvedValue(pool({ hosts: [host({ runtime_versions_available: null })] }));
    renderSettings('Agent versions');
    await screen.findByText('No server has reported available versions yet.');
    expect(screen.getByRole('heading', { name: 'Agent versions' })).toBeInTheDocument();
    expect(screen.queryByLabelText('Claude Code')).not.toBeInTheDocument();
  });

  it('keeps a single-version agent selectable', async () => {
    list.mockResolvedValue(
      pool({ hosts: [host({ runtime_versions_available: { codex: ['0.145.0'] } })] }),
    );
    renderSettings('Agent versions');
    const select = await screen.findByLabelText<HTMLSelectElement>('Codex');
    expect([...select.options].map((option) => option.textContent)).toEqual([
      'Server default',
      '0.145.0',
    ]);
  });

  it('keeps an unavailable saved version visible and lets the admin clear it', async () => {
    list.mockResolvedValue(pool({ hosts: [], runtime_versions: { 'claude-code': '2.1.235' } }));
    setVersion.mockResolvedValue(pool({ hosts: [] }));
    renderSettings('Agent versions');
    const select = await screen.findByLabelText('Claude Code');
    expect(select).toHaveValue('2.1.235');
    expect(screen.getByText('2.1.235 (unavailable)')).toBeInTheDocument();
    fireEvent.change(select, { target: { value: '' } });
    submit(select);
    await waitFor(() => expect(setVersion).toHaveBeenCalledWith('claude-code', null));
  });

  it('names a server that lacks the chosen version, before and after Save', async () => {
    const lacking = host({
      host: 'runner-b',
      runtime_versions_available: { 'claude-code': ['2.1.220'] },
    });
    list.mockResolvedValue(pool({ hosts: [host(), lacking] }));
    setVersion.mockResolvedValue(
      pool({ hosts: [host(), lacking], runtime_versions: { 'claude-code': '2.1.235' } }),
    );
    renderSettings('Agent versions');
    const select = await screen.findByLabelText('Claude Code');
    expect(screen.queryByText(/Not on runner-b/)).not.toBeInTheDocument();
    fireEvent.change(select, { target: { value: '2.1.235' } });
    expect(
      screen.getByText('Not on runner-b: new sandboxes there would use 2.1.220.'),
    ).toBeInTheDocument();
    submit(select);
    expect(
      await screen.findByText('Not on runner-b: new sandboxes there use 2.1.220.'),
    ).toBeInTheDocument();
  });

  it('says when the servers do not agree on a default', async () => {
    list.mockResolvedValue(
      pool({
        hosts: [host(), host({ host: 'runner-b', runtime_versions: { 'claude-code': '2.1.235' } })],
      }),
    );
    renderSettings('Agent versions');
    const select = await screen.findByLabelText<HTMLSelectElement>('Claude Code');
    expect(select.options[0]).toHaveTextContent('Server default (varies by server)');
  });
});

describe('global limits', () => {
  it.each([
    ['Container CPU', 'sandbox_cpu_millicores', 'vCPU', '1.25', 1250],
    ['Container memory', 'sandbox_memory_mib', 'GiB', '8', 8192],
    ['Container disk', 'sandbox_disk_mib', 'GiB', '20', 20480],
  ] as const)(
    'saves %s in the API unit and tracks application separately',
    async (label, key, unit, amount, value) => {
      const defaults = Object.fromEntries(
        resourceDefinitions.map((definition) => [definition.key, definition.default]),
      );
      const resourcePool = pool({
        host_setting_definitions: resourceDefinitions,
        hosts: [host({ settings_applied: defaults })],
      });
      list.mockResolvedValue(resourcePool);
      setLimit.mockResolvedValue({ ...resourcePool, host_settings: { [key]: value } });
      renderSettings('Sandbox');

      const input = await screen.findByLabelText(`${label} for all servers`);
      fireEvent.change(screen.getByLabelText(`${label} for all servers unit`), {
        target: { value: unit },
      });
      fireEvent.change(input, { target: { value: amount } });
      submit(input);

      await waitFor(() => expect(setLimit).toHaveBeenCalledWith({ [key]: value }));
      expect(await screen.findByText('Applied on 0 of 1 server')).toBeInTheDocument();
    },
  );

  it('saves zero disk quota as a value and clears it separately', async () => {
    const resourcePool = pool({
      host_setting_definitions: resourceDefinitions,
      host_settings: { sandbox_disk_mib: 20480 },
    });
    list.mockResolvedValue(resourcePool);
    setLimit.mockResolvedValueOnce({ ...resourcePool, host_settings: { sandbox_disk_mib: 0 } });
    setLimit.mockResolvedValueOnce({ ...resourcePool, host_settings: {} });
    renderSettings('Sandbox');

    const input = await screen.findByLabelText('Container disk for all servers');
    fireEvent.change(input, { target: { value: '0' } });
    submit(input);
    await waitFor(() => expect(setLimit).toHaveBeenCalledWith({ sandbox_disk_mib: 0 }));
    fireEvent.click(await screen.findByRole('button', { name: 'Use deployment default' }));
    await waitFor(() => expect(setLimit).toHaveBeenCalledWith({ sandbox_disk_mib: null }));
  });

  it('does not offer controls when no limits were reported', async () => {
    list.mockResolvedValue(pool({ host_setting_definitions: undefined }));
    renderSettings('Sandbox');
    expect(await screen.findByText('No sandbox settings are available.')).toBeVisible();
    expect(screen.queryByLabelText('Turn time limit for all servers')).not.toBeInTheDocument();
  });

  it('saves one value and reports actual application separately', async () => {
    setLimit.mockResolvedValue(pool({ host_settings: { agent_timeout_s: 7200 } }));
    renderSettings('Sandbox');
    const input = await screen.findByLabelText('Turn time limit for all servers');
    expect(screen.getAllByText('Deployment default: 1 h')).toHaveLength(1);
    expect(screen.getByLabelText('Turn time limit for all servers unit')).toHaveValue('h');
    fireEvent.change(input, { target: { value: '2' } });
    submit(input);
    await waitFor(() => expect(setLimit).toHaveBeenCalledWith({ agent_timeout_s: 7200 }));
    expect(await screen.findByText('Applied on 0 of 1 server')).toBeInTheDocument();
    expect(screen.getByText('2 h')).toBeInTheDocument();
  });

  it('rejects an out-of-range value without sending it', async () => {
    renderSettings('Sandbox');
    const input = await screen.findByLabelText('Turn time limit for all servers');
    fireEvent.change(input, { target: { value: '30' } });
    submit(input);
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter a value from 1 min to 1 d.');
    expect(setLimit).not.toHaveBeenCalled();
  });

  it('clears a global value so servers fall back to their own defaults', async () => {
    list.mockResolvedValue(pool({ host_settings: { terminal_max_sessions: 16 } }));
    setLimit.mockResolvedValue(pool());
    renderSettings('Sandbox');
    fireEvent.click(await screen.findByRole('button', { name: 'Use deployment default' }));
    await waitFor(() => expect(setLimit).toHaveBeenCalledWith({ terminal_max_sessions: null }));
  });

  it('reports unsupported servers for each setting', async () => {
    list.mockResolvedValue(
      pool({
        hosts: [host({ capabilities: [], settings_applied: null })],
        host_setting_definitions: [definitions[0], definitions[6]],
      }),
    );
    renderSettings('Sandbox');
    await screen.findByLabelText('Turn time limit for all servers');
    const sandboxCard = screen.getByRole('heading', { name: 'Sandbox' }).parentElement!;
    expect(within(sandboxCard).getAllByText(/runner-a needs a redeploy/)).toHaveLength(2);
  });

  it('retains a draft after a rejected save so it can be retried', async () => {
    setLimit.mockRejectedValueOnce(new Error('Could not save'));
    setLimit.mockResolvedValueOnce(pool({ host_settings: { agent_timeout_s: 7200 } }));
    renderSettings('Sandbox');
    const input = await screen.findByLabelText('Turn time limit for all servers');
    fireEvent.change(input, { target: { value: '2' } });
    submit(input);
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save');
    expect(input).toHaveValue(2);
    submit(input);
    expect(await screen.findByText('Applied on 0 of 1 server')).toBeInTheDocument();
    expect(setLimit).toHaveBeenCalledTimes(2);
  });

  it('takes an amount in the unit the admin picks', async () => {
    setLimit.mockResolvedValue(pool({ host_settings: { agent_timeout_s: 5400 } }));
    renderSettings('Sandbox');
    const input = await screen.findByLabelText('Turn time limit for all servers');
    fireEvent.change(screen.getByLabelText('Turn time limit for all servers unit'), {
      target: { value: 'min' },
    });
    expect(input).toHaveAttribute('placeholder', '60');
    fireEvent.change(input, { target: { value: '90' } });
    submit(input);
    await waitFor(() => expect(setLimit).toHaveBeenCalledWith({ agent_timeout_s: 5400 }));
  });

  it('warns before a shorter terminal lifetime closes open terminals', async () => {
    renderSettings('Sandbox');
    const input = await screen.findByLabelText('Terminal max lifetime for all servers');
    fireEvent.change(input, { target: { value: '4' } });
    expect(screen.getByText('Open terminals older than 4 h will close.')).toBeInTheDocument();
    fireEvent.change(input, { target: { value: '12' } });
    expect(screen.queryByText(/Open terminals older than/)).not.toBeInTheDocument();
  });

  it('names the server that sets its own value', async () => {
    list.mockResolvedValue(
      pool({ hosts: [host({ settings_desired: { terminal_max_sessions: 128 } })] }),
    );
    renderSettings('Sandbox');
    expect(await screen.findByText(/runner-a sets its own \(128\)/)).toBeInTheDocument();
  });

  it('saves archive policy atomically and waits for broker rollout', async () => {
    updateArchiveSettings.mockResolvedValue({ ...archiveSettings, enabled: true, revision: 2 });
    renderSettings('Sandbox');

    fireEvent.click(await screen.findByLabelText('Enable sandbox archives'));
    fireEvent.click(screen.getByRole('button', { name: 'Save archive settings' }));

    await waitFor(() =>
      expect(updateArchiveSettings).toHaveBeenCalledWith({
        enabled: true,
        region: archiveSettings.region,
        kms_key_arn: archiveSettings.kms_key_arn,
        expected_bucket_owner: archiveSettings.expected_bucket_owner,
        max_archive_bytes: archiveSettings.max_archive_bytes,
        multipart_threshold_bytes: archiveSettings.multipart_threshold_bytes,
        multipart_part_bytes: archiveSettings.multipart_part_bytes,
        revision: 1,
      }),
    );
    expect(await screen.findByRole('status')).toHaveTextContent('Sandbox archives enabled.');
    expect(screen.getByLabelText('Enable sandbox archives')).toBeChecked();
    expect(screen.getByText(/Waiting for runner-a/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save archive settings' })).toBeDisabled();
  });

  it('retains the archive draft after a rejected save', async () => {
    updateArchiveSettings.mockRejectedValue(new Error('Archive settings conflict'));
    renderSettings('Sandbox');

    fireEvent.click(await screen.findByLabelText('Enable sandbox archives'));
    fireEvent.click(screen.getByRole('button', { name: 'Save archive settings' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Archive settings conflict');
    expect(screen.getByLabelText('Enable sandbox archives')).toBeChecked();
  });

  it('labels a configuration-only archive save without claiming enablement changed', async () => {
    updateArchiveSettings.mockResolvedValue({
      ...archiveSettings,
      max_archive_bytes: 1_073_741_824,
      revision: 2,
    });
    renderSettings('Sandbox');

    fireEvent.change(await screen.findByLabelText('Maximum archive bytes'), {
      target: { value: '1073741824' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save archive settings' }));

    expect(await screen.findByRole('status')).toHaveTextContent('Sandbox archive settings saved.');
  });

  it('resets a normalized draft after an idempotent save keeps the same revision', async () => {
    updateArchiveSettings.mockResolvedValue(archiveSettings);
    renderSettings('Sandbox');

    const maximum = await screen.findByLabelText('Maximum archive bytes');
    fireEvent.change(maximum, { target: { value: '02147483648' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save archive settings' }));

    await waitFor(() => expect(updateArchiveSettings).toHaveBeenCalledTimes(1));
    expect(screen.getByLabelText('Maximum archive bytes')).toHaveValue(2_147_483_648);
    expect(screen.getByRole('button', { name: 'Save archive settings' })).toBeDisabled();
  });
});

describe('polling and account boundaries', () => {
  it('preserves drafts while polling, then updates application status', async () => {
    vi.useFakeTimers();
    renderSettings('Agent versions');
    await vi.waitFor(() => expect(screen.getByLabelText('Claude Code')).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText('Claude Code'), { target: { value: '2.1.235' } });
    selectSettingsTab('Sandbox');
    fireEvent.change(screen.getByLabelText('Turn time limit for all servers'), {
      target: { value: '2' },
    });
    await vi.advanceTimersByTimeAsync(30_000);
    expect(screen.getByLabelText('Claude Code')).toHaveValue('2.1.235');
    expect(screen.getByLabelText('Turn time limit for all servers')).toHaveValue(2);

    setLimit.mockResolvedValue(pool({ host_settings: { agent_timeout_s: 7200 } }));
    submit(screen.getByLabelText('Turn time limit for all servers'));
    await vi.waitFor(() =>
      expect(screen.getByText('Applied on 0 of 1 server')).toBeInTheDocument(),
    );
    list.mockResolvedValue(
      pool({
        host_settings: { agent_timeout_s: 7200 },
        hosts: [host({ settings_applied: { agent_timeout_s: 7200 } })],
      }),
    );
    await vi.advanceTimersByTimeAsync(30_000);
    expect(screen.getByText('In use on 1 server')).toBeInTheDocument();
  });

  it('discards a read that started before a successful save', async () => {
    vi.useFakeTimers();
    renderSettings('Agent versions');
    await vi.waitFor(() => expect(screen.getByLabelText('Claude Code')).toBeInTheDocument());
    let finishPoll!: (value: AgentRunnerHostPool) => void;
    list.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishPoll = resolve;
        }),
    );
    await vi.advanceTimersByTimeAsync(30_000);
    setVersion.mockResolvedValue(pool({ runtime_versions: { 'claude-code': '2.1.235' } }));
    const select = screen.getByLabelText('Claude Code');
    fireEvent.change(select, { target: { value: '2.1.235' } });
    submit(select);
    await vi.waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('sandboxes use 2.1.235'),
    );
    finishPoll(pool());
    await vi.advanceTimersByTimeAsync(0);
    expect(screen.getByLabelText('Claude Code')).toHaveValue('2.1.235');
  });

  it('keeps the last snapshot on a failed poll and labels the failure', async () => {
    vi.useFakeTimers();
    renderSettings('Agent versions');
    await vi.waitFor(() => expect(screen.getByLabelText('Claude Code')).toBeInTheDocument());
    list.mockRejectedValue(new Error('Connection lost'));
    await vi.advanceTimersByTimeAsync(30_000);
    expect(screen.getByRole('alert')).toHaveTextContent('Connection lost');
    expect(screen.getByLabelText('Claude Code')).toBeInTheDocument();
  });

  it('clears protected data and stops polling when admin access is revoked', async () => {
    vi.useFakeTimers();
    renderSettings('Agent versions');
    await vi.waitFor(() => expect(screen.getByLabelText('Claude Code')).toBeInTheDocument());
    list.mockRejectedValue(new APIError('FORBIDDEN', 'Forbidden', 403));
    await vi.advanceTimersByTimeAsync(30_000);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(screen.queryByLabelText('Claude Code')).not.toBeInTheDocument();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("does not apply the old account's pending save after switching accounts", async () => {
    let finishSave!: (value: AgentRunnerHostPool) => void;
    setVersion.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishSave = resolve;
        }),
    );
    const view = renderSettings('Agent versions');
    const select = await screen.findByLabelText('Claude Code');
    fireEvent.change(select, { target: { value: '2.1.235' } });
    submit(select);
    auth.id = 'admin-b';
    view.rerender(<AdminSettingsPage />);
    selectSettingsTab('Agent versions');
    expect(await screen.findByLabelText('Claude Code')).toHaveValue('');
    finishSave(pool({ runtime_versions: { 'claude-code': '2.1.235' } }));
    await waitFor(() => expect(screen.getByLabelText('Claude Code')).toHaveValue(''));
    expect(screen.queryByText(/sandboxes use/)).not.toBeInTheDocument();
  });
});

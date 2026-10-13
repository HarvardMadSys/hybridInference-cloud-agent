// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useSyncExternalStore } from 'react';

import type { AgentRunnerHost, AgentRunnerHostPool, HostSettingDefinition } from '@/lib/api/admin';
import type { SandboxArchiveSettings } from '@/lib/api/adminArchiveSettings';
import type { AgentAdminSettings } from '@/lib/api/adminSettings';
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

import { categoryTabId } from './ApplicationSettings';
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
  ssh_public_key: '',
  node_types: ['r6615', 'c6620', 'r650', 'r6525', 'c6525-100g', 'c6525-25g'],
  node_type_binding: 'phystype',
};

const getArchiveSettings = vi.mocked(getSandboxArchiveSettings);
const updateArchiveSettings = vi.mocked(updateSandboxArchiveSettings);

const archiveSettings: SandboxArchiveSettings = {
  enabled: false,
  s3_uri: 's3://freeinference/cloud-agent/sandbox',
  region: 'us-east-1',
  kms_key_arn: 'arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000',
  expected_bucket_owner: '123456789012',
  aws_access_key_id: '',
  aws_secret_access_key_configured: false,
  max_archive_bytes: 2_147_483_648,
  multipart_threshold_bytes: 8_388_608,
  multipart_part_bytes: 8_388_608,
  revision: 1,
  updated_at: null,
};

function applicationSnapshot(over: Partial<AgentAdminSettings> = {}): AgentAdminSettings {
  return {
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
      {
        key: 'identity.registration_enabled',
        label: 'Registration enabled',
        description: 'Allow new users to register.',
        category: 'Identity & access',
        kind: 'boolean',
        default: true,
        secret: false,
      },
      {
        key: 'future.maximum',
        label: 'Future limit',
        description: 'A setting introduced by the server.',
        category: 'Future / category ✓',
        kind: 'integer',
        default: 10,
        minimum: 1,
        maximum: 100,
        secret: false,
      },
    ],
    values: {
      'site.app_name': 'Cloud Agent',
      'identity.registration_enabled': true,
      'future.maximum': 10,
    },
    secret_configured: {},
    bootstrap: [],
    ...over,
  };
}

function organizedApplicationSnapshot(): AgentAdminSettings {
  const fields = [
    ['site.app_name', 'Appearance', 'Application name'],
    ['gateway_authorize_url', 'Sign-in', 'Sign-in authorization URL'],
    ['trust_proxy_headers', 'Security', 'Trust forwarded client addresses'],
    ['github_app_id', 'Source control', 'GitHub App ID'],
    ['publish_base_branch', 'Publication', 'Default pull request base branch'],
    ['publish_git_timeout_s', 'Publication', 'Publication Git timeout'],
    ['sandbox_archive.bucket', 'Sandbox archives', 'Sandbox archive S3 bucket'],
    ['sandbox_archive.endpoint', 'Sandbox archives', 'Sandbox archive S3 endpoint'],
  ];
  return applicationSnapshot({
    definitions: fields.map(([key, category, label]) => ({
      key,
      category,
      label,
      description: '',
      kind: 'string',
      default: '',
      secret: false,
    })),
    values: Object.fromEntries(fields.map(([key]) => [key, ''])),
  });
}

function openAdvancedForField(field: HTMLElement) {
  const details = field.closest('details')!;
  if (!details.open) fireEvent.click(details.querySelector('summary')!);
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

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
    scope: 'sandbox_manager',
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
    scope: 'sandbox_manager',
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
    scope: 'sandbox_manager',
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

type SettingsTab = string;

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
  vi.mocked(setAgentAdminSettings).mockReset();
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

    selectSettingsTab('Appearance');
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

describe('application category tabs', () => {
  beforeEach(() => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(applicationSnapshot());
  });

  it.each([
    ['Sign-in', 'Identity & access', 'Sign-in authorization URL'],
    ['Publication', 'Source control', 'Default pull request base branch'],
  ])(
    'resolves an existing %s category URL to %s after definitions load',
    async (legacy, merged, label) => {
      const pending = deferred<AgentAdminSettings>();
      vi.mocked(getAgentAdminSettings).mockReturnValueOnce(pending.promise);
      window.history.replaceState(
        null,
        '',
        `/admin/settings?context=keep&tab=${encodeURIComponent(categoryTabId(legacy))}`,
      );
      const query = window.location.search;
      renderSettings();
      expect(screen.getByRole('tab', { name: 'Application' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
      await act(async () => pending.resolve(organizedApplicationSnapshot()));
      expect(screen.getByRole('tab', { name: merged })).toHaveAttribute('aria-selected', 'true');
      expect(screen.getByRole('tabpanel', { name: merged })).toBeVisible();
      expect(screen.getByLabelText(label)).toBeVisible();
      expect(screen.queryByRole('tab', { name: legacy })).not.toBeInTheDocument();
      expect(window.location.search).toBe(query);
    },
  );

  it('opens Advanced and focuses a linked field from Publication in the merged Source control page', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(organizedApplicationSnapshot());
    window.history.replaceState(
      null,
      '',
      '/admin/settings#application-setting-publish_git_timeout_s',
    );
    renderSettings();
    const input = await screen.findByRole('textbox', { name: 'Publication Git timeout' });
    await waitFor(() => expect(input).toHaveFocus());
    expect(input.closest('details')).toHaveAttribute('open');
    expect(screen.getByRole('tabpanel', { name: 'Source control' })).toBeVisible();
    expect(screen.getByLabelText('GitHub App ID')).toBeVisible();
    expect(new URLSearchParams(window.location.search).get('tab')).toBe(
      categoryTabId('Source control'),
    );
  });

  it('expands Advanced for a missing Security field beside the merged Sign-in settings', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue({
      ...organizedApplicationSnapshot(),
      setup: {
        required: true,
        groups: [
          {
            key: 'security',
            label: 'Session security',
            required: true,
            configured: false,
            needs_setup: true,
            missing: [{ key: 'trust_proxy_headers', label: 'Trust forwarded client addresses' }],
          },
        ],
        bootstrap_missing: [],
      },
    });
    renderSettings();
    const link = await screen.findByRole('link', { name: 'Trust forwarded client addresses' });
    const input = screen.getByLabelText('Trust forwarded client addresses');
    expect(input).not.toBeVisible();
    fireEvent.click(link);
    await waitFor(() => expect(input).toHaveFocus());
    expect(input.closest('details')).toHaveAttribute('open');
    expect(screen.getByRole('tabpanel', { name: 'Identity & access' })).toBeVisible();
    expect(screen.getByLabelText('Sign-in authorization URL')).toBeVisible();
    expect(new URLSearchParams(window.location.search).get('tab')).toBe(
      categoryTabId('Identity & access'),
    );
  });

  it('puts deletion archive storage in the existing Sandbox page and saves its own setting key', async () => {
    const current = organizedApplicationSnapshot();
    vi.mocked(getAgentAdminSettings).mockResolvedValue(current);
    vi.mocked(setAgentAdminSettings).mockResolvedValue({
      ...current,
      revision: 2,
      values: { ...current.values, 'sandbox_archive.bucket': 'deletion-archive-bucket' },
    });
    window.history.replaceState(
      null,
      '',
      `/admin/settings?tab=${encodeURIComponent(categoryTabId('Sandbox archives'))}`,
    );
    renderSettings();
    const input = await screen.findByRole('textbox', { name: 'Sandbox archive S3 bucket' });
    expect(screen.queryByRole('tab', { name: 'Sandbox archives' })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Sandbox' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getAllByRole('tabpanel')).toEqual([
      screen.getByRole('tabpanel', { name: 'Sandbox' }),
    ]);
    expect(screen.getByRole('region', { name: 'Deletion archive destination' })).toContainElement(
      input,
    );
    expect(await screen.findByLabelText('Enable sandbox archives')).toBeVisible();
    expect(screen.getByLabelText('S3 destination')).toHaveValue(archiveSettings.s3_uri);
    fireEvent.change(input, { target: { value: 'deletion-archive-bucket' } });
    submit(input);
    await waitFor(() =>
      expect(setAgentAdminSettings).toHaveBeenCalledWith(
        { 'sandbox_archive.bucket': 'deletion-archive-bucket' },
        expect.any(AbortSignal),
      ),
    );
    expect(updateArchiveSettings).not.toHaveBeenCalled();
  });

  it('reveals an Advanced deletion archive link in the existing Sandbox tab', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(organizedApplicationSnapshot());
    window.history.replaceState(
      null,
      '',
      '/admin/settings#application-setting-sandbox_archive.endpoint',
    );
    renderSettings();
    const input = await screen.findByRole('textbox', { name: 'Sandbox archive S3 endpoint' });
    await waitFor(() => expect(input).toHaveFocus());
    expect(input.closest('details')).toHaveAttribute('open');
    expect(screen.getByRole('tabpanel', { name: 'Sandbox' })).toBeVisible();
    expect(new URLSearchParams(window.location.search).get('tab')).toBe('sandbox');
  });

  it('keeps application settings errors and retry available on a legacy Sandbox archives URL', async () => {
    vi.mocked(getAgentAdminSettings)
      .mockRejectedValueOnce(new Error('Deletion archive settings unavailable'))
      .mockResolvedValue(organizedApplicationSnapshot());
    window.history.replaceState(
      null,
      '',
      `/admin/settings?tab=${encodeURIComponent(categoryTabId('Sandbox archives'))}`,
    );
    renderSettings();
    const panel = screen.getByRole('tabpanel', { name: 'Sandbox' });
    expect(await within(panel).findByRole('alert')).toHaveTextContent(
      'Deletion archive settings unavailable',
    );
    fireEvent.click(within(panel).getByRole('button', { name: 'Retry application settings' }));
    expect(await screen.findByRole('textbox', { name: 'Sandbox archive S3 bucket' })).toBeVisible();
    expect(screen.getByRole('tab', { name: 'Sandbox' })).toHaveAttribute('aria-selected', 'true');
    expect(within(panel).queryByRole('alert')).not.toBeInTheDocument();
    expect(getAgentAdminSettings).toHaveBeenCalledTimes(2);
  });

  it('places every API category beside the other settings tabs with one visible panel', async () => {
    renderSettings();
    await screen.findByRole('tab', { name: 'Appearance' });
    const tablist = screen.getByRole('tablist', { name: 'Settings sections' });
    const names = [
      'Appearance',
      'Identity & access',
      'Future / category ✓',
      'Agent versions',
      'Sandbox',
      'CloudLab',
    ];
    expect(
      within(tablist)
        .getAllByRole('tab')
        .map((tab) => tab.textContent),
    ).toEqual(names);
    expect(screen.getAllByRole('tablist')).toEqual([tablist]);
    expect(screen.queryByRole('tab', { name: 'Application' })).not.toBeInTheDocument();
    expect(
      screen.queryByRole('navigation', { name: 'Application setting categories' }),
    ).not.toBeInTheDocument();
    expect(screen.getAllByRole('tabpanel', { hidden: true })).toHaveLength(names.length);
    expect(screen.getAllByRole('button', { name: 'Refresh settings', hidden: true })).toHaveLength(
      1,
    );
    expect(screen.getByRole('heading', { name: 'Appearance', level: 2 })).toBeVisible();
    expect(screen.queryByRole('heading', { name: 'Appearance', level: 3 })).not.toBeInTheDocument();

    for (const name of names) {
      selectSettingsTab(name);
      const tab = within(tablist).getByRole('tab', { name });
      const panel = screen.getByRole('tabpanel', { name });
      expect(screen.getAllByRole('tabpanel')).toEqual([panel]);
      expect(tab).toHaveAttribute('aria-selected', 'true');
      expect(tab).toHaveAttribute('tabindex', '0');
      expect(tab).toHaveAttribute('aria-controls', panel.id);
      expect(panel).toHaveAttribute('aria-labelledby', tab.id);
      expect(panel).toBeVisible();
      for (const otherName of names.filter((other) => other !== name)) {
        const otherTab = within(tablist).getByRole('tab', { name: otherName });
        expect(otherTab).toHaveAttribute('aria-selected', 'false');
        expect(otherTab).toHaveAttribute('tabindex', '-1');
        expect(document.getElementById(otherTab.getAttribute('aria-controls')!)).not.toBeVisible();
      }
    }
    expect(screen.getByLabelText('Application name')).toBeInTheDocument();
    expect(screen.getByLabelText('Registration enabled')).toBeInTheDocument();
    expect(screen.getByLabelText('Future limit')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Refresh settings', hidden: true }),
    ).not.toBeVisible();
    expect(getAgentAdminSettings).toHaveBeenCalledTimes(1);
  });

  it('navigates across the dynamic categories and fixed tabs with Arrow, Home, and End keys', async () => {
    renderSettings();
    const first = await screen.findByRole('tab', { name: 'Appearance' });
    first.focus();
    const move = (key: string, name: string) => {
      fireEvent.keyDown(document.activeElement!, { key });
      const tab = screen.getByRole('tab', { name });
      expect(tab).toHaveFocus();
      expect(tab).toHaveAttribute('aria-selected', 'true');
      expect(screen.getByRole('tabpanel', { name })).toBeVisible();
    };
    move('ArrowRight', 'Identity & access');
    move('ArrowRight', 'Future / category ✓');
    move('ArrowRight', 'Agent versions');
    move('End', 'CloudLab');
    move('ArrowRight', 'Appearance');
    move('ArrowLeft', 'CloudLab');
    move('Home', 'Appearance');
  });

  it('retains a direct category URL while loading and resolves it after definitions arrive', async () => {
    const pending = deferred<AgentAdminSettings>();
    vi.mocked(getAgentAdminSettings).mockReturnValueOnce(pending.promise);
    const tab = categoryTabId('Future / category ✓');
    window.history.replaceState(
      null,
      '',
      `/admin/settings?context=keep&tab=${encodeURIComponent(tab)}`,
    );
    const query = window.location.search;
    renderSettings();
    expect(screen.getByRole('tab', { name: 'Application' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(window.location.search).toBe(query);
    await act(async () => pending.resolve(applicationSnapshot()));
    expect(screen.getByRole('tab', { name: 'Future / category ✓' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.getByRole('spinbutton', { name: 'Future limit' })).toBeVisible();
    expect(screen.getAllByRole('tabpanel')).toHaveLength(1);
    expect(window.location.search).toBe(query);
    expect(getAgentAdminSettings).toHaveBeenCalledTimes(1);
  });

  it('keeps keyboard focus on the selected category when the loading Application tab is replaced', async () => {
    const pending = deferred<AgentAdminSettings>();
    vi.mocked(getAgentAdminSettings).mockReturnValueOnce(pending.promise);
    window.history.replaceState(
      null,
      '',
      `/admin/settings?tab=${encodeURIComponent(categoryTabId('Future / category ✓'))}`,
    );
    renderSettings();
    const loadingTab = screen.getByRole('tab', { name: 'Application' });
    loadingTab.focus();
    expect(loadingTab).toHaveFocus();
    await act(async () => pending.resolve(applicationSnapshot()));
    const category = screen.getByRole('tab', { name: 'Future / category ✓' });
    expect(loadingTab).not.toBeInTheDocument();
    expect(category).toHaveAttribute('aria-selected', 'true');
    expect(category).toHaveFocus();
    fireEvent.keyDown(category, { key: 'ArrowRight' });
    expect(screen.getByRole('tab', { name: 'Agent versions' })).toHaveFocus();
  });

  it('keeps focus outside the tabs when the application categories finish loading', async () => {
    const pending = deferred<AgentAdminSettings>();
    vi.mocked(getAgentAdminSettings).mockReturnValueOnce(pending.promise);
    renderSettings();
    screen.getByRole('tab', { name: 'Application' }).focus();
    const settingsLink = screen.getByRole('link', { name: 'Settings' });
    settingsLink.focus();
    await act(async () => pending.resolve(applicationSnapshot()));
    expect(screen.getByRole('tab', { name: 'Appearance' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(settingsLink).toHaveFocus();
  });

  it('moves focus to the first remaining category when polling removes the focused tab', async () => {
    vi.useFakeTimers();
    renderSettings();
    await vi.waitFor(() => expect(screen.getByRole('tab', { name: 'Appearance' })).toBeVisible());
    selectSettingsTab('Future / category ✓');
    const removedTab = screen.getByRole('tab', { name: 'Future / category ✓' });
    removedTab.focus();
    const current = applicationSnapshot();
    vi.mocked(getAgentAdminSettings).mockResolvedValueOnce({
      ...current,
      revision: 2,
      definitions: current.definitions.filter((definition) => definition.key !== 'future.maximum'),
    });
    await act(async () => vi.advanceTimersByTimeAsync(30_000));
    expect(removedTab).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Appearance' })).toHaveFocus();
    expect(screen.getByRole('tabpanel', { name: 'Appearance' })).toBeVisible();
    expect(getAgentAdminSettings).toHaveBeenCalledTimes(2);
  });

  it.each(['', '?tab=application', '?tab=unknown', '?tab=application-missing'])(
    'opens the first API category for the default or legacy URL %s',
    async (query) => {
      window.history.replaceState(null, '', `/admin/settings${query}`);
      renderSettings();
      expect(await screen.findByRole('tab', { name: 'Appearance' })).toHaveAttribute(
        'aria-selected',
        'true',
      );
      expect(screen.getByRole('tabpanel', { name: 'Appearance' })).toBeVisible();
      expect(getCloudLabDefaults).not.toHaveBeenCalled();
    },
  );

  it('uses stable category IDs in the URL and follows navigation without refetching or losing drafts', async () => {
    window.history.replaceState(null, '', '/admin/settings?context=keep#details');
    renderSettings();
    await screen.findByRole('tab', { name: 'Future / category ✓' });
    selectSettingsTab('Future / category ✓');
    expect(new URLSearchParams(window.location.search).get('tab')).toBe(
      categoryTabId('Future / category ✓'),
    );
    expect(new URLSearchParams(window.location.search).get('context')).toBe('keep');
    expect(window.location.hash).toBe('#details');
    fireEvent.change(screen.getByLabelText('Future limit'), { target: { value: '25' } });
    act(() =>
      window.history.replaceState(
        null,
        '',
        `/admin/settings?tab=${encodeURIComponent(categoryTabId('Identity & access'))}`,
      ),
    );
    expect(screen.getByRole('tabpanel', { name: 'Identity & access' })).toBeVisible();
    act(() =>
      window.history.replaceState(
        null,
        '',
        `/admin/settings?tab=${encodeURIComponent(categoryTabId('Future / category ✓'))}`,
      ),
    );
    expect(screen.getByRole('spinbutton', { name: 'Future limit' })).toHaveValue(25);
    expect(getAgentAdminSettings).toHaveBeenCalledTimes(1);
  });

  it('groups settings without a category under a top-level General tab', async () => {
    const current = applicationSnapshot();
    vi.mocked(getAgentAdminSettings).mockResolvedValue({
      ...current,
      definitions: current.definitions.map((definition, index) => ({
        ...definition,
        category: index === 0 ? '' : definition.category,
      })),
    });
    renderSettings();
    expect(await screen.findByRole('tab', { name: 'General' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.getByRole('tabpanel', { name: 'General' })).toBeVisible();
    expect(screen.getByRole('textbox', { name: 'Application name' })).toBeVisible();
  });

  it('reveals and focuses the category of an initial field hash after its definitions load', async () => {
    const pending = deferred<AgentAdminSettings>();
    vi.mocked(getAgentAdminSettings).mockReturnValueOnce(pending.promise);
    window.history.replaceState(null, '', '/admin/settings#application-setting-future.maximum');
    renderSettings();
    await act(async () => pending.resolve(applicationSnapshot()));
    const field = await screen.findByRole('spinbutton', { name: 'Future limit' });
    await waitFor(() => expect(field).toHaveFocus());
    expect(screen.getByRole('tabpanel', { name: 'Future / category ✓' })).toBeVisible();
    expect(screen.getByRole('tab', { name: 'Future / category ✓' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(new URLSearchParams(window.location.search).get('tab')).toBe(
      categoryTabId('Future / category ✓'),
    );
  });

  it('opens the corresponding top-level category from a missing setup field link', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(
      applicationSnapshot({
        setup: {
          required: true,
          groups: [
            {
              key: 'future',
              label: 'Future feature',
              required: true,
              configured: false,
              needs_setup: true,
              missing: [{ key: 'future.maximum', label: 'Future limit' }],
            },
          ],
          bootstrap_missing: [],
        },
      }),
    );
    renderSettings();
    const link = await screen.findByRole('link', { name: 'Future limit' });
    expect(link).toHaveAttribute('href', '#application-setting-future.maximum');
    fireEvent.click(link);
    const field = screen.getByRole('spinbutton', { name: 'Future limit' });
    await waitFor(() => expect(field).toHaveFocus());
    expect(screen.getByRole('tabpanel', { name: 'Future / category ✓' })).toBeVisible();
    expect(screen.getAllByRole('heading', { name: 'Administrator setup required' })).toHaveLength(
      1,
    );
    expect(screen.getByRole('heading', { name: 'Future / category ✓', level: 2 })).toBeVisible();
  });

  it('keeps drafts in other categories when a setting is saved or reset', async () => {
    renderSettings();
    fireEvent.change(await screen.findByLabelText('Application name'), {
      target: { value: 'Draft application' },
    });
    selectSettingsTab('Future / category ✓');
    fireEvent.change(screen.getByLabelText('Future limit'), { target: { value: '25' } });
    selectSettingsTab('Identity & access');
    const registration = screen.getByLabelText('Registration enabled');
    fireEvent.change(registration, { target: { value: 'false' } });
    const current = applicationSnapshot();
    vi.mocked(setAgentAdminSettings).mockResolvedValueOnce({
      ...current,
      revision: 2,
      values: { ...current.values, 'identity.registration_enabled': false },
    });
    submit(registration);
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent('Registration enabled saved.'),
    );
    expect(screen.getAllByRole('status')).toHaveLength(1);
    expect(setAgentAdminSettings).toHaveBeenNthCalledWith(
      1,
      { 'identity.registration_enabled': false },
      expect.any(AbortSignal),
    );
    selectSettingsTab('Appearance');
    expect(screen.getByRole('textbox', { name: 'Application name' })).toHaveValue(
      'Draft application',
    );
    expect(screen.getByRole('button', { name: 'Save Application name' })).toBeEnabled();
    selectSettingsTab('Future / category ✓');
    expect(screen.getByRole('spinbutton', { name: 'Future limit' })).toHaveValue(25);
    selectSettingsTab('Identity & access');
    vi.mocked(setAgentAdminSettings).mockResolvedValueOnce({ ...current, revision: 3 });
    fireEvent.click(screen.getByRole('button', { name: 'Reset Registration enabled' }));
    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(
        'Registration enabled reset to its default.',
      ),
    );
    expect(setAgentAdminSettings).toHaveBeenNthCalledWith(
      2,
      { 'identity.registration_enabled': null },
      expect.any(AbortSignal),
    );
    expect(screen.getByRole('combobox', { name: 'Registration enabled' })).toHaveValue('true');
    selectSettingsTab('Appearance');
    expect(screen.getByRole('textbox', { name: 'Application name' })).toHaveValue(
      'Draft application',
    );
    selectSettingsTab('Future / category ✓');
    expect(screen.getByRole('spinbutton', { name: 'Future limit' })).toHaveValue(25);
    expect(screen.getByRole('button', { name: 'Save Future limit' })).toBeEnabled();
    expect(getAgentAdminSettings).toHaveBeenCalledTimes(1);
  });

  it('shares one write lock across category tabs', async () => {
    const pending = deferred<AgentAdminSettings>();
    vi.mocked(setAgentAdminSettings).mockReturnValueOnce(pending.promise);
    renderSettings();
    await screen.findByRole('tab', { name: 'Appearance' });
    selectSettingsTab('Identity & access');
    fireEvent.change(screen.getByLabelText('Registration enabled'), {
      target: { value: 'false' },
    });
    selectSettingsTab('Appearance');
    const name = screen.getByLabelText('Application name');
    fireEvent.change(name, { target: { value: 'Saved application' } });
    submit(name);
    selectSettingsTab('Identity & access');
    expect(screen.getByRole('combobox', { name: 'Registration enabled' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save Registration enabled' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Reset Registration enabled' })).toBeDisabled();
    submit(screen.getByLabelText('Registration enabled'));
    expect(setAgentAdminSettings).toHaveBeenCalledTimes(1);
    const current = applicationSnapshot();
    await act(async () =>
      pending.resolve({
        ...current,
        revision: 2,
        values: { ...current.values, 'site.app_name': 'Saved application' },
      }),
    );
    expect(screen.getByRole('combobox', { name: 'Registration enabled' })).toHaveValue('false');
    expect(screen.getByRole('button', { name: 'Save Registration enabled' })).toBeEnabled();
  });

  it('removes every settings panel when the application API revokes access', async () => {
    renderSettings();
    await screen.findByRole('tab', { name: 'Appearance' });
    vi.mocked(getAgentAdminSettings).mockRejectedValueOnce(
      new APIError('FORBIDDEN', 'Forbidden', 403),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Refresh settings' }));
    await screen.findByRole('heading', { name: 'Admin access required' });
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
    expect(screen.queryByRole('tabpanel', { hidden: true })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Application name')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Registration enabled')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Future limit')).not.toBeInTheDocument();
  });

  it.each(['hosts', 'archives', 'CloudLab'])(
    'aborts the application read and stops polling when %s revokes access',
    async (source) => {
      vi.useFakeTimers();
      const applicationRead = deferred<AgentAdminSettings>();
      const protectedRead = deferred<never>();
      vi.mocked(getAgentAdminSettings).mockReturnValueOnce(applicationRead.promise);
      if (source === 'hosts') list.mockReturnValueOnce(protectedRead.promise);
      if (source === 'archives') getArchiveSettings.mockReturnValueOnce(protectedRead.promise);
      if (source === 'CloudLab')
        vi.mocked(getCloudLabDefaults).mockReturnValueOnce(protectedRead.promise);
      renderSettings(source === 'CloudLab' ? 'CloudLab' : 'Application');
      await vi.waitFor(() => expect(getAgentAdminSettings).toHaveBeenCalledTimes(1));
      const signal = vi.mocked(getAgentAdminSettings).mock.calls[0][0]!;
      expect(signal.aborted).toBe(false);
      await act(async () => protectedRead.reject(new APIError('FORBIDDEN', 'Forbidden', 403)));
      expect(screen.getByRole('heading', { name: 'Admin access required' })).toBeVisible();
      expect(signal.aborted).toBe(true);
      await act(async () => applicationRead.resolve(applicationSnapshot()));
      await act(async () => vi.advanceTimersByTimeAsync(90_000));
      expect(getAgentAdminSettings).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
      expect(screen.queryByLabelText('Application name')).not.toBeInTheDocument();
    },
  );

  it('aborts an application save when another settings API revokes access', async () => {
    vi.useFakeTimers();
    const pendingSave = deferred<AgentAdminSettings>();
    const protectedRead = deferred<never>();
    vi.mocked(setAgentAdminSettings).mockReturnValueOnce(pendingSave.promise);
    vi.mocked(getCloudLabDefaults).mockReturnValueOnce(protectedRead.promise);
    renderSettings();
    await vi.waitFor(() => expect(screen.getByLabelText('Application name')).toBeVisible());
    const name = screen.getByLabelText('Application name');
    fireEvent.change(name, { target: { value: 'Saved application' } });
    submit(name);
    const signal = vi.mocked(setAgentAdminSettings).mock.calls[0][1]!;
    expect(signal.aborted).toBe(false);
    selectSettingsTab('CloudLab');
    await act(async () => protectedRead.reject(new APIError('FORBIDDEN', 'Forbidden', 403)));
    expect(screen.getByRole('heading', { name: 'Admin access required' })).toBeVisible();
    expect(signal.aborted).toBe(true);
    const current = applicationSnapshot();
    await act(async () =>
      pendingSave.resolve({
        ...current,
        revision: 2,
        values: { ...current.values, 'site.app_name': 'Saved application' },
      }),
    );
    await act(async () => vi.advanceTimersByTimeAsync(90_000));
    expect(getAgentAdminSettings).toHaveBeenCalledTimes(1);
    expect(setAgentAdminSettings).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Application name')).not.toBeInTheDocument();
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
    const inventory = screen.getByRole('table', {
      name: 'Agent versions installed on each server',
      hidden: true,
    });
    expect(inventory).not.toBeVisible();
    openAdvancedForField(inventory);
    expect(inventory).toBeVisible();
    const row = within(inventory).getByRole('row', {
      name: 'runner-a Claude Code 2.1.220 Not reported',
    });
    expect(within(row).getByText('2.1.220')).toBeVisible();
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
    openAdvancedForField(await screen.findByLabelText('Terminals per server for all servers'));
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
    openAdvancedForField(screen.getByLabelText('Terminals per server for all servers'));
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
    openAdvancedForField(input);
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
    openAdvancedForField(await screen.findByLabelText('Terminals per server for all servers'));
    expect(await screen.findByText(/runner-a sets its own \(128\)/)).toBeInTheDocument();
  });

  it('saves archive policy atomically and waits for sandbox manager rollout', async () => {
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
        aws_access_key_id: '',
        aws_secret_access_key: '',
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
    const maximum = await screen.findByLabelText('Maximum archive bytes');
    openAdvancedForField(maximum);
    fireEvent.change(maximum, {
      target: { value: '1073741824' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save archive settings' }));

    expect(await screen.findByRole('status')).toHaveTextContent('Sandbox archive settings saved.');
  });

  it('resets a normalized draft after an idempotent save keeps the same revision', async () => {
    updateArchiveSettings.mockResolvedValue(archiveSettings);
    renderSettings('Sandbox');

    const maximum = await screen.findByLabelText('Maximum archive bytes');
    openAdvancedForField(maximum);
    fireEvent.change(maximum, { target: { value: '02147483648' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save archive settings' }));

    await waitFor(() => expect(updateArchiveSettings).toHaveBeenCalledTimes(1));
    expect(screen.getByLabelText('Maximum archive bytes')).toHaveValue(2_147_483_648);
    expect(screen.getByRole('button', { name: 'Save archive settings' })).toBeDisabled();
  });
});

describe('settings search', () => {
  function searchFor(query: string) {
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search settings' }), {
      target: { value: query },
    });
    return screen.getByRole('region', { name: 'Settings search results' });
  }

  it.each([
    { key: 'Enter', isComposing: true, keyCode: 13, signal: 'native composition' },
    { key: 'Escape', isComposing: true, keyCode: 27, signal: 'native composition' },
    { key: 'Enter', isComposing: false, keyCode: 229, signal: 'legacy IME key code' },
    { key: 'Escape', isComposing: false, keyCode: 229, signal: 'legacy IME key code' },
  ])(
    'ignores $key with $signal while composing a search',
    async ({ key, isComposing, keyCode }) => {
      renderSettings('Sandbox');
      await screen.findByLabelText('Turn time limit for all servers');
      const input = screen.getByRole('searchbox', { name: 'Search settings' });
      act(() => input.focus());
      fireEvent.compositionStart(input);
      const results = searchFor('claude code');
      expect(
        within(results).getByRole('button', { name: 'Open Claude Code in Agent versions' }),
      ).toBeVisible();

      expect(fireEvent.keyDown(input, { key, isComposing, keyCode })).toBe(true);
      expect(input).toHaveValue('claude code');
      expect(input).toHaveFocus();
      expect(screen.getByRole('region', { name: 'Settings search results' })).toBe(results);
      expect(screen.getByRole('tabpanel', { name: 'Sandbox' })).toBeVisible();
      expect(screen.getByRole('tab', { name: 'Sandbox' })).toHaveAttribute('aria-selected', 'true');

      fireEvent.compositionEnd(input);
      fireEvent.keyDown(input, { key });
      expect(input).toHaveValue('');
      expect(
        screen.queryByRole('region', { name: 'Settings search results' }),
      ).not.toBeInTheDocument();
      if (key === 'Enter') {
        await waitFor(() => expect(screen.getByLabelText('Claude Code')).toHaveFocus());
        expect(screen.getByRole('tabpanel', { name: 'Agent versions' })).toBeVisible();
      } else {
        expect(input).toHaveFocus();
        expect(screen.getByRole('tabpanel', { name: 'Sandbox' })).toBeVisible();
      }
    },
  );

  it('opens an Advanced application result in its merged category and clears the query', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(organizedApplicationSnapshot());
    renderSettings();
    await screen.findByRole('tab', { name: 'Appearance' });
    const input = screen.getByLabelText('Publication Git timeout');
    expect(input).not.toBeVisible();

    const results = searchFor('PUBLICATION timeout');
    expect(within(results).getAllByRole('button')).toHaveLength(1);
    fireEvent.click(
      within(results).getByRole('button', {
        name: 'Open Publication Git timeout in Source control',
      }),
    );

    await waitFor(() => expect(input).toHaveFocus());
    expect(input.closest('details')).toHaveAttribute('open');
    expect(screen.getByRole('tabpanel', { name: 'Source control' })).toBeVisible();
    expect(new URLSearchParams(window.location.search).get('tab')).toBe(
      categoryTabId('Source control'),
    );
    expect(screen.getByRole('searchbox', { name: 'Search settings' })).toHaveValue('');
    expect(
      screen.queryByRole('region', { name: 'Settings search results' }),
    ).not.toBeInTheDocument();
    expect(setAgentAdminSettings).not.toHaveBeenCalled();
  });

  it('selects an unfamiliar server-defined category with Enter without losing an application draft', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(applicationSnapshot());
    renderSettings();
    const applicationName = await screen.findByLabelText('Application name');
    fireEvent.change(applicationName, { target: { value: 'Unsaved application name' } });

    const results = searchFor('future.maximum');
    expect(within(results).getAllByRole('button')).toHaveLength(1);
    fireEvent.keyDown(screen.getByRole('searchbox', { name: 'Search settings' }), {
      key: 'Enter',
    });

    const future = screen.getByRole('spinbutton', { name: 'Future limit' });
    await waitFor(() => expect(future).toHaveFocus());
    expect(screen.getByRole('tabpanel', { name: 'Future / category ✓' })).toBeVisible();
    expect(screen.getByRole('searchbox', { name: 'Search settings' })).toHaveValue('');
    expect(applicationName).toBeInTheDocument();
    expect(applicationName).not.toBeVisible();
    expect(applicationName).toHaveValue('Unsaved application name');

    fireEvent.click(
      within(searchFor('application name')).getByRole('button', {
        name: 'Open Application name in Appearance',
      }),
    );
    await waitFor(() => expect(applicationName).toHaveFocus());
    expect(screen.getByRole('textbox', { name: 'Application name' })).toBe(applicationName);
    expect(screen.getByRole('button', { name: 'Save Application name' })).toBeEnabled();
    expect(getAgentAdminSettings).toHaveBeenCalledTimes(1);
  });

  it('opens an Advanced host setting by its environment key and preserves its draft across searches', async () => {
    renderSettings();
    await screen.findByText('No application settings are available.');
    const input = await screen.findByLabelText('Terminals per server for all servers');
    expect(input).not.toBeVisible();

    fireEvent.click(
      within(searchFor('AGENT_TERMINAL_MAX_SESSIONS')).getByRole('button', {
        name: 'Open Terminals per server in Sandbox',
      }),
    );

    await waitFor(() => expect(input).toHaveFocus());
    expect(input.closest('details')).toHaveAttribute('open');
    expect(screen.getByRole('tabpanel', { name: 'Sandbox' })).toBeVisible();
    fireEvent.change(input, { target: { value: '100' } });
    fireEvent.click(
      within(searchFor('claude-code')).getByRole('button', {
        name: 'Open Claude Code in Agent versions',
      }),
    );
    await waitFor(() => expect(screen.getByLabelText('Claude Code')).toHaveFocus());
    expect(input).toHaveValue(100);
    expect(input).not.toBeVisible();

    fireEvent.click(
      within(searchFor('Terminals per server')).getByRole('button', {
        name: 'Open Terminals per server in Sandbox',
      }),
    );
    await waitFor(() => expect(input).toHaveFocus());
    expect(screen.getByLabelText('Terminals per server for all servers')).toBe(input);
    expect(input).toHaveValue(100);
    expect(setLimit).not.toHaveBeenCalled();
    expect(setVersion).not.toHaveBeenCalled();
  });

  it('searches CloudLab before visiting it and focuses its Advanced field after defaults arrive', async () => {
    const pending = deferred<{ defaults: typeof cloudLabDefaults }>();
    vi.mocked(getCloudLabDefaults).mockReturnValueOnce(pending.promise);
    renderSettings();
    await screen.findByText('No application settings are available.');

    const results = searchFor('cloudlab ssh public key');
    expect(getCloudLabDefaults).not.toHaveBeenCalled();
    fireEvent.click(
      within(results).getByRole('button', {
        name: 'Open CloudLab SSH public key in CloudLab',
      }),
    );
    expect(screen.getByRole('tabpanel', { name: 'CloudLab' })).toBeVisible();
    expect(getCloudLabDefaults).toHaveBeenCalledTimes(1);
    expect(screen.queryByLabelText('CloudLab SSH public key')).not.toBeInTheDocument();
    expect(screen.getByRole('searchbox', { name: 'Search settings' })).toHaveValue('');

    await act(async () => pending.resolve({ defaults: cloudLabDefaults }));
    const input = await screen.findByLabelText('CloudLab SSH public key');
    await waitFor(() => expect(input).toHaveFocus());
    expect(input.closest('details')).toHaveAttribute('open');
    expect(input).toBeVisible();
    expect(saveCloudLabDefaults).not.toHaveBeenCalled();
  });

  it('keeps a new search focused when defaults arrive for an earlier CloudLab result', async () => {
    const pending = deferred<{ defaults: typeof cloudLabDefaults }>();
    vi.mocked(getCloudLabDefaults).mockReturnValueOnce(pending.promise);
    renderSettings();
    await screen.findByText('No application settings are available.');
    fireEvent.click(
      within(searchFor('cloudlab ssh public key')).getByRole('button', {
        name: 'Open CloudLab SSH public key in CloudLab',
      }),
    );
    expect(getCloudLabDefaults).toHaveBeenCalledTimes(1);

    const searchInput = screen.getByRole('searchbox', { name: 'Search settings' });
    act(() => searchInput.focus());
    searchFor('claude code');
    await act(async () => pending.resolve({ defaults: cloudLabDefaults }));

    const oldTarget = await screen.findByLabelText('CloudLab SSH public key');
    expect(searchInput).toHaveFocus();
    expect(searchInput).toHaveValue('claude code');
    expect(
      within(screen.getByRole('region', { name: 'Settings search results' })).getByRole('button', {
        name: 'Open Claude Code in Agent versions',
      }),
    ).toBeVisible();
    expect(oldTarget.closest('details')).not.toHaveAttribute('open');
    expect(oldTarget).not.toBeVisible();
  });

  it('cancels delayed CloudLab result focus when the user chooses another tab', async () => {
    const pending = deferred<{ defaults: typeof cloudLabDefaults }>();
    vi.mocked(getCloudLabDefaults).mockReturnValueOnce(pending.promise);
    renderSettings();
    await screen.findByText('No application settings are available.');
    fireEvent.click(
      within(searchFor('cloudlab ssh public key')).getByRole('button', {
        name: 'Open CloudLab SSH public key in CloudLab',
      }),
    );

    const sandboxTab = screen.getByRole('tab', { name: 'Sandbox' });
    act(() => sandboxTab.focus());
    fireEvent.click(sandboxTab);
    await act(async () => pending.resolve({ defaults: cloudLabDefaults }));
    const oldTarget = await screen.findByLabelText('CloudLab SSH public key');
    expect(sandboxTab).toHaveFocus();
    expect(screen.getByRole('tabpanel', { name: 'Sandbox' })).toBeVisible();

    const cloudLabTab = screen.getByRole('tab', { name: 'CloudLab' });
    act(() => cloudLabTab.focus());
    fireEvent.click(cloudLabTab);
    expect(cloudLabTab).toHaveFocus();
    expect(oldTarget.closest('details')).not.toHaveAttribute('open');
    expect(oldTarget).not.toBeVisible();
  });

  it('routes an Advanced archive policy result to Sandbox and focuses its editor', async () => {
    renderSettings();
    await screen.findByText('No application settings are available.');
    await screen.findByLabelText('Maximum archive bytes');
    fireEvent.click(
      within(searchFor('max_archive_bytes')).getByRole('button', {
        name: 'Open Maximum archive bytes in Sandbox archives',
      }),
    );

    const input = screen.getByLabelText('Maximum archive bytes');
    await waitFor(() => expect(input).toHaveFocus());
    expect(input.closest('details')).toHaveAttribute('open');
    expect(screen.getByRole('tabpanel', { name: 'Sandbox' })).toBeVisible();
    expect(new URLSearchParams(window.location.search).get('tab')).toBe('sandbox');
    expect(updateArchiveSettings).not.toHaveBeenCalled();
  });

  it('routes deletion archive metadata to the existing Sandbox page', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(organizedApplicationSnapshot());
    renderSettings();
    await screen.findByRole('tab', { name: 'Appearance' });
    fireEvent.click(
      within(searchFor('sandbox_archive.endpoint')).getByRole('button', {
        name: 'Open Sandbox archive S3 endpoint in Sandbox',
      }),
    );

    const input = screen.getByLabelText('Sandbox archive S3 endpoint');
    await waitFor(() => expect(input).toHaveFocus());
    expect(input.closest('details')).toHaveAttribute('open');
    expect(screen.getByRole('tabpanel', { name: 'Sandbox' })).toBeVisible();
    expect(screen.getByRole('region', { name: 'Deletion archive destination' })).toContainElement(
      input,
    );
    expect(screen.queryByRole('tab', { name: 'Sandbox archives' })).not.toBeInTheDocument();
  });

  it('reveals and focuses deployment prerequisites from another settings section', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(
      applicationSnapshot({
        bootstrap: [
          {
            key: 'AGENT_DATABASE_URL',
            label: 'Database connection',
            description: 'Database connection supplied before the service starts.',
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

    const target = screen.getByText('Database connection').closest('div')!;
    await waitFor(() => expect(target).toHaveFocus());
    expect(target.closest('details')).toHaveAttribute('open');
    expect(screen.getByRole('tabpanel', { name: 'Appearance' })).toBeVisible();
    expect(target).toBeVisible();
  });

  it('focuses the setting form when an application result is read-only', async () => {
    const current = applicationSnapshot();
    vi.mocked(getAgentAdminSettings).mockResolvedValue({
      ...current,
      definitions: current.definitions.map((definition) => ({
        ...definition,
        read_only: definition.key === 'future.maximum',
      })),
    });
    renderSettings();
    await screen.findByRole('tab', { name: 'Appearance' });
    fireEvent.click(
      within(searchFor('future limit')).getByRole('button', {
        name: 'Open Future limit in Future / category ✓',
      }),
    );

    const input = screen.getByRole('spinbutton', { name: 'Future limit' });
    expect(input).toBeDisabled();
    await waitFor(() => expect(input.closest('form')).toHaveFocus());
    expect(screen.getByRole('tabpanel', { name: 'Future / category ✓' })).toBeVisible();
  });

  it('keeps the current section for no matches and clears search with Escape or the clear button', async () => {
    renderSettings('Sandbox');
    await screen.findByLabelText('Turn time limit for all servers');
    const input = screen.getByRole('searchbox', { name: 'Search settings' });
    const results = searchFor('no-such-setting-ever');
    expect(within(results).getByRole('status')).toHaveTextContent('No settings found.');
    expect(within(results).queryByRole('button')).not.toBeInTheDocument();
    input.focus();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(input).toHaveValue('no-such-setting-ever');
    expect(screen.getByRole('tabpanel', { name: 'Sandbox' })).toBeVisible();

    fireEvent.keyDown(input, { key: 'Escape' });
    expect(input).toHaveValue('');
    expect(input).toHaveFocus();
    expect(
      screen.queryByRole('region', { name: 'Settings search results' }),
    ).not.toBeInTheDocument();
    searchFor('CloudLab');
    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }));
    expect(input).toHaveValue('');
    expect(input).toHaveFocus();
    expect(
      screen.queryByRole('region', { name: 'Settings search results' }),
    ).not.toBeInTheDocument();
    expect(getCloudLabDefaults).not.toHaveBeenCalled();
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
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search settings' }), {
      target: { value: 'Claude Code' },
    });
    expect(screen.getByRole('region', { name: 'Settings search results' })).toBeInTheDocument();
    list.mockRejectedValue(new APIError('FORBIDDEN', 'Forbidden', 403));
    await vi.advanceTimersByTimeAsync(30_000);
    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(screen.queryByLabelText('Claude Code')).not.toBeInTheDocument();
    expect(screen.queryByRole('searchbox', { name: 'Search settings' })).not.toBeInTheDocument();
    expect(
      screen.queryByRole('region', { name: 'Settings search results' }),
    ).not.toBeInTheDocument();
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

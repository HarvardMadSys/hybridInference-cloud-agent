// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  clearMemories,
  getMemories,
  getMemoryProjects,
  setMemoryEnabled,
  type AgentMemoryScope,
} from '@/lib/api/agentMemory';
import { getInsights, type WorkInsights } from '@/lib/api/insights';
import { getUserProfile } from '@/lib/api/userProfile';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { NEUTRAL_SITE_CONFIG } from '@/config/site';
import { IntegrationsView } from './IntegrationsView';
import { MySettingsView } from '../settings/MySettingsView';
import { PluginsSettingsView } from '../settings/PluginsSettingsView';
import { EnvironmentsSettingsView } from '../settings/EnvironmentsSettingsView';
import type { AgentIntegrationProviderApi, AgentIntegrationsApi } from '@/lib/api/agents';

vi.mock('@/components/features/agents/environment/EnvironmentManager', () => ({
  EnvironmentManager: () => <div data-testid="environment-manager" />,
}));

vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: { id: 'settings-owner' } } }),
}));

import { listSkills } from '@/lib/api/skills';
vi.mock('@/lib/api/skills', () => ({ listSkills: vi.fn() }));
vi.mock('@/lib/api/userProfile', () => ({ getUserProfile: vi.fn(), saveUserProfile: vi.fn() }));

vi.mock('@/lib/api/agents', () => ({
  getAgentConfig: vi.fn(),
  getAgentIntegrations: vi.fn(),
  disconnectAgentIntegration: vi.fn(),
  disconnectGitHubAccount: vi.fn(),
}));

vi.mock('@/lib/api/agentMemory', () => ({
  getMemories: vi.fn(),
  addMemory: vi.fn(),
  updateMemory: vi.fn(),
  deleteMemory: vi.fn(),
  restoreMemory: vi.fn(),
  clearMemories: vi.fn(),
  setMemoryEnabled: vi.fn(),
  getMemoryProjects: vi.fn(),
}));

vi.mock('@/lib/api/insights', () => ({ getInsights: vi.fn() }));

// The MCP section loads its own connections; mocked empty so this file stays
// about the source-control rows.
vi.mock('@/lib/api/mcp', () => ({
  listMcpConnections: vi.fn(),
  testMcpConnection: vi.fn(),
  disconnectMcpConnection: vi.fn(),
  authorizeMcpConnection: vi.fn(),
}));

import {
  disconnectAgentIntegration,
  disconnectGitHubAccount,
  getAgentConfig,
  getAgentIntegrations,
} from '@/lib/api/agents';
import { listMcpConnections } from '@/lib/api/mcp';

const mockedGetIntegrations = getAgentIntegrations as unknown as ReturnType<typeof vi.fn>;
const mockedDisconnect = disconnectAgentIntegration as unknown as ReturnType<typeof vi.fn>;
const mockedListMcpConnections = listMcpConnections as unknown as ReturnType<typeof vi.fn>;

const COMPANION = 'data:image/svg+xml;base64,Zml4ZWQ=';

const insights: WorkInsights = {
  focus_areas: [],
  avatar: { data_url: 'data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=', style: 'illustrated' },
  suggestions: [
    {
      id: 'suggestion-1',
      title: 'Include a validation command',
      reason: 'Recent tasks often require test fixes.',
      action: 'Add the expected test command to future requests.',
    },
  ],
};

const memory: AgentMemoryScope = {
  repo: null,
  enabled: true,
  memories: [
    {
      id: 'preference-1',
      repo: null,
      content: 'Use small pull requests.',
      description: 'Use small pull requests.',
      status: 'active',
      origin: 'agent',
      source_job_ids: ['job_memory_source'],
      version: 0,
      created_at: '2026-10-14T00:00:00Z',
      updated_at: '2026-10-14T00:00:00Z',
      last_used_at: null,
      archived_at: null,
    },
  ],
  archived: [],
};

const integrations: AgentIntegrationsApi = {
  providers: [
    {
      provider: 'github',
      configured: true,
      connected: true,
      connect_url: 'https://github.com/apps/example/installations/new?state=signed',
      capabilities: ['repository:read', 'pull_request:write'],
      accounts: [{ id: 'gh-42', label: 'HarvardMadSys' }],
      repositories: [
        { id: 'one', name: 'HarvardMadSys/one' },
        { id: 'two', name: 'HarvardMadSys/two' },
      ],
    },
    {
      provider: 'gitlab',
      configured: true,
      connected: false,
      connect_url: 'https://gitlab.com/oauth/authorize?state=signed',
      capabilities: ['repository:read'],
      accounts: [],
      repositories: [],
    },
  ],
};

/** GitHub as the service reports it while its App cannot be loaded. */
function unloadedGithub(
  overrides: Partial<AgentIntegrationProviderApi> = {},
): AgentIntegrationProviderApi {
  return {
    provider: 'github',
    configured: false,
    connected: false,
    connect_url: null,
    capabilities: [],
    accounts: [],
    repositories: [],
    load_status: 'unavailable',
    ...overrides,
  };
}

beforeEach(() => {
  vi.mocked(getUserProfile).mockResolvedValue({
    email: 'owner@example.com',
    first_name: '',
    last_name: '',
    image_data_url: null,
    revision: 0,
    companion_data_url: COMPANION,
  });
  localStorage.clear();
  document.documentElement.lang = 'en';
  vi.mocked(getAgentConfig).mockResolvedValue({
    models: ['model-a', 'model-b'],
    repos: [],
    runtimes: ['codex'],
    github_connected: true,
    github_install_url: null,
  });
  vi.mocked(listSkills).mockResolvedValue([]);
  mockedListMcpConnections.mockResolvedValue({ connections: [], templates: [] });
  vi.mocked(getInsights).mockResolvedValue(insights);
  vi.mocked(getMemories).mockResolvedValue(memory);
  vi.mocked(getMemoryProjects).mockResolvedValue([]);
  vi.mocked(setMemoryEnabled).mockResolvedValue({ ...memory, enabled: false });
  vi.mocked(clearMemories).mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('IntegrationsView', () => {
  it('shows environment management even while the capability request is unavailable', async () => {
    vi.mocked(getAgentConfig).mockRejectedValue(new Error('Config unavailable'));
    mockedGetIntegrations.mockResolvedValue(integrations);
    render(<EnvironmentsSettingsView />);
    expect(screen.getByTestId('environment-manager')).toBeInTheDocument();
    await screen.findByRole('heading', { level: 1, name: 'Environments' });
  });
  it('shows connected source-control accounts independently of other settings', async () => {
    mockedGetIntegrations.mockResolvedValue(integrations);
    render(<IntegrationsView connectedProvider="github" />);
    expect(screen.getByRole('heading', { level: 1, name: 'Integrations' })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Keyboard shortcuts' })).not.toBeInTheDocument();
    expect(
      await screen.findByText('Connected as HarvardMadSys · 2 repositories'),
    ).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('GitHub connected.');
    expect(screen.queryByRole('link', { name: /Connect/ })).not.toBeInTheDocument();
    expect(screen.getAllByText('Coming soon')).toHaveLength(1);
    expect(getUserProfile).not.toHaveBeenCalled();
    expect(getMemories).not.toHaveBeenCalled();
    expect(listSkills).not.toHaveBeenCalled();
    expect(listMcpConnections).not.toHaveBeenCalled();
  });
  it('keeps profile, notifications, keyboard shortcuts, and memory in My Settings', async () => {
    render(<MySettingsView />);
    expect(screen.getByRole('heading', { level: 1, name: 'My Settings' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Profile' })).toBeInTheDocument();
    expect(screen.queryByText('Integrations & MCP')).not.toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Notifications' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Keyboard shortcuts' })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Agent questions' })).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Turn completions' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Memory' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: 'Memory' })).toBeInTheDocument();
    expect(screen.getByRole('tabpanel', { name: 'User memory' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'Project memory' }));
    expect(screen.getByRole('combobox', { name: 'Repository' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Theme' })).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox', { name: 'Display language' })).not.toBeInTheDocument();
    expect(mockedGetIntegrations).not.toHaveBeenCalled();
    expect(listSkills).not.toHaveBeenCalled();
    expect(listMcpConnections).not.toHaveBeenCalled();
    await waitFor(() => expect(getUserProfile).toHaveBeenCalled());
  });
  it('groups skills with MCP servers and loads only their data', async () => {
    render(<PluginsSettingsView />);
    expect(screen.getByRole('heading', { level: 1, name: 'Plugins & MCPs' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: 'MCP servers' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add with JSON' })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Add server' })).toBeInTheDocument();
    expect(listSkills).toHaveBeenCalled();
    expect(mockedGetIntegrations).not.toHaveBeenCalled();
    expect(getUserProfile).not.toHaveBeenCalled();
    expect(getMemories).not.toHaveBeenCalled();
  });

  it('keeps documentation out of the page heading even when configured', async () => {
    mockedGetIntegrations.mockResolvedValue(integrations);
    const docsUrl = 'https://docs.example.com/agents/';
    const { unmount } = render(
      <SiteConfigProvider
        config={{ ...NEUTRAL_SITE_CONFIG, branding: { ...NEUTRAL_SITE_CONFIG.branding, docsUrl } }}
      >
        <IntegrationsView />
      </SiteConfigProvider>,
    );
    expect(screen.queryByRole('link', { name: 'Documentation' })).not.toBeInTheDocument();
    await screen.findByText('Connected as HarvardMadSys · 2 repositories');
    unmount();

    render(<IntegrationsView />);
    await screen.findByText('Connected as HarvardMadSys · 2 repositories');
    expect(screen.queryByRole('link', { name: 'Documentation' })).not.toBeInTheDocument();
  });

  it("shows the account's fixed companion in Profile, independent of memory and insights", async () => {
    mockedGetIntegrations.mockResolvedValue(integrations);
    render(<MySettingsView />);
    const image = await within(screen.getByRole('region', { name: 'Profile' })).findByRole('img', {
      name: 'Your Pokémon companion avatar',
    });
    expect(image).toHaveAttribute('src', COMPANION);
    fireEvent.click(screen.getByRole('tab', { name: 'Project memory' }));
    expect(screen.getByRole('tabpanel', { name: 'Project memory' })).toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: 'User memory' }));
    const section = screen.getByRole('tabpanel', { name: 'User memory' });
    fireEvent.click(await within(section).findByRole('switch', { name: 'Use memory' }));
    await waitFor(() => expect(setMemoryEnabled).toHaveBeenCalled());
    expect(image).toHaveAttribute('src', COMPANION);
    expect(getInsights).not.toHaveBeenCalled();
  });

  it('keeps the uploaded photo over the companion', async () => {
    const photo = 'data:image/png;base64,cGhvdG8=';
    vi.mocked(getUserProfile).mockResolvedValue({
      email: 'owner@example.com',
      first_name: '',
      last_name: '',
      image_data_url: photo,
      revision: 1,
      companion_data_url: COMPANION,
    });
    mockedGetIntegrations.mockResolvedValue(integrations);
    render(<MySettingsView />);
    const profileSection = screen.getByRole('region', { name: 'Profile' });
    const image = await within(profileSection).findByRole('img', { name: 'Your profile image' });
    expect(image).toHaveAttribute('src', photo);
    expect(
      within(profileSection).queryByRole('img', { name: 'Your Pokémon companion avatar' }),
    ).not.toBeInTheDocument();
  });

  it('manages memories on the Setting page while suggestions live on the homepage', async () => {
    mockedGetIntegrations.mockResolvedValue(integrations);
    render(<MySettingsView />);

    const section = screen.getByRole('tabpanel', { name: 'User memory' });
    expect(await within(section).findByText(memory.memories[0].content)).toBeInTheDocument();
    expect(within(section).getByText('Saved by an agent')).toBeInTheDocument();
    expect(within(section).queryByRole('button', { name: 'Consolidate' })).not.toBeInTheDocument();
    expect(within(section).queryByRole('img')).not.toBeInTheDocument();
    expect(within(section).queryByText(insights.suggestions[0].title)).not.toBeInTheDocument();
    const toggle = within(section).getByRole('switch', { name: 'Use memory' });
    vi.mocked(getMemories).mockResolvedValueOnce({ ...memory, enabled: false });
    fireEvent.click(toggle);
    await waitFor(() => expect(toggle).not.toBeChecked());
    expect(setMemoryEnabled).toHaveBeenCalledWith(null, false, expect.any(AbortSignal));
    expect(within(section).getByText(/tasks do not see these memories/)).toBeInTheDocument();

    fireEvent.click(within(section).getByRole('button', { name: 'Clear' }));
    expect(clearMemories).not.toHaveBeenCalled();
    vi.mocked(getMemories).mockResolvedValueOnce({ ...memory, memories: [] });
    fireEvent.click(within(section).getByRole('button', { name: 'Yes, clear' }));
    await within(section).findByText(/No memories yet/);
    expect(clearMemories).toHaveBeenCalledWith(null, expect.any(AbortSignal));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('translates the Setting page and its memory card into Chinese', async () => {
    mockedGetIntegrations.mockResolvedValue(integrations);
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    render(
      <LocaleProvider>
        <MySettingsView />
      </LocaleProvider>,
    );

    expect(await screen.findByRole('heading', { level: 1, name: '我的设置' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '个人资料' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '记忆' })).toBeInTheDocument();
    const section = screen.getByRole('tabpanel', { name: '用户记忆' });
    await within(section).findByRole('switch', { name: '启用记忆' });
    expect(within(section).queryByRole('img')).not.toBeInTheDocument();
    expect(within(section).getByRole('switch', { name: '启用记忆' })).toBeChecked();
    expect(within(section).queryByRole('heading', { name: '建议' })).not.toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '项目记忆' })).toBeInTheDocument();
    expect(screen.getByText('管理你的个人资料、通知、键盘快捷键和记忆。')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '键盘快捷键' })).toBeInTheDocument();
  });

  it('marks providers that need operator configuration', async () => {
    mockedGetIntegrations.mockResolvedValue({ providers: [] });

    render(<IntegrationsView />);

    // GitHub only: a provider that is not offered yet does not tell a user to
    // go and ask an admin for it.
    expect(await screen.findAllByText('Admin setup required')).toHaveLength(1);
    expect(screen.getAllByText('Coming soon')).toHaveLength(1);
  });

  // Issue #47: a GitHub App the service could not load reads as "temporarily
  // unavailable". "Admin setup required" sent operators to check configuration
  // that was already correct, when the service only had to read its key again.
  it('says an App the service could not load is temporarily unavailable', async () => {
    mockedGetIntegrations.mockResolvedValue({
      providers: [unloadedGithub(), integrations.providers[1]],
    });

    render(<IntegrationsView />);

    expect(await screen.findByText('Temporarily unavailable')).toBeInTheDocument();
    expect(screen.queryByText('Admin setup required')).not.toBeInTheDocument();
  });

  it('keeps a connected account on screen, and removable, while the App is unavailable', async () => {
    mockedGetIntegrations.mockResolvedValue({
      providers: [
        unloadedGithub({ connected: true, accounts: integrations.providers[0].accounts }),
        integrations.providers[1],
      ],
    });

    render(<IntegrationsView />);

    expect(await screen.findByText('HarvardMadSys · Temporarily unavailable')).toBeInTheDocument();
    expect(screen.queryByText(/Connected as/)).not.toBeInTheDocument();
    expect(screen.queryByText('Reauthorize GitHub')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disconnect GitHub' })).toBeInTheDocument();
  });

  it('looks again while GitHub is unavailable, and stops once it is back', async () => {
    vi.useFakeTimers();
    try {
      mockedGetIntegrations
        .mockResolvedValueOnce({ providers: [unloadedGithub(), integrations.providers[1]] })
        .mockResolvedValue(integrations);
      await act(async () => {
        render(<IntegrationsView />);
      });
      expect(screen.getByText('Temporarily unavailable')).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      expect(screen.getByText('Connected as HarvardMadSys · 2 repositories')).toBeInTheDocument();
      const calls = mockedGetIntegrations.mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(90_000);
      });
      expect(mockedGetIntegrations).toHaveBeenCalledTimes(calls);
    } finally {
      vi.useRealTimers();
    }
  });

  it('never lets an older re-check undo a disconnect made after it was sent', async () => {
    // A background re-check sent before a disconnect can be answered after the
    // reload that followed it. Applied in arrival order, it put the removed
    // account back on the page.
    vi.useFakeTimers();
    try {
      const connectedWhileDown: AgentIntegrationsApi = {
        providers: [
          unloadedGithub({ connected: true, accounts: integrations.providers[0].accounts }),
          integrations.providers[1],
        ],
      };
      let answerStaleCheck: (value: AgentIntegrationsApi) => void = () => undefined;
      mockedGetIntegrations
        .mockResolvedValueOnce(connectedWhileDown)
        .mockImplementationOnce(
          () =>
            new Promise<AgentIntegrationsApi>((resolve) => {
              answerStaleCheck = resolve;
            }),
        )
        .mockResolvedValue({ providers: [unloadedGithub(), integrations.providers[1]] });
      vi.mocked(disconnectGitHubAccount).mockResolvedValue({
        providers: [unloadedGithub(), integrations.providers[1]],
      });
      await act(async () => {
        render(<IntegrationsView />);
      });
      expect(screen.getByText('HarvardMadSys · Temporarily unavailable')).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000); // the re-check goes out, unanswered
      });
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Disconnect GitHub' }));
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(screen.queryByText('HarvardMadSys · Temporarily unavailable')).not.toBeInTheDocument();

      await act(async () => {
        answerStaleCheck(connectedWhileDown);
        await vi.advanceTimersByTimeAsync(0);
      });

      expect(screen.queryByText('HarvardMadSys · Temporarily unavailable')).not.toBeInTheDocument();
      expect(screen.getByText('Temporarily unavailable')).toBeInTheDocument();
      expect(screen.queryByLabelText('Loading GitHub')).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not keep asking about a GitLab that failed to load', async () => {
    // The service never retries GitLab: its configuration is environment only.
    vi.useFakeTimers();
    try {
      mockedGetIntegrations.mockResolvedValue({
        providers: [
          integrations.providers[0],
          {
            ...integrations.providers[1],
            configured: false,
            connect_url: null,
            load_status: 'unavailable',
          },
        ],
      });
      await act(async () => {
        render(<IntegrationsView />);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(90_000);
      });

      expect(mockedGetIntegrations).toHaveBeenCalledTimes(1);
      expect(screen.getByText('Coming soon')).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('does not trust the callback query string as connection status', async () => {
    mockedGetIntegrations.mockResolvedValue({
      providers: integrations.providers.map((provider) => ({
        ...provider,
        connected: false,
        accounts: [],
        repositories: [],
      })),
    });

    render(<IntegrationsView connectedProvider="github" />);

    await screen.findByRole('link', { name: /Connect/ });
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('still lets a user disconnect a stored account after provider setup is removed', async () => {
    mockedGetIntegrations.mockResolvedValue({
      providers: [{ ...integrations.providers[0], configured: false, connect_url: null }],
    });

    render(<IntegrationsView />);

    await screen.findByText('Connected as HarvardMadSys · 2 repositories');
    expect(screen.getByText('Manage')).toBeInTheDocument();
    // Was 1, from the GitLab row this response omits — that row now reads
    // "Coming soon" instead, and GitHub's is the connected one.
    expect(screen.queryAllByText('Admin setup required')).toHaveLength(0);
  });

  // Beside Manage rather than inside it: adding a personal account or another
  // organization happens there, and behind a menu nobody found it.
  it('offers a repository configuration link for a connected installation', async () => {
    mockedGetIntegrations.mockResolvedValue({
      providers: [
        {
          ...integrations.providers[0],
          manage_url: 'https://github.com/apps/example/installations/new?state=manage',
        },
        integrations.providers[1],
      ],
    });

    render(<IntegrationsView />);

    await screen.findByText('Connected as HarvardMadSys · 2 repositories');

    expect(screen.getByRole('link', { name: 'Configure repositories' })).toHaveAttribute(
      'href',
      'https://github.com/apps/example/installations/new?state=manage',
    );
  });

  it('omits the configuration link when the gateway offers no install URL', async () => {
    mockedGetIntegrations.mockResolvedValue(integrations);

    render(<IntegrationsView />);

    await screen.findByText('Connected as HarvardMadSys · 2 repositories');
    fireEvent.click(screen.getByText('Manage'));

    expect(screen.queryByRole('link', { name: 'Configure repositories' })).not.toBeInTheDocument();
  });

  // One way out for GitHub. Removing an installation lasted only until the
  // next connection, and reauthorizing only mattered for an installation made
  // outside this page — both covered by disconnecting and connecting again.
  it('offers GitHub only its one way out', async () => {
    mockedGetIntegrations.mockResolvedValue({
      providers: [
        {
          ...integrations.providers[0],
          accounts: [
            { id: 'gh-42', label: 'HarvardMadSys' },
            { id: 'gh-43', label: 'realtmxi' },
          ],
        },
        integrations.providers[1],
      ],
    });

    render(<IntegrationsView />);
    await screen.findByText('Connected as HarvardMadSys, realtmxi · 2 repositories');
    fireEvent.click(screen.getByText('Manage'));

    expect(screen.getByRole('button', { name: 'Disconnect GitHub' })).toBeInTheDocument();
    expect(screen.queryByText('Reauthorize GitHub')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /Disconnect HarvardMadSys/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Disconnect realtmxi/ })).not.toBeInTheDocument();
  });

  // The user's token is kept for GitHub to be asked as them; removing
  // installations one by one leaves it, so the whole connection has its own way out.
  it('disconnects GitHub as a whole and shows the state the service answers', async () => {
    mockedGetIntegrations.mockResolvedValue(integrations);
    vi.mocked(disconnectGitHubAccount).mockResolvedValue({
      providers: [
        { ...integrations.providers[0], connected: false, accounts: [], repositories: [] },
        integrations.providers[1],
      ],
    });

    render(<IntegrationsView />);
    await screen.findByText('Connected as HarvardMadSys · 2 repositories');
    fireEvent.click(screen.getByText('Manage'));
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect GitHub' }));

    expect(await screen.findByRole('status')).toHaveTextContent('GitHub disconnected.');
    expect(screen.queryByText(/Connected as/)).not.toBeInTheDocument();
    expect(mockedDisconnect).not.toHaveBeenCalled();
    // One request: the answer is the page's state, not a reason to reload it.
    expect(mockedGetIntegrations).toHaveBeenCalledTimes(1);
  });

  it('keeps a connected GitLab account manageable', async () => {
    // "Coming soon" replaces the invitation to connect, not the way out of a
    // connection somebody already has. Hiding Manage here would strand them
    // with a token grant they cannot revoke from this page.
    mockedGetIntegrations.mockResolvedValue({
      providers: [
        {
          ...integrations.providers[1],
          connected: true,
          accounts: [{ id: 'gl-7', label: 'murphy' }],
        },
      ],
    });

    render(<IntegrationsView />);

    expect(await screen.findByText('Manage')).toBeInTheDocument();
    // None left: GitLab's is gone because it is connected, and the MCP section
    // no longer carries a placeholder pill.
    expect(screen.queryAllByText('Coming soon')).toHaveLength(0);
  });
});

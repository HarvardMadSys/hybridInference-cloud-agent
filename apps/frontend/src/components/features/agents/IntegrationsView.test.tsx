// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { IntegrationsView } from './IntegrationsView';
import type { AgentIntegrationProviderApi, AgentIntegrationsApi } from '@/lib/api/agents';

import { listSkills } from '@/lib/api/skills';
vi.mock('@/lib/api/skills', () => ({ listSkills: vi.fn() }));

vi.mock('@/lib/api/agents', () => ({
  getAgentIntegrations: vi.fn(),
  disconnectAgentIntegration: vi.fn(),
  disconnectGitHubAccount: vi.fn(),
}));

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
  getAgentIntegrations,
} from '@/lib/api/agents';
import { listMcpConnections } from '@/lib/api/mcp';

const mockedGetIntegrations = getAgentIntegrations as unknown as ReturnType<typeof vi.fn>;
const mockedDisconnect = disconnectAgentIntegration as unknown as ReturnType<typeof vi.fn>;
const mockedListMcpConnections = listMcpConnections as unknown as ReturnType<typeof vi.fn>;

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
  vi.mocked(listSkills).mockResolvedValue([]);
  mockedListMcpConnections.mockResolvedValue({ connections: [], templates: [] });
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('IntegrationsView', () => {
  it('shows connected accounts and a same-window connect link', async () => {
    mockedGetIntegrations.mockResolvedValue(integrations);

    render(<IntegrationsView connectedProvider="github" />);

    expect(screen.getByRole('heading', { level: 1, name: 'Integrations' })).toBeInTheDocument();
    expect(screen.queryByText('Integrations & MCP')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: 'MCP servers' })).toBeInTheDocument();
    expect(
      await screen.findByText('Connected as HarvardMadSys · 2 repositories'),
    ).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('GitHub connected.');
    // GitLab is behind "Coming soon", so its connect_url is deliberately not
    // offered even though the API still returns one.
    expect(screen.queryByRole('link', { name: /Connect/ })).not.toBeInTheDocument();
    // One: GitLab's. The MCP section is a real list now, not a "Coming soon"
    // placeholder, so it contributes a button instead.
    expect(screen.getAllByText('Coming soon')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'Add with JSON' })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: 'Add server' })).toBeInTheDocument();
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
